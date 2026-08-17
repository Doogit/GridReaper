"""License re-verification sweep tests (R10.7): python -m app.reverify.

Hermetic: real migrations against SQLite (in-memory for the sweep, a throwaway
temp file for the end-to-end main() run via the GRIDSIGNALS_DB override), FK
enforcement on, no network.

The load-bearing property is the SHARED notion of "due": the sweep reads
``app.ui.data.stale_facts``, the same call the Admin banner makes, so the
scheduled job and the page can never disagree about which facts are stale.
"""
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone

from app import reverify
from app.db.migrate import apply_migrations
from app.ui import data

NOW = datetime(2026, 8, 16, tzinfo=timezone.utc)


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO products (product_id, name) VALUES ('purview', 'Purview')")
    for fact_id, verified_date in (
            ("f_fresh", "2026-07-17"),    # 30d old -> not due
            ("f_old", "2025-01-01"),      # ~592d old -> due
            ("f_unknown", ""),            # unverifiable -> due, age unknown
    ):
        conn.execute(
            "INSERT INTO license_facts (fact_id, product_id, verified_date, "
            " source_quality) VALUES (?, 'purview', ?, 'primary')",
            (fact_id, verified_date))
    conn.commit()
    return conn


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def test_due_set_is_exactly_the_admin_banner_list(self):
        # Not "equivalent to" — the same call. A second staleness rule here is
        # the drift this test exists to prevent.
        result = reverify.sweep(self.conn, now=NOW)
        self.assertEqual([f["fact_id"] for f in result["due"]],
                         [f["fact_id"] for f in data.stale_facts(self.conn, now=NOW)])
        self.assertEqual([f["fact_id"] for f in result["due"]],
                         ["f_old", "f_unknown"])

    def test_due_count_ships_with_its_denominator(self):
        result = reverify.sweep(self.conn, now=NOW)
        self.assertEqual(result["facts_total"], 3)
        self.assertEqual(result["as_of"], "2026-08-16T00:00:00+00:00")

    def test_format_names_each_due_fact_and_its_age(self):
        lines = reverify.format_sweep(reverify.sweep(self.conn, now=NOW))
        self.assertEqual(
            lines[0],
            "reverify: success due=2 facts=3 as_of=2026-08-16T00:00:00+00:00")
        self.assertIn("fact_id=f_old", lines[1])
        self.assertIn("age_days=592", lines[1])
        # An unparseable verified_date is due with an HONEST unknown age — never
        # a fabricated one, and never quietly dropped from the list.
        self.assertIn("fact_id=f_unknown", lines[2])
        self.assertIn("verified_date=(none)", lines[2])
        self.assertIn("age_days=unknown", lines[2])

    def test_nothing_due_is_still_a_reported_run(self):
        self.conn.execute("DELETE FROM license_facts WHERE fact_id != 'f_fresh'")
        lines = reverify.format_sweep(reverify.sweep(self.conn, now=NOW))
        self.assertEqual(len(lines), 1)
        self.assertIn("due=0 facts=1", lines[0])

    def test_the_sweep_writes_nothing(self):
        # R10.7's task is a human walking the validation checklist. The job
        # announces the work; it must never invent a verified_date.
        before = self.conn.execute(
            "SELECT fact_id, verified_date FROM license_facts "
            "ORDER BY fact_id").fetchall()
        reverify.sweep(self.conn, now=NOW)
        after = self.conn.execute(
            "SELECT fact_id, verified_date FROM license_facts "
            "ORDER BY fact_id").fetchall()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])


class MainTests(unittest.TestCase):
    def test_main_runs_against_a_throwaway_db(self):
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
                code = reverify.main()
        finally:
            if saved is None:
                os.environ.pop("GRIDSIGNALS_DB", None)
            else:
                os.environ["GRIDSIGNALS_DB"] = saved
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)
        self.assertEqual(code, 0)
        self.assertTrue(buf.getvalue().startswith("reverify: success due=0 facts=0 "))


if __name__ == "__main__":
    unittest.main()
