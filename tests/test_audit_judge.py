"""Audit judge runner tests (R9.7, R9.8, R9.9, R9.12, R3.2).

Hermetic: real migrations against in-memory SQLite (FK on), a fake injected
judge client returning canned JudgeResults — no network, ever. Covers record
assembly, the happy path, re-run dedupe, the R9.12 no-key / over-budget /
no-signals skips, per-signal error containment, deterministic stratified
sampling, the reap of runs stranded at 'running' by a killed process, and the
date-aware model price schedule (R10.1).
"""
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout

from app.audit import config as cfg
from app.audit import schema
from app.audit.client import JudgeResult
from app.audit.judge import (REAP_NOTE, assemble_record, cli,
                             estimate_pending_cost, run_audit, sample_signals)
from app.db.migrate import apply_migrations

NOW = "2026-08-11T00:00:00+00:00"
MODEL = "claude-haiku-4-5"


@contextmanager
def throwaway_db():
    """A migrated temp-file store wired to GRIDSIGNALS_DB, torn down after.

    Mirrors tests/test_precision_cli.py's helper: cli() opens whatever
    get_connection() resolves, so a CLI-level test needs GRIDSIGNALS_DB
    pointed at a throwaway file, never data/gridsignals.db.
    """
    saved = os.environ.get("GRIDSIGNALS_DB")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(conn)
        os.environ["GRIDSIGNALS_DB"] = path
        try:
            yield conn
        finally:
            conn.close()
    finally:
        if saved is None:
            os.environ.pop("GRIDSIGNALS_DB", None)
        else:
            os.environ["GRIDSIGNALS_DB"] = saved
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)


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


def _bulk_signals(conn, n, prefix="bulk"):
    """Add N more account-scope, t_lead/src_a unaudited signals on top of
    fixture_conn's 3, so a test can prove the U26 default is uncapped rather
    than stopping at the old min(20, all) ceiling."""
    for i in range(n):
        rid = f"src_a:{prefix}{i}"
        sid = f"t_lead:{rid}:EA1"
        _raw_event(conn, rid, "src_a", {"title": f"Acme update {i}", "body": "x"})
        _signal(conn, sid, rid, "EA1", "account", "t_lead",
                f"Acme update {i}", "PC")
    conn.commit()


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

    def test_no_limit_samples_beyond_the_old_twenty_signal_cap(self):
        # U26: the operator dropped R9.7's literal min(20, all) cap. With no
        # --limit, a backlog over 20 must ALL be sampled — bounded only by the
        # budget, never a fixed count. FakeClient's default never reports
        # over_budget, so every signal reaches 'ok'.
        _bulk_signals(self.conn, 25)   # 3 fixture + 25 = 28 unaudited total
        summary = run_audit(self.conn, FakeClient(), now=NOW)
        self.assertEqual(summary["signals_sampled"], 28)
        self.assertGreater(summary["signals_sampled"], 20)
        self.assertEqual(summary["verdicts_written"], 28 * len(schema.CHECKS))

    def test_explicit_limit_still_caps_a_large_backlog(self):
        # An explicit --limit N remains a real cap even when the backlog is
        # far bigger than N — only the IMPLICIT default became uncapped.
        _bulk_signals(self.conn, 25)
        summary = run_audit(self.conn, FakeClient(), now=NOW, limit=5)
        self.assertEqual(summary["signals_sampled"], 5)


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

    def test_limit_none_selects_every_eligible_signal(self):
        # U26: sample_signals' own default is now "no cap" — the caller no
        # longer has to pass a large number to get everything.
        _bulk_signals(self.conn, 25)
        ids = sample_signals(self.conn, schema.PROMPT_VERSION, None, rng_seed=0)
        self.assertEqual(len(ids), 28)
        self.assertEqual(len(set(ids)), 28)   # no duplicates


class TestReapStrandedRuns(unittest.TestCase):
    """B6 / R9.12: a run killed mid-flight strands its audit_runs row at
    'running' forever, because only _finalize_run moves a row off it. The next
    run must close that row out — marked, never deleted."""

    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def _insert_stranded(self, run_id, started_at):
        self.conn.execute(
            "INSERT INTO audit_runs (run_id, started_at, model_id, status) "
            "VALUES (?, ?, ?, 'running')", (run_id, started_at, MODEL))
        self.conn.commit()

    def test_stranded_row_is_reaped_and_new_run_unaffected(self):
        self._insert_stranded("audit:dead0000", "2026-08-10T00:00:00+00:00")
        summary = run_audit(self.conn, FakeClient(), now=NOW)

        dead = self.conn.execute(
            "SELECT * FROM audit_runs WHERE run_id = 'audit:dead0000'").fetchone()
        self.assertIsNotNone(dead, "the row must be marked, never deleted")
        self.assertEqual(dead["status"], "error")
        self.assertEqual(dead["error_state"], REAP_NOTE)
        self.assertEqual(dead["finished_at"], NOW)
        # its own bookkeeping is untouched by the reap
        self.assertEqual(dead["started_at"], "2026-08-10T00:00:00+00:00")

        # the new run is unaffected: it completes normally and writes verdicts
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["verdicts_written"], 3 * len(schema.CHECKS))
        new = self.conn.execute(
            "SELECT * FROM audit_runs WHERE run_id = ?",
            (summary["run_id"],)).fetchone()
        self.assertEqual(new["status"], "success")
        self.assertEqual(new["error_state"], "")

    def test_every_stranded_row_is_reaped(self):
        # audit_runs has no scope column, so the reap cannot be qualified: any
        # pre-existing 'running' row predates this invocation.
        self._insert_stranded("audit:dead0001", "2026-08-09T00:00:00+00:00")
        self._insert_stranded("audit:dead0002", "2026-08-10T00:00:00+00:00")
        run_audit(self.conn, FakeClient(), now=NOW)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM audit_runs WHERE status = 'running'"
        ).fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM audit_runs WHERE error_state = ?",
            (REAP_NOTE,)).fetchone()[0], 2)

    def test_finalized_rows_are_never_touched(self):
        self.conn.execute(
            "INSERT INTO audit_runs (run_id, started_at, status, skipped_reason) "
            "VALUES ('audit:ok0001', '2026-08-01T00:00:00+00:00', "
            "'skipped_no_key', 'ANTHROPIC_API_KEY not set')")
        self.conn.commit()
        run_audit(self.conn, FakeClient(), now=NOW)
        row = self.conn.execute(
            "SELECT * FROM audit_runs WHERE run_id = 'audit:ok0001'").fetchone()
        self.assertEqual(row["status"], "skipped_no_key")
        self.assertEqual(row["error_state"], "")
        self.assertEqual(row["finished_at"], "")


class TestPriceSchedule(unittest.TestCase):
    """B8 / R10.1: claude-sonnet-5 launched with promotional $2/$10 through
    2026-08-31, but Anthropic made that price permanent on 2026-08-10. The rate
    still resolves through a schedule so a future announced change can be pinned
    to the run's UTC timestamp."""

    def test_sonnet_5_rate_before_the_original_boundary(self):
        self.assertEqual(
            cfg.price_per_mtok("claude-sonnet-5", "2026-08-31T23:59:59+00:00"),
            (2.00, 10.00))

    def test_sonnet_5_rate_stays_flat_after_the_original_boundary(self):
        self.assertEqual(
            cfg.price_per_mtok("claude-sonnet-5", "2026-09-01T00:00:00+00:00"),
            (2.00, 10.00))

    def test_cost_estimate_follows_the_schedule(self):
        self.assertAlmostEqual(
            cfg.estimate_cost_usd("claude-sonnet-5", 1_000_000, 1_000_000,
                                  at="2026-08-15T00:00:00+00:00"),
            12.0, places=6)
        self.assertAlmostEqual(
            cfg.estimate_cost_usd("claude-sonnet-5", 1_000_000, 1_000_000,
                                  at="2026-09-15T00:00:00+00:00"),
            12.0, places=6)

    def test_models_without_a_scheduled_change_are_flat(self):
        for at in ("2026-08-15T00:00:00+00:00", "2026-09-15T00:00:00+00:00"):
            # Haiku is DEFAULT_MODEL_ID, so it alone prices live spend today.
            self.assertEqual(cfg.price_per_mtok("claude-haiku-4-5", at),
                             (1.00, 5.00))
            self.assertEqual(cfg.price_per_mtok("claude-opus-4-8", at),
                             (5.00, 25.00))

    def test_unknown_model_still_falls_back_to_haiku_rate(self):
        self.assertEqual(cfg.price_per_mtok("some-future-model",
                                            "2026-09-15T00:00:00+00:00"),
                         (1.00, 5.00))

    def test_default_timestamp_is_now(self):
        # No `at` -> the moment the spend is incurred; must still be a real
        # scheduled rate, never zero.
        in_rate, out_rate = cfg.price_per_mtok("claude-sonnet-5")
        self.assertIn((in_rate, out_rate), {(2.00, 10.00), (3.00, 15.00)})


class TestEstimatePendingCost(unittest.TestCase):
    """U26: a pre-run cost projection for the CURRENT pending batch. Read-only
    by construction — it never builds an AuditClient and never calls .judge()."""

    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def test_estimate_covers_every_pending_signal_and_is_read_only(self):
        est = estimate_pending_cost(self.conn)
        self.assertEqual(est["signals"], 3)
        self.assertGreater(est["input_tokens"], 0)
        self.assertEqual(est["output_tokens"], 3 * cfg.MAX_TOKENS)
        self.assertGreater(est["cost_usd"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM audit").fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM audit_runs").fetchone()[0], 0)

    def test_estimate_is_uncapped_over_a_large_backlog(self):
        # Mirrors run_audit with no --limit: the estimate is for the WHOLE
        # pending batch, not bounded by any CLI --limit the operator might
        # also pass — the estimate answers "what would a full run cost".
        _bulk_signals(self.conn, 25)
        est = estimate_pending_cost(self.conn)
        self.assertEqual(est["signals"], 28)

    def test_estimate_excludes_already_audited_signals(self):
        self.conn.execute(
            "INSERT INTO audit (signal_id, check_type, result, prompt_version, "
            " parser_version, ts) VALUES ('t_lead:src_a:1:EA1', 'entity_match', "
            " 'pass', ?, ?, ?)",
            (schema.PROMPT_VERSION, schema.PARSER_VERSION, NOW))
        self.conn.commit()
        est = estimate_pending_cost(self.conn)
        self.assertEqual(est["signals"], 2)


class TestCli(unittest.TestCase):
    """CLI-level coverage for --estimate (U26): a throwaway file-backed store
    via GRIDSIGNALS_DB, matching tests/test_precision_cli.py's pattern, since
    cli() opens whatever get_connection() resolves rather than taking a conn."""

    def test_estimate_prints_a_projection_and_touches_nothing(self):
        with throwaway_db() as conn:
            _seed_minimal_cli_fixture(conn)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli(["--estimate"])
            self.assertEqual(code, 0)
            output = buf.getvalue()
            self.assertIn("audit --estimate:", output)
            self.assertIn("estimated", output)
            self.assertIn("signals", output)
            self.assertIn("actual may vary", output)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM audit").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM audit_runs").fetchone()[0], 0)

    def test_estimate_never_takes_the_ingestion_lock(self):
        # Read-only: --estimate must not contend with a concurrent ingest run
        # for app/ingest/runner.py's single-writer lock.
        with throwaway_db():
            lock_path = os.path.join(tempfile.gettempdir(), "gs-estimate-test.lock")
            if os.path.exists(lock_path):
                os.remove(lock_path)
            saved = os.environ.get("GRIDSIGNALS_LOCK")
            os.environ["GRIDSIGNALS_LOCK"] = lock_path
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = cli(["--estimate"])
                self.assertEqual(code, 0)
                self.assertFalse(
                    os.path.exists(lock_path),
                    "--estimate must never create/take the ingestion lock",
                )
            finally:
                if saved is None:
                    os.environ.pop("GRIDSIGNALS_LOCK", None)
                else:
                    os.environ["GRIDSIGNALS_LOCK"] = saved
                if os.path.exists(lock_path):
                    os.remove(lock_path)


def _seed_minimal_cli_fixture(conn):
    """The smallest store estimate_pending_cost can report on non-trivially:
    one source, one trigger, one entity, one raw_event, one signal."""
    conn.execute(
        "INSERT INTO source_policies (source_id, name, evidence_rank) "
        "VALUES ('src_a', 'Source A', 1)")
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        " decay_half_life_days) VALUES ('t_lead', 'Leadership change', 4, 90)")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name) "
        "VALUES ('EA1', 'Acme Utilities')")
    _raw_event(conn, "src_a:1", "src_a",
               {"title": "Acme names new CISO", "body": "appointment"})
    _signal(conn, "t_lead:src_a:1:EA1", "src_a:1", "EA1", "account", "t_lead",
            "Acme Utilities names new CISO", "PC")
    conn.commit()


if __name__ == "__main__":
    unittest.main()
