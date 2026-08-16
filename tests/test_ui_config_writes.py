"""Config-write helper tests (R8.7 Admin/Config; R3.2, R3.3, R7.4, R8.1).

Hermetic: real migrations against in-memory SQLite, FK on, fixture rows. Covers
the three writers (update_weight, update_half_life, set_source_enabled), their
validation, the config_audit provenance row, the frozen-score contract, the
never-DELETE guarantee (regression gate b), and the audit-tail / source-policy
reads. The single-writer lock (config_write_conn) is exercised in
test_ui_admin.py against a temp-file DB; here we call the helpers with a passed
connection so the DB assertions stay hermetic.

Also covers R3.2's bounded exponential backoff on the two lock-free sanctioned
writes (record_feedback, triage_decision) - see TestWriteBackoff.
"""
import sqlite3
import unittest
from datetime import datetime, timezone

from app.db.migrate import apply_migrations
from app.ui import data

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)

    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_lead','Leadership',4,90)")
    conn.execute("INSERT INTO scoring_weights (weight_kind, key, weight, notes) "
                 "VALUES ('scope','sector',0.55,'seed note')")
    conn.execute("INSERT INTO source_policies (source_id, name, enabled) "
                 "VALUES ('edgar','EDGAR',1)")
    # An active signal (rescored) and a frozen decayed signal, both on t_lead.
    conn.execute(
        "INSERT INTO signals (signal_id, signal_scope, trigger_id, event_date, "
        " headline, status, score, score_base, score_decay, score_account_fit, "
        " score_scope_fit) VALUES "
        "('s_active','sector','t_lead','2026-08-01','Active card','active', "
        " 2.2, 4, 1.0, 1.0, 0.55)")
    conn.execute(
        "INSERT INTO signals (signal_id, signal_scope, trigger_id, event_date, "
        " headline, status, score, score_base, score_decay, score_account_fit, "
        " score_scope_fit) VALUES "
        "('s_frozen','sector','t_lead','2025-01-01','Old card','decayed', "
        " 0.5, 4, 0.25, 1.0, 0.5)")
    conn.commit()
    return conn


class TestUpdateWeight(unittest.TestCase):
    def test_happy_updates_audits_and_rescores(self):
        conn = fixture_conn()
        result = data.update_weight(conn, "scope", "sector", 0.80,
                                    reason="tuning", now=NOW)
        self.assertEqual(result["old"], 0.55)
        self.assertEqual(result["new"], 0.80)
        self.assertTrue(result["changed"])
        self.assertGreaterEqual(result["scored"], 1)
        # stored value changed
        self.assertEqual(conn.execute(
            "SELECT weight FROM scoring_weights WHERE weight_kind='scope' "
            "AND key='sector'").fetchone()["weight"], 0.80)
        # exactly one audit row, correct shape
        audit = conn.execute("SELECT * FROM config_audit").fetchall()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["table_name"], "scoring_weights")
        self.assertEqual(audit[0]["field"], "weight")
        self.assertEqual(audit[0]["old_value"], "0.55")
        self.assertEqual(audit[0]["new_value"], "0.8")
        self.assertEqual(audit[0]["reason"], "tuning")
        self.assertTrue(audit[0]["ts"].startswith("2026-08-01"))
        # active signal reflects the new scope weight: base*decay*fit*0.80
        active = conn.execute(
            "SELECT score, score_scope_fit FROM signals "
            "WHERE signal_id='s_active'").fetchone()
        self.assertEqual(active["score_scope_fit"], 0.80)

    def test_validation_rejects_bad_and_unknown(self):
        conn = fixture_conn()
        for bad in (-0.1, float("inf"), float("nan"), "abc", None):
            with self.assertRaises(ValueError):
                data.update_weight(conn, "scope", "sector", bad, now=NOW)
        with self.assertRaises(ValueError):
            data.update_weight(conn, "scope", "NOPE", 1.0, now=NOW)
        # no write happened
        self.assertEqual(conn.execute(
            "SELECT weight FROM scoring_weights WHERE key='sector'"
            ).fetchone()["weight"], 0.55)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM config_audit").fetchone()["c"], 0)


class TestNoOpSaves(unittest.TestCase):
    """A save whose new value equals the current one writes no provenance row
    and does not rescore (persona-pass trust finding: the audit trail records
    real changes, not button presses)."""

    def test_weight_noop_writes_nothing_and_skips_rescore(self):
        conn = fixture_conn()
        before = conn.execute(
            "SELECT scored_at FROM signals WHERE signal_id='s_active'").fetchone()
        r = data.update_weight(conn, "scope", "sector", 0.55, now=NOW)  # == seed
        self.assertFalse(r["changed"])
        self.assertNotIn("scored", r)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM config_audit").fetchone()["c"], 0)
        after = conn.execute(
            "SELECT scored_at FROM signals WHERE signal_id='s_active'").fetchone()
        self.assertEqual(after["scored_at"], before["scored_at"])   # no rescore

    def test_half_life_noop_writes_nothing(self):
        conn = fixture_conn()
        r = data.update_half_life(conn, "t_lead", 90, now=NOW)       # == seed
        self.assertFalse(r["changed"])
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM config_audit").fetchone()["c"], 0)

    def test_source_noop_writes_nothing(self):
        conn = fixture_conn()
        r = data.set_source_enabled(conn, "edgar", True, now=NOW)    # already on
        self.assertFalse(r["changed"])
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM config_audit").fetchone()["c"], 0)


class TestFrozenScoreContract(unittest.TestCase):
    def test_weight_edit_leaves_frozen_rows_untouched(self):
        conn = fixture_conn()
        before = conn.execute(
            "SELECT score, score_base, score_decay, score_account_fit, "
            " score_scope_fit FROM signals WHERE signal_id='s_frozen'").fetchone()
        data.update_weight(conn, "scope", "sector", 0.90, now=NOW)
        after = conn.execute(
            "SELECT score, score_base, score_decay, score_account_fit, "
            " score_scope_fit, status FROM signals "
            "WHERE signal_id='s_frozen'").fetchone()
        # decayed row: score + all four components unchanged, still multiply out
        self.assertEqual(after["status"], "decayed")
        self.assertEqual(after["score"], before["score"])
        self.assertEqual(after["score_scope_fit"], before["score_scope_fit"])
        prod = (after["score_base"] * after["score_decay"]
                * after["score_account_fit"] * after["score_scope_fit"])
        self.assertAlmostEqual(prod, after["score"], places=6)
        # active row components still multiply to its (new) score
        act = conn.execute(
            "SELECT score, score_base, score_decay, score_account_fit, "
            " score_scope_fit FROM signals WHERE signal_id='s_active'").fetchone()
        self.assertAlmostEqual(
            act["score_base"] * act["score_decay"] * act["score_account_fit"]
            * act["score_scope_fit"], act["score"], places=6)


class TestRescoreFailureRollback(unittest.TestCase):
    def test_weight_edit_rolls_back_if_rescore_fails(self):
        conn = fixture_conn()
        original_rescore = data.rescore

        def boom(conn, now=None):
            raise RuntimeError("rescore failed")

        data.rescore = boom
        try:
            with self.assertRaises(RuntimeError):
                data.update_weight(conn, "scope", "sector", 0.90, now=NOW)
        finally:
            data.rescore = original_rescore

        self.assertEqual(conn.execute(
            "SELECT weight FROM scoring_weights WHERE weight_kind='scope' "
            "AND key='sector'").fetchone()["weight"], 0.55)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM config_audit").fetchone()["c"], 0)

    def test_half_life_edit_rolls_back_if_rescore_fails(self):
        conn = fixture_conn()
        original_rescore = data.rescore

        def boom(conn, now=None):
            raise RuntimeError("rescore failed")

        data.rescore = boom
        try:
            with self.assertRaises(RuntimeError):
                data.update_half_life(conn, "t_lead", 120, now=NOW)
        finally:
            data.rescore = original_rescore

        self.assertEqual(conn.execute(
            "SELECT decay_half_life_days FROM triggers WHERE trigger_id='t_lead'"
            ).fetchone()["decay_half_life_days"], 90)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM config_audit").fetchone()["c"], 0)


class TestUpdateTuningBatch(unittest.TestCase):
    def test_unknown_key_rejects_whole_batch_before_any_write(self):
        conn = fixture_conn()
        with self.assertRaises(ValueError):
            data.update_tuning(
                conn,
                [("scope", "sector", 0.90), ("scope", "missing", 1.1)],
                [("t_lead", 120)],
                reason="batch",
                now=NOW)

        self.assertEqual(conn.execute(
            "SELECT weight FROM scoring_weights WHERE weight_kind='scope' "
            "AND key='sector'").fetchone()["weight"], 0.55)
        self.assertEqual(conn.execute(
            "SELECT decay_half_life_days FROM triggers WHERE trigger_id='t_lead'"
            ).fetchone()["decay_half_life_days"], 90)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM config_audit").fetchone()["c"], 0)

    def test_rescore_failure_rolls_back_whole_batch(self):
        conn = fixture_conn()
        original_rescore = data.rescore

        def boom(conn, now=None):
            raise RuntimeError("rescore failed")

        data.rescore = boom
        try:
            with self.assertRaises(RuntimeError):
                data.update_tuning(
                    conn,
                    [("scope", "sector", 0.90)],
                    [("t_lead", 120)],
                    reason="batch",
                    now=NOW)
        finally:
            data.rescore = original_rescore

        self.assertEqual(conn.execute(
            "SELECT weight FROM scoring_weights WHERE weight_kind='scope' "
            "AND key='sector'").fetchone()["weight"], 0.55)
        self.assertEqual(conn.execute(
            "SELECT decay_half_life_days FROM triggers WHERE trigger_id='t_lead'"
            ).fetchone()["decay_half_life_days"], 90)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM config_audit").fetchone()["c"], 0)


class TestUpdateHalfLife(unittest.TestCase):
    def test_happy(self):
        conn = fixture_conn()
        result = data.update_half_life(conn, "t_lead", 120, reason="recal", now=NOW)
        self.assertEqual(result["old"], 90)
        self.assertEqual(result["new"], 120)
        self.assertEqual(conn.execute(
            "SELECT decay_half_life_days FROM triggers WHERE trigger_id='t_lead'"
            ).fetchone()["decay_half_life_days"], 120)
        self.assertEqual(conn.execute(
            "SELECT field FROM config_audit").fetchone()["field"],
            "decay_half_life_days")

    def test_validation(self):
        conn = fixture_conn()
        for bad in (0, -5, 90.5, True, "x", None):
            with self.assertRaises(ValueError):
                data.update_half_life(conn, "t_lead", bad, now=NOW)
        with self.assertRaises(ValueError):
            data.update_half_life(conn, "t_missing", 100, now=NOW)
        self.assertEqual(conn.execute(
            "SELECT decay_half_life_days FROM triggers WHERE trigger_id='t_lead'"
            ).fetchone()["decay_half_life_days"], 90)

    def test_whole_number_float_accepted(self):
        conn = fixture_conn()
        result = data.update_half_life(conn, "t_lead", 120.0, now=NOW)
        self.assertEqual(result["new"], 120)


class TestSetSourceEnabled(unittest.TestCase):
    def test_toggle_audits_no_rescore(self):
        conn = fixture_conn()
        before_scored_at = conn.execute(
            "SELECT scored_at FROM signals WHERE signal_id='s_active'").fetchone()
        result = data.set_source_enabled(conn, "edgar", False,
                                         reason="G2 low", now=NOW)
        self.assertEqual(result["old"], 1)
        self.assertEqual(result["new"], 0)
        self.assertEqual(conn.execute(
            "SELECT enabled FROM source_policies WHERE source_id='edgar'"
            ).fetchone()["enabled"], 0)
        self.assertEqual(conn.execute(
            "SELECT field FROM config_audit").fetchone()["field"], "enabled")
        # no rescore ran: active signal's scored_at is untouched
        after_scored_at = conn.execute(
            "SELECT scored_at FROM signals WHERE signal_id='s_active'").fetchone()
        self.assertEqual(after_scored_at["scored_at"], before_scored_at["scored_at"])

    def test_unknown_source(self):
        conn = fixture_conn()
        with self.assertRaises(ValueError):
            data.set_source_enabled(conn, "nope", True, now=NOW)


class _RecordingConn:
    """Wraps a sqlite3 connection and records every executed SQL statement, so a
    test can assert on the statements a helper runs (not just their effect)."""

    def __init__(self, conn):
        self._conn = conn
        self.statements = []

    def execute(self, sql, *args):
        self.statements.append(sql)
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestNeverDelete(unittest.TestCase):
    """Regression gate (b): config-write helpers only UPDATE + INSERT-audit,
    never DELETE - so nothing FK-referencing a config row can be orphaned. This
    slice adds no reset/regenerate path; when one lands, delete-safety with a
    referencing row present becomes the live gate."""

    def test_helpers_execute_no_delete_statements(self):
        conn = fixture_conn()
        rec = _RecordingConn(conn)
        data.update_weight(rec, "scope", "sector", 0.7, now=NOW)
        data.update_half_life(rec, "t_lead", 100, now=NOW)
        data.set_source_enabled(rec, "edgar", False, now=NOW)
        # rescore() runs on the wrapped conn too; capture everything it issued.
        for sql in rec.statements:
            self.assertFalse(sql.lstrip().upper().startswith("DELETE"),
                             f"config write issued a DELETE: {sql!r}")
        # and a real DELETE would have been recorded if present
        self.assertTrue(any(s.lstrip().upper().startswith("UPDATE")
                            for s in rec.statements))


class TestConfigReads(unittest.TestCase):
    def test_audit_tail_newest_first(self):
        conn = fixture_conn()
        data.update_weight(conn, "scope", "sector", 0.7, now=NOW)
        data.update_half_life(conn, "t_lead", 100, now=NOW)
        tail = data.config_audit_tail(conn)
        self.assertEqual(len(tail), 2)
        self.assertEqual(tail[0]["field"], "decay_half_life_days")  # newest
        self.assertEqual(tail[1]["field"], "weight")

    def test_source_policy_rows_carry_state_and_g2(self):
        conn = fixture_conn()
        rows = data.source_policy_rows(conn, now=NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_id"], "edgar")
        self.assertIn("state", rows[0])
        self.assertIn("g2", rows[0])          # None (no rated feedback) is fine


# -- R3.2 bounded backoff on the sanctioned feedback / review writes ----------

LOCKED = "database is locked"


class _BusyConn:
    """Connection stand-in whose first ``fail_times`` commits raise SQLite's
    'database is locked'. Records statements, commits and rollbacks so a test
    can assert a retry never half-applies a multi-statement write."""

    def __init__(self, fail_times, message=LOCKED):
        self.remaining = fail_times
        self.message = message
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, *args):
        self.executed.append(sql)

    def commit(self):
        if self.remaining > 0:
            self.remaining -= 1
            raise sqlite3.OperationalError(self.message)
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _FlakyConn:
    """Wraps a REAL connection and makes its first ``fail_times`` commits raise
    'database is locked' (without committing), so an end-to-end test can prove
    the row lands exactly once after a retry."""

    def __init__(self, conn, fail_times):
        self._conn = conn
        self.remaining = fail_times

    def commit(self):
        if self.remaining > 0:
            self.remaining -= 1
            raise sqlite3.OperationalError(LOCKED)
        self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _Sleeper:
    """Injected stand-in for time.sleep: records the waits instead of taking
    them, so the retry schedule is asserted without spending 650ms."""

    def __init__(self):
        self.waits = []

    def __call__(self, seconds):
        self.waits.append(seconds)


def triage_fixture_conn():
    """fixture_conn plus the rows triage_decision needs (R8.2)."""
    conn = fixture_conn()
    conn.execute("INSERT INTO watchlist_entities (entity_id, name, active) "
                 "VALUES ('E_DARK','Dark Muni Co',1)")
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, payload, "
        " url) VALUES ('re_rev','edgar','2026-08-01','{}','http://x/1')")
    conn.execute(
        "INSERT INTO review_queue (raw_event_id, candidate_entity_id, reason, "
        " confidence, created_at, disposition) VALUES "
        "('re_rev','E_DARK','fuzzy_below_threshold',0.82,'2026-08-01','pending')")
    conn.commit()
    return conn


class TestWriteBackoff(unittest.TestCase):
    """R3.2: feedback and review-queue writes are the two operator writes that
    do NOT take the ingestion lock, so a concurrent run used to surface a raw
    'database is locked'. They now retry with bounded exponential backoff and
    fail with a named, legible error once the waits are spent."""

    def test_locked_twice_then_success_writes_silently(self):
        conn, sleeper = _BusyConn(fail_times=2), _Sleeper()
        data._commit_with_backoff(conn, lambda: conn.execute("INSERT 1"),
                                  "your feedback", sleeper)
        self.assertEqual(conn.commits, 1)
        self.assertEqual(sleeper.waits, [0.05, 0.15])

    def test_success_path_never_sleeps(self):
        conn, sleeper = _BusyConn(fail_times=0), _Sleeper()
        data._commit_with_backoff(conn, lambda: conn.execute("INSERT 1"),
                                  "your feedback", sleeper)
        self.assertEqual(sleeper.waits, [])
        self.assertEqual(conn.rollbacks, 0)

    def test_locked_four_times_raises_a_named_error_not_a_traceback(self):
        conn, sleeper = _BusyConn(fail_times=4), _Sleeper()
        with self.assertRaises(data.WriteBusyError) as ctx:
            data._commit_with_backoff(conn, lambda: conn.execute("INSERT 1"),
                                      "your feedback", sleeper)
        message = str(ctx.exception)
        self.assertIn("your feedback", message)
        self.assertIn("nothing was saved", message)
        self.assertIn("try again", message)
        # bounded: exactly the configured waits, then stop
        self.assertEqual(sleeper.waits, list(data.WRITE_RETRY_DELAYS_S))
        self.assertEqual(conn.commits, 0)
        # the raw sqlite error is preserved as the cause, not swallowed
        self.assertIsInstance(ctx.exception.__cause__, sqlite3.OperationalError)

    def test_every_failed_attempt_rolls_back_first(self):
        # triage_decision writes two statements; a retry must not half-apply.
        conn, sleeper = _BusyConn(fail_times=2), _Sleeper()
        data._commit_with_backoff(conn, lambda: conn.execute("INSERT 1"),
                                  "this triage decision", sleeper)
        self.assertEqual(conn.rollbacks, 2)

    def test_a_real_sql_error_is_not_retried(self):
        # not a lock: a genuine SQL fault must surface immediately, unwrapped.
        conn = _BusyConn(fail_times=1, message="no such table: feedback")
        sleeper = _Sleeper()
        with self.assertRaises(sqlite3.OperationalError) as ctx:
            data._commit_with_backoff(conn, lambda: conn.execute("INSERT 1"),
                                      "your feedback", sleeper)
        self.assertNotIsInstance(ctx.exception, data.WriteBusyError)
        self.assertEqual(sleeper.waits, [])

    def test_record_feedback_lands_exactly_once_after_retries(self):
        conn = fixture_conn()
        sleeper = _Sleeper()
        data.record_feedback(_FlakyConn(conn, fail_times=2), "s_active",
                             "useful", now=NOW, sleep=sleeper)
        rows = conn.execute(
            "SELECT signal_id, verdict FROM feedback").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "useful")
        self.assertEqual(sleeper.waits, [0.05, 0.15])

    def test_triage_decision_lands_exactly_once_after_retries(self):
        conn = triage_fixture_conn()
        data.triage_decision(_FlakyConn(conn, fail_times=2), "re_rev", "E_DARK",
                             accept=True, now=NOW, sleep=_Sleeper())
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM entity_match_decisions "
            "WHERE raw_event_id='re_rev'").fetchone()[0], 1)
        self.assertEqual(conn.execute(
            "SELECT disposition FROM review_queue WHERE raw_event_id='re_rev' "
            "AND candidate_entity_id='E_DARK'").fetchone()[0], "accepted")

    def test_validation_still_rejects_before_any_retry(self):
        # R9.1 is unchanged: a bad verdict never reaches the retry loop.
        conn, sleeper = _BusyConn(fail_times=4), _Sleeper()
        with self.assertRaises(ValueError):
            data.record_feedback(conn, "s_active", "bogus", now=NOW,
                                 sleep=sleeper)
        self.assertEqual(conn.executed, [])
        self.assertEqual(sleeper.waits, [])


if __name__ == "__main__":
    unittest.main()
