"""Audit judge runner tests (R9.7, R9.8, R9.9, R9.12, R3.2).

Hermetic: real migrations against in-memory SQLite (FK on), a fake injected
judge client returning canned JudgeResults — no network, ever. Covers record
assembly, the happy path, re-run dedupe, the R9.12 no-key / over-budget /
no-signals skips, per-signal error containment, and deterministic stratified
sampling.
"""
import json
import sqlite3
import unittest

from app.audit import schema
from app.audit.client import JudgeResult
from app.audit.judge import assemble_record, run_audit, sample_signals
from app.db.migrate import apply_migrations

NOW = "2026-08-11T00:00:00+00:00"
MODEL = "claude-haiku-4-5"


def _ok_verdicts():
    return {c: {"result": "pass", "notes": f"{c} ok"} for c in schema.CHECKS}


class FakeClient:
    """Injectable stand-in for AuditClient. ``results_by_signal`` maps a
    signal_id to the JudgeResult to return; ``default`` is used otherwise.
    Records every record it was asked to judge."""

    def __init__(self, model_id=MODEL, default=None, results_by_signal=None,
                 available=True):
        self.model_id = model_id
        self._available = available
        self._default = default
        self._by_signal = results_by_signal or {}
        self.calls = []

    def available(self):
        return self._available

    def judge(self, record):
        self.calls.append(record["signal_id"])
        if record["signal_id"] in self._by_signal:
            return self._by_signal[record["signal_id"]]
        if self._default is not None:
            return self._default
        return JudgeResult("ok", _ok_verdicts(), self.model_id,
                           0.0001, 100, 20, "")


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)

    conn.execute(
        "INSERT INTO source_policies (source_id, name, evidence_rank) "
        "VALUES ('src_a', 'Source A', 1)")
    conn.execute(
        "INSERT INTO source_policies (source_id, name, evidence_rank) "
        "VALUES ('src_b', 'Source B', 2)")
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        " decay_half_life_days) VALUES ('t_lead', 'Leadership change', 4, 90)")
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        " decay_half_life_days) VALUES ('t_reg', 'Regulatory', 5, 600)")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name) "
        "VALUES ('EA1', 'Acme Utilities')")
    conn.execute(
        "INSERT INTO products (product_id, name) "
        "VALUES ('P_DEF', 'Defender for IoT')")
    conn.execute(
        "INSERT INTO license_play_candidates (play_id, trigger_id, product_id, "
        " recommended_path, discovery_question) "
        "VALUES ('play_1', 't_lead', 'P_DEF', 'attach', 'Do you monitor OT?')")

    _raw_event(conn, "src_a:1", "src_a",
               {"title": "Acme names new CISO", "body": "appointment"})
    _raw_event(conn, "src_a:2", "src_a",
               {"title": "Acme reorg", "body": "leadership shuffle"})
    _raw_event(conn, "src_b:1", "src_b",
               {"title": "FERC CIP order", "body": "regulatory calendar"})

    # account-scope signal WITH a license play (src_a:1, t_lead, EA1)
    _signal(conn, "t_lead:src_a:1:EA1", "src_a:1", "EA1", "account", "t_lead",
            "Acme Utilities names new CISO", "PC")
    conn.execute(
        "INSERT INTO signal_evidence (signal_id, raw_event_id, evidence_text, "
        " evidence_locator, evidence_rank, extraction_version) "
        "VALUES ('t_lead:src_a:1:EA1', 'src_a:1', 'Acme names new CISO', "
        " 'title', 1, 'v1')")
    conn.execute(
        "INSERT INTO license_play_snapshots (signal_id, play_id, fact_ids, "
        " generated_at, generation_version, display_text, outreach_safe_text) "
        "VALUES ('t_lead:src_a:1:EA1', 'play_1', '[]', ?, 'g1', "
        " 'Attach Defender for IoT', 'safe')", (NOW,))

    # account-scope signal WITHOUT a license play (src_a:2, t_lead, EA1)
    _signal(conn, "t_lead:src_a:2:EA1", "src_a:2", "EA1", "account", "t_lead",
            "Acme leadership reorg", "PC")
    conn.execute(
        "INSERT INTO signal_evidence (signal_id, raw_event_id, evidence_text, "
        " evidence_locator, evidence_rank, extraction_version) "
        "VALUES ('t_lead:src_a:2:EA1', 'src_a:2', 'leadership shuffle', "
        " 'body', 1, 'v1')")

    # sector-scope signal with NULL entity (src_b:1, t_reg, sector)
    _signal(conn, "t_reg:src_b:1:sector", "src_b:1", None, "sector", "t_reg",
            "FERC directs CIP revisions", "IR")
    conn.execute(
        "INSERT INTO signal_evidence (signal_id, raw_event_id, evidence_text, "
        " evidence_locator, evidence_rank, extraction_version) "
        "VALUES ('t_reg:src_b:1:sector', 'src_b:1', 'CIP order', "
        " 'abstract', 1, 'v1')")

    conn.commit()
    return conn


def _raw_event(conn, raw_event_id, source_id, payload):
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, url, "
        " payload, first_seen_at) VALUES (?, ?, '2026-08-01', "
        " 'https://example.test/e', ?, '2026-08-01T00:00:00Z')",
        (raw_event_id, source_id, json.dumps(payload)))


def _signal(conn, signal_id, raw_event_id, entity_id, scope, trigger_id,
            headline, quality):
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
        " signal_scope, trigger_id, event_date, headline, evidence_snippet, "
        " evidence_quality, customer_facing_allowed, status) "
        "VALUES (?, ?, ?, ?, ?, '2026-08-01', ?, ?, ?, 1, 'active')",
        (signal_id, raw_event_id, entity_id, scope, trigger_id, headline,
         headline, quality))


class TestAssembleRecord(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def test_shape_evidence_and_raw_source(self):
        rec = assemble_record(self.conn, "t_lead:src_a:1:EA1")
        self.assertEqual(rec["signal_id"], "t_lead:src_a:1:EA1")
        self.assertEqual(rec["entity_name"], "Acme Utilities")
        self.assertEqual(rec["signal_scope"], "account")
        self.assertEqual(rec["trigger_name"], "Leadership change")
        self.assertEqual(rec["evidence_quality"], "PC")
        self.assertIn("Acme names new CISO", rec["raw_source_text"])
        self.assertEqual(len(rec["evidence"]), 1)
        self.assertEqual(rec["evidence"][0]["text"], "Acme names new CISO")
        self.assertEqual(rec["evidence"][0]["locator"], "title")

    def test_license_play_present(self):
        rec = assemble_record(self.conn, "t_lead:src_a:1:EA1")
        self.assertEqual(len(rec["license_plays"]), 1)
        play = rec["license_plays"][0]
        self.assertEqual(play["product_name"], "Defender for IoT")
        self.assertEqual(play["recommended_path"], "attach")
        self.assertEqual(play["display_text"], "Attach Defender for IoT")

    def test_no_license_play_is_empty(self):
        rec = assemble_record(self.conn, "t_lead:src_a:2:EA1")
        self.assertEqual(rec["license_plays"], [])

    def test_sector_scope_entity_name_none(self):
        rec = assemble_record(self.conn, "t_reg:src_b:1:sector")
        self.assertIsNone(rec["entity_name"])
        self.assertEqual(rec["signal_scope"], "sector")

    def test_unknown_signal_returns_none(self):
        self.assertIsNone(assemble_record(self.conn, "nope"))


class TestRunAudit(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def _audit_rows(self, signal_id=None):
        if signal_id:
            return self.conn.execute(
                "SELECT * FROM audit WHERE signal_id = ? ORDER BY check_type",
                (signal_id,)).fetchall()
        return self.conn.execute(
            "SELECT * FROM audit ORDER BY signal_id, check_type").fetchall()

    def _run_row(self, run_id):
        return self.conn.execute(
            "SELECT * FROM audit_runs WHERE run_id = ?", (run_id,)).fetchone()

    def test_happy_path_writes_four_rows_per_signal(self):
        client = FakeClient()
        summary = run_audit(self.conn, client, now=NOW)
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["signals_sampled"], 3)
        self.assertEqual(summary["verdicts_written"], 3 * len(schema.CHECKS))
        self.assertGreater(summary["budget_spent"], 0.0)

        rows = self._audit_rows("t_lead:src_a:1:EA1")
        self.assertEqual({r["check_type"] for r in rows}, set(schema.CHECKS))
        for r in rows:
            self.assertEqual(r["model_id"], MODEL)
            self.assertEqual(r["prompt_version"], schema.PROMPT_VERSION)
            self.assertEqual(r["parser_version"], schema.PARSER_VERSION)
            self.assertEqual(r["ts"], NOW)
            self.assertEqual(r["result"], "pass")

        run = self._run_row(summary["run_id"])
        self.assertEqual(run["status"], "success")
        self.assertEqual(run["signals_sampled"], 3)
        self.assertEqual(run["verdicts_written"], 3 * len(schema.CHECKS))
        self.assertEqual(run["finished_at"], NOW)
        self.assertEqual(run["prompt_version"], schema.PROMPT_VERSION)
        self.assertEqual(run["parser_version"], schema.PARSER_VERSION)
        self.assertGreater(run["budget_spent"], 0.0)

    def test_rerun_same_prompt_version_audits_nothing_new(self):
        run_audit(self.conn, FakeClient(), now=NOW)
        first_count = self.conn.execute(
            "SELECT COUNT(*) FROM audit").fetchone()[0]
        summary = run_audit(self.conn, FakeClient(), now=NOW)
        self.assertEqual(summary["status"], "skipped_no_signals")
        self.assertEqual(summary["signals_sampled"], 0)
        self.assertEqual(summary["verdicts_written"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM audit").fetchone()[0], first_count)

    def test_no_key_skips_and_writes_no_verdicts(self):
        client = FakeClient(available=False)
        summary = run_audit(self.conn, client, now=NOW)
        self.assertEqual(summary["status"], "skipped_no_key")
        self.assertEqual(summary["verdicts_written"], 0)
        self.assertTrue(summary["skipped_reason"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM audit").fetchone()[0], 0)
        self.assertEqual(client.calls, [])   # judge never called
        run = self._run_row(summary["run_id"])
        self.assertEqual(run["status"], "skipped_no_key")
        self.assertEqual(run["finished_at"], NOW)

    def test_over_budget_mid_run_persists_partial(self):
        # First sampled signal returns ok; the second trips the budget.
        ids = sample_signals(self.conn, schema.PROMPT_VERSION, 20, rng_seed=0)
        second = ids[1]
        client = FakeClient(results_by_signal={
            second: JudgeResult("over_budget", None, MODEL, 0.0, 0, 0,
                                "per-run budget $0.5000 reached")})
        summary = run_audit(self.conn, client, now=NOW)
        # loop stopped at the 2nd signal: only the first signal's verdicts stay
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["verdicts_written"], len(schema.CHECKS))
        self.assertIn("stopped early", summary["skipped_reason"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(DISTINCT signal_id) FROM audit").fetchone()[0], 1)
        self.assertEqual(len(client.calls), 2)   # stopped after the 2nd

    def test_over_budget_before_any_verdict_is_skip(self):
        first = sample_signals(
            self.conn, schema.PROMPT_VERSION, 20, rng_seed=0)[0]
        client = FakeClient(results_by_signal={
            first: JudgeResult("over_budget", None, MODEL, 0.0, 0, 0,
                               "budget reached")})
        summary = run_audit(self.conn, client, now=NOW)
        self.assertEqual(summary["status"], "skipped_over_budget")
        self.assertEqual(summary["verdicts_written"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM audit").fetchone()[0], 0)

    def test_per_signal_error_is_contained(self):
        ids = sample_signals(self.conn, schema.PROMPT_VERSION, 20, rng_seed=0)
        bad = ids[0]
        client = FakeClient(results_by_signal={
            bad: JudgeResult("error", None, MODEL, 0.0, 0, 0,
                             "parse error: bad json")})
        summary = run_audit(self.conn, client, now=NOW)
        # the errored signal has no rows; the other two proceed
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["verdicts_written"], 2 * len(schema.CHECKS))
        self.assertEqual(self._audit_rows(bad), [])
        self.assertEqual(len(client.calls), 3)   # all signals attempted
        run = self._run_row(summary["run_id"])
        self.assertEqual(run["status"], "success")
        self.assertIn("parse error", run["error_state"])

    def test_all_signals_error_finalizes_error(self):
        client = FakeClient(default=JudgeResult(
            "error", None, MODEL, 0.0, 0, 0, "boom"))
        summary = run_audit(self.conn, client, now=NOW)
        self.assertEqual(summary["status"], "error")
        self.assertEqual(summary["verdicts_written"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM audit").fetchone()[0], 0)

    def test_limit_capped_and_applied(self):
        summary = run_audit(self.conn, FakeClient(), now=NOW, limit=1)
        self.assertEqual(summary["signals_sampled"], 1)
        self.assertEqual(summary["verdicts_written"], len(schema.CHECKS))


class TestSampling(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def test_deterministic_for_fixed_seed(self):
        a = sample_signals(self.conn, schema.PROMPT_VERSION, 2, rng_seed=7)
        b = sample_signals(self.conn, schema.PROMPT_VERSION, 2, rng_seed=7)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 2)

    def test_excludes_already_audited(self):
        # mark one signal audited at this prompt_version
        self.conn.execute(
            "INSERT INTO audit (signal_id, check_type, result, prompt_version, "
            " parser_version, ts) VALUES ('t_lead:src_a:1:EA1', 'entity_match', "
            " 'pass', ?, ?, ?)",
            (schema.PROMPT_VERSION, schema.PARSER_VERSION, NOW))
        self.conn.commit()
        ids = sample_signals(self.conn, schema.PROMPT_VERSION, 20, rng_seed=0)
        self.assertNotIn("t_lead:src_a:1:EA1", ids)
        self.assertEqual(len(ids), 2)

    def test_stratified_spread_across_triggers(self):
        # limit 2 across two triggers should draw from both strata, not one.
        ids = sample_signals(self.conn, schema.PROMPT_VERSION, 2, rng_seed=0)
        triggers = {
            self.conn.execute(
                "SELECT trigger_id FROM signals WHERE signal_id = ?",
                (sid,)).fetchone()["trigger_id"] for sid in ids}
        self.assertEqual(triggers, {"t_lead", "t_reg"})


if __name__ == "__main__":
    unittest.main()
