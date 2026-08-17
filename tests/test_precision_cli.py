"""Tests for the monthly precision job (R9.3): python -m app.audit.precision.

Hermetic: real migrations against SQLite (in-memory for the shaping tests, a
throwaway temp file for the end-to-end main() run via the GRIDSIGNALS_DB
override), FK enforcement on, no network. The report is read through
``app.ui.data.precision_report`` — the same composition the Precision page uses
— so these tests fail if the job ever grows a second, drifting one.
"""
import io
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from unittest import mock

from app.audit import precision
from app.db.migrate import apply_migrations
from app.ui import data

NOW = "2026-08-16T00:00:00Z"
# The real cron instant: `47 15 1 * *` -> 15:47 on the 1st, ~16 hours into a
# month that has barely started.
CRON_NOW = datetime(2026, 9, 1, 15, 47, tzinfo=timezone.utc)


@contextmanager
def throwaway_db():
    """A migrated temp-file store wired to GRIDSIGNALS_DB, torn down after.

    Never data/gridsignals.db: main() opens whatever get_connection() resolves.
    """
    saved = os.environ.get("GRIDSIGNALS_DB")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
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


class PhantomSourceTests(unittest.TestCase):
    """The g2 denominator counts SOURCES, and a NULL bucket is not one."""

    def _report_lines(self, conn):
        return precision.format_report(data.precision_report(conn, now=NOW))

    def test_a_null_source_bucket_is_not_counted_as_a_source(self):
        # Sector and regulatory cards have no raw_event, so the LEFT JOIN
        # resolves source_id to NULL and g2_status buckets that NULL alongside
        # real sources. This store is dominated by such cards, so counting the
        # bucket would print "1 source assessed" when zero were.
        conn = fixture_conn()
        try:
            conn.execute(
                "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
                " signal_scope, trigger_id, status) "
                "VALUES ('s_sector', NULL, NULL, 'regulatory_calendar', "
                " 'leadership_change', 'active')")
            conn.execute(
                "INSERT INTO feedback (signal_id, verdict, reason_code, ts) "
                "VALUES ('s_sector', 'not_useful', 'other', "
                " '2026-08-10T00:00:00Z')")
            conn.commit()
            report = data.precision_report(conn, now=NOW)
            # The NULL bucket really is there in the computation...
            self.assertIn(None, report["g2"])
            # ...and really is excluded from the headline count: src_a only.
            self.assertIn("g2_demote_recommended=0/1",
                          precision.format_report(report)[0])
        finally:
            conn.close()

    def test_only_a_null_bucket_reports_zero_sources_assessed(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(conn)
        try:
            conn.execute(
                "INSERT INTO triggers (trigger_id, name, base_strength, "
                " decay_half_life_days) "
                "VALUES ('reg_cal', 'Regulatory calendar', 2, 90)")
            conn.execute(
                "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
                " signal_scope, trigger_id, status) "
                "VALUES ('s_sector', NULL, NULL, 'regulatory_calendar', "
                " 'reg_cal', 'active')")
            conn.execute(
                "INSERT INTO feedback (signal_id, verdict, reason_code, ts) "
                "VALUES ('s_sector', 'not_useful', 'other', "
                " '2026-08-10T00:00:00Z')")
            conn.commit()
            self.assertIn("g2_demote_recommended=0/0", self._report_lines(conn)[0])
        finally:
            conn.close()


class PriorMonthTests(unittest.TestCase):
    def test_prior_month_end_is_the_last_instant_of_the_month_before(self):
        end = precision.prior_month_end(CRON_NOW)
        self.assertEqual(end.year, 2026)
        self.assertEqual(end.month, 8)
        self.assertEqual(end.day, 31)
        self.assertEqual(end.tzinfo, timezone.utc)
        # Strictly inside August, strictly before September.
        self.assertLess(end, datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.assertGreater(end, datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc))

    def test_january_reports_the_previous_december(self):
        end = precision.prior_month_end(
            datetime(2027, 1, 1, 15, 47, tzinfo=timezone.utc))
        self.assertEqual((end.year, end.month, end.day), (2026, 12, 31))


class CronTimeReportTests(unittest.TestCase):
    """The scheduled run must summarize the month that ENDED (R9.3)."""

    def _august_store(self, conn):
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
            "INSERT INTO raw_events (raw_event_id, source_id) "
            "VALUES ('re1', 'src_a')")
        for n in range(12):
            sid = f"s{n}"
            conn.execute(
                "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
                " signal_scope, trigger_id, status) "
                "VALUES (?, 're1', 'E_ACME', 'account', 'leadership_change', "
                " 'active')", (sid,))
            conn.execute(
                "INSERT INTO audit (signal_id, check_type, result, ts) "
                "VALUES (?, 'entity_match', 'pass', '2026-08-11T00:00:00Z')",
                (sid,))
            conn.execute(
                "INSERT INTO feedback (signal_id, verdict, ts) "
                "VALUES (?, 'useful', '2026-08-12T00:00:00Z')", (sid,))
        conn.commit()

    def test_the_cron_instant_reports_august_not_sixteen_hours_of_september(self):
        # `47 15 1 * *` fires at 2026-09-01T15:47Z. Before this fix the record
        # windowed to the month CONTAINING that instant, so it summarized ~16
        # hours of a new month, printed spotcheck=0/5, and August's real
        # coverage was never written by any run — a permanently false line in a
        # permanent record.
        with throwaway_db() as conn:
            self._august_store(conn)
            conn.close()      # main() opens the store itself, via GRIDSIGNALS_DB
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = precision.main(["--report"], now=CRON_NOW)
        self.assertEqual(code, 0)
        summary = buf.getvalue().splitlines()[0]
        self.assertIn("spotcheck_window=2026-08", summary)
        self.assertNotIn("2026-09", summary)
        self.assertIn("spotcheck=12/5", summary)
        self.assertIn("as_of=2026-08-31T23:59:59.999999+00:00", summary)

    def test_the_live_page_still_windows_to_the_current_month(self):
        # prior_month_end is what the SCHEDULED invocation asks for; the
        # Precision page keeps asking spotcheck_coverage about "now", which is
        # right for a live view. This is the half that must NOT change.
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(conn)
        try:
            self._august_store(conn)
            live = data.precision_report(conn, now="2026-08-16T00:00:00Z")
            self.assertEqual(live["spotcheck"]["window"], "2026-08")
            self.assertEqual(live["spotcheck"]["reviewed"], 12)
        finally:
            conn.close()


class DurableReportFileTests(unittest.TestCase):
    """R9.3's monthly record needs a durable home (U27): the container's
    ``/var/log`` is not on the mounted volume, so the log line the scheduled
    job printed there never survived a restart. ``--report`` now also writes
    the same lines to a dated file beside the configured DB."""

    def _report_dir(self, db_path):
        return os.path.join(os.path.dirname(db_path), "precision-reports")

    def test_report_writes_a_dated_file_with_the_same_content_as_stdout(self):
        with throwaway_db() as conn:
            self._august_store_minimal(conn)
            db_path = os.environ["GRIDSIGNALS_DB"]
            conn.close()
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = precision.main(["--report"], now=CRON_NOW)
            self.assertEqual(code, 0)
            report_dir = self._report_dir(db_path)
            expected_file = os.path.join(report_dir, "2026-08.txt")
            self.assertTrue(os.path.exists(expected_file))
            with open(expected_file, encoding="utf-8") as fh:
                written = fh.read()
            self.assertEqual(written, buf.getvalue())
            shutil.rmtree(report_dir, ignore_errors=True)

    def test_rerunning_for_the_same_period_overwrites_not_appends(self):
        with throwaway_db() as conn:
            self._august_store_minimal(conn)
            db_path = os.environ["GRIDSIGNALS_DB"]
            conn.close()
            report_dir = self._report_dir(db_path)
            expected_file = os.path.join(report_dir, "2026-08.txt")
            with redirect_stdout(io.StringIO()):
                precision.main(["--report"], now=CRON_NOW)
                precision.main(["--report"], now=CRON_NOW)
            with open(expected_file, encoding="utf-8") as fh:
                content = fh.read()
            self.assertEqual(content.count("precision: success"), 1)
            shutil.rmtree(report_dir, ignore_errors=True)

    def test_write_path_is_derived_from_gridsignals_db_not_hardcoded(self):
        with throwaway_db() as conn:
            conn.close()
            db_path = os.environ["GRIDSIGNALS_DB"]
            path = precision.report_path("2026-08")
            self.assertEqual(
                os.path.dirname(path), self._report_dir(db_path))
            # No hardcoded container path — it tracks wherever the test
            # tempdir put the DB, not a fixed "/app/data".
            self.assertNotIn("app/data", path.replace("\\", "/"))
            shutil.rmtree(self._report_dir(db_path), ignore_errors=True)

    def test_successful_write_leaves_no_temp_file_behind(self):
        # Not a true crash-mid-write test (hard to force reliably here) — this
        # confirms the atomic temp-file-then-replace pattern
        # write_report_file() uses cleans up after itself, so a reader beside
        # the final file never finds a stray partial write (U27).
        with throwaway_db() as conn:
            self._august_store_minimal(conn)
            db_path = os.environ["GRIDSIGNALS_DB"]
            conn.close()
            with redirect_stdout(io.StringIO()):
                precision.main(["--report"], now=CRON_NOW)
            report_dir = self._report_dir(db_path)
            leftovers = [f for f in os.listdir(report_dir)
                        if f.startswith(".tmp-")]
            self.assertEqual(leftovers, [])
            shutil.rmtree(report_dir, ignore_errors=True)

    def test_filesystem_failure_degrades_gracefully_not_fatally(self):
        # --report runs unguarded/lock-free off cron; a filesystem problem
        # persisting the durability copy must not crash a run whose report
        # was already computed correctly and printed to stdout (U27).
        with throwaway_db() as conn:
            self._august_store_minimal(conn)
            conn.close()
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("app.audit.precision.tempfile.mkstemp",
                            side_effect=OSError("disk full")):
                with redirect_stdout(out), redirect_stderr(err):
                    code = precision.main(["--report"], now=CRON_NOW)
            self.assertEqual(code, 0)
            self.assertTrue(out.getvalue().startswith("precision: success "))
            self.assertIn("NOT persisted", err.getvalue())

    @staticmethod
    def _august_store_minimal(conn):
        conn.execute(
            "INSERT INTO source_policies (source_id, name, evidence_rank) "
            "VALUES ('src_a', 'Source A', 1)")
        conn.execute(
            "INSERT INTO triggers (trigger_id, name, base_strength, "
            " decay_half_life_days) "
            "VALUES ('leadership_change', 'Leadership change', 3, 90)")
        conn.commit()


class MainTests(unittest.TestCase):
    def test_report_runs_against_a_throwaway_db(self):
        with throwaway_db() as conn:
            conn.close()      # main() opens the store itself, via GRIDSIGNALS_DB
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = precision.main(["--report"])
        self.assertEqual(code, 0)
        self.assertTrue(buf.getvalue().startswith("precision: success "))

    def test_bare_invocation_does_nothing_rather_than_guessing(self):
        # The crontab spells the entry point out; a bare `python -m
        # app.audit.precision` must not silently pick an action.
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                precision.main([])
        self.assertEqual(raised.exception.code, 2)

    def test_the_retired_materialize_flag_is_gone(self):
        # The flag promised a durable write ("materialize" means a durable write
        # in this repo — see app/obligations.py) and only ever printed. It was
        # renamed, not aliased, and deploy/crontab moved with it in the same
        # commit; a surviving alias would keep the dishonest name reachable.
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                precision.main(["--materialize"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
