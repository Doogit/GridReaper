"""Tests for the monthly precision job (R9.3): python -m app.audit.precision.

Hermetic: real migrations against SQLite (in-memory for the shaping tests, a
throwaway temp file for the end-to-end main() run via the GRIDSIGNALS_DB
override), FK enforcement on, no network. The report is read through
``app.ui.data.precision_report`` — the same composition the Precision page uses
— so these tests fail if the job ever grows a second, drifting one.
"""
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from app.audit import precision
from app.db.migrate import apply_migrations
from app.ui import data

NOW = "2026-08-16T00:00:00Z"


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO source_policies (source_id, name, evidence_rank) "
        "VALUES ('src_a', 'Source A', 1)")
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        " decay_half_life_days) "
        "VALUES ('leadership_change', 'Leadership change', 3, 90)")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name) "
        "VALUES ('E_ACME', 'Acme Energy')")
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id) VALUES ('re1', 'src_a')")
    for n in range(3):
        conn.execute(
            "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
            " signal_scope, trigger_id, status) "
            "VALUES (?, 're1', 'E_ACME', 'account', 'leadership_change', "
            " 'active')", (f"s{n}",))
    # 2 useful / 1 not_useful -> 66.7% over n=3.
    for n, verdict in enumerate(("useful", "useful", "not_useful")):
        conn.execute(
            "INSERT INTO feedback (signal_id, verdict, reason_code, ts) "
            "VALUES (?, ?, ?, ?)",
            (f"s{n}", verdict, "other" if verdict == "not_useful" else None,
             "2026-08-10T00:00:00Z"))
    # 2 pass / 1 fail on an auto-accuracy check -> 66.7% over n=3.
    for n, result in enumerate(("pass", "pass", "fail")):
        conn.execute(
            "INSERT INTO audit (signal_id, check_type, result, ts) "
            "VALUES (?, 'entity_match', ?, '2026-08-11T00:00:00Z')",
            (f"s{n}", result))
    conn.commit()
    return conn


class FormatReportTests(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()
        self.lines = precision.format_report(
            data.precision_report(self.conn, now=NOW))

    def tearDown(self):
        self.conn.close()

    def test_summary_line_follows_the_producer_convention(self):
        self.assertTrue(self.lines[0].startswith("precision: success "))

    def test_every_rate_ships_with_its_denominator(self):
        # The module's trust invariant, carried into the log: no line may show a
        # rate without the n it was computed over.
        for line in self.lines:
            self.assertEqual(
                line.count("n="), line.count("useful=") + line.count("auto="),
                f"a rate is printed without its n: {line}")

    def test_reports_the_three_r93_dimensions(self):
        dimensions = {line.split()[1].split("=")[0]
                      for line in self.lines[1:]}
        self.assertEqual(dimensions, set(precision.REPORT_DIMENSIONS))
        self.assertIn("precision trigger=leadership_change useful=66.7% n=3 "
                      "auto=66.7% n=3", self.lines)
        self.assertIn("precision source=src_a useful=66.7% n=3 "
                      "auto=66.7% n=3", self.lines)
        self.assertIn("precision signal_scope=account useful=66.7% n=3 "
                      "auto=66.7% n=3", self.lines)

    def test_gate_states_are_reported_not_recomputed(self):
        # The waived G1 state and the G2 demote count come straight off the
        # shared report, so the log can never say something the page does not.
        report = data.precision_report(self.conn, now=NOW)
        self.assertIn(f"g1={report['g1']['state']}", self.lines[0])
        self.assertIn("g2_demote_recommended=0/1", self.lines[0])

    def test_empty_store_reports_n_a_over_a_real_zero_denominator(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_migrations(conn)
        try:
            lines = precision.format_report(data.precision_report(conn, now=NOW))
        finally:
            conn.close()
        # Never a fake 0.0: an empty denominator reads "n/a", beside its n=0.
        self.assertIn("useful=n/a n=0", lines[0])
        self.assertIn("auto=n/a n=0", lines[0])
        self.assertEqual(len(lines), 1)   # no buckets to report


class MainTests(unittest.TestCase):
    def test_materialize_runs_against_a_throwaway_db(self):
        saved = os.environ.get("GRIDSIGNALS_DB")
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            apply_migrations(conn)
            conn.close()
            os.environ["GRIDSIGNALS_DB"] = path
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = precision.main(["--materialize"])
        finally:
            if saved is None:
                os.environ.pop("GRIDSIGNALS_DB", None)
            else:
                os.environ["GRIDSIGNALS_DB"] = saved
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)
        self.assertEqual(code, 0)
        self.assertTrue(buf.getvalue().startswith("precision: success "))

    def test_bare_invocation_does_nothing_rather_than_guessing(self):
        # The crontab spells the entry point out; a bare `python -m
        # app.audit.precision` must not silently pick an action.
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                precision.main([])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
