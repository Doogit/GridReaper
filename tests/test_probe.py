"""Tests for app.probe (internal diagnostic; KTD1 - not a PRD MUST).

Hermetic in-memory SQLite: real migrations applied, FK enforcement on, no
network. Mirrors the fixture pattern in tests/test_resolve.py.
"""
import io
import sqlite3
import unittest
from contextlib import redirect_stdout

from app.db.migrate import apply_migrations
from app.probe import run_probe


def empty_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    return conn


def seeded_conn():
    conn = empty_conn()
    conn.execute(
        "INSERT INTO source_policies (source_id, name) VALUES (?, ?)",
        ("sec_edgar_submissions", "SEC EDGAR submissions API"),
    )
    conn.execute(
        "INSERT INTO source_policies (source_id, name) VALUES (?, ?)",
        ("gdelt", "GDELT DOC API"),
    )
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name) VALUES (?, ?)",
        ("E0001", "Test Utility"),
    )
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        "decay_half_life_days) VALUES (?, ?, ?, ?)",
        ("T1", "Trigger One", 5, 30),
    )
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        "decay_half_life_days) VALUES (?, ?, ?, ?)",
        ("T2", "Trigger Two", 5, 30),
    )
    # raw_events across 2 sources
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date) "
        "VALUES (?, ?, ?)",
        ("raw-1", "sec_edgar_submissions", "2026-08-01"),
    )
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date) "
        "VALUES (?, ?, ?)",
        ("raw-2", "sec_edgar_submissions", "2026-08-05"),
    )
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date) "
        "VALUES (?, ?, ?)",
        ("raw-3", "gdelt", "2026-07-20"),
    )
    # 2 signals, 2 distinct triggers, same entity -> co-occurrence
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
        "signal_scope, trigger_id, event_date) VALUES (?, ?, ?, ?, ?, ?)",
        ("sig-1", "raw-1", "E0001", "account", "T1", "2026-08-01"),
    )
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
        "signal_scope, trigger_id, event_date) VALUES (?, ?, ?, ?, ?, ?)",
        ("sig-2", "raw-2", "E0001", "account", "T2", "2026-08-05"),
    )
    # 1 tier edit
    conn.execute(
        "INSERT INTO incident_tier_edits (signal_id, old_level, new_level, "
        "old_cfa, new_cfa, ts) VALUES (?, ?, ?, ?, ?, ?)",
        ("sig-1", "unconfirmed_early_warning", "confirmed", 0, 1,
         "2026-08-10T00:00:00+00:00"),
    )
    conn.commit()
    return conn


def run_and_capture(conn, **kwargs):
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_probe(conn, **kwargs)
    return buf.getvalue()


class TestProbeSeeded(unittest.TestCase):
    def setUp(self):
        self.conn = seeded_conn()

    def tearDown(self):
        self.conn.close()

    def test_raw_events_per_source_and_freshness(self):
        out = run_and_capture(self.conn)
        self.assertIn("sec_edgar_submissions", out)
        self.assertIn("count=2", out)
        self.assertIn("freshest=2026-08-05", out)
        self.assertIn("gdelt", out)

    def test_signals_grouped_by_trigger_scope_tier(self):
        out = run_and_capture(self.conn)
        self.assertIn("trigger=T1", out)
        self.assertIn("trigger=T2", out)
        self.assertIn("scope=account", out)

    def test_cooccurrence_lists_entity_with_two_triggers(self):
        out = run_and_capture(self.conn)
        self.assertIn("E0001", out)
        self.assertIn("distinct_triggers=2", out)

    def test_gdelt_window_age_reported(self):
        out = run_and_capture(self.conn)
        self.assertIn("GDELT window age", out)
        self.assertIn("oldest=2026-07-20", out)
        self.assertIn("newest=2026-07-20", out)

    def test_review_queue_empty_reads_none(self):
        out = run_and_capture(self.conn)
        section = out.split("review_queue pending depth --")[1].split("--")[0]
        self.assertIn("none", section)

    def test_tier_edits_count(self):
        out = run_and_capture(self.conn)
        section = out.split("incident_tier_edits count")[1]
        self.assertIn("total=1", section)

    def test_source_filter_restricts_sections(self):
        out = run_and_capture(self.conn, source_filter="gdelt")
        self.assertIn("(filtered to source_id=gdelt)", out)
        self.assertNotIn("sec_edgar_submissions", out)

    def test_exit_code_zero_via_main(self):
        # Hermetic: point main() at a throwaway on-disk DB via the same
        # GRIDSIGNALS_DB override app/db/connection.get_connection already
        # honors, so no real data/gridsignals.db is touched.
        import os
        import sys
        import tempfile

        from app import probe as probe_mod

        argv = sys.argv
        env_db = os.environ.get("GRIDSIGNALS_DB")
        fd, tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            tmp_conn = sqlite3.connect(tmp_path)
            apply_migrations(tmp_conn)
            tmp_conn.close()

            sys.argv = ["probe"]
            os.environ["GRIDSIGNALS_DB"] = tmp_path
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = probe_mod.main()
        finally:
            sys.argv = argv
            if env_db is None:
                os.environ.pop("GRIDSIGNALS_DB", None)
            else:
                os.environ["GRIDSIGNALS_DB"] = env_db
            os.remove(tmp_path)
        self.assertEqual(code, 0)


class TestProbeEmpty(unittest.TestCase):
    def setUp(self):
        self.conn = empty_conn()

    def tearDown(self):
        self.conn.close()

    def test_every_section_reads_none_or_zero(self):
        out = run_and_capture(self.conn)
        self.assertIn("none", out)  # raw_events / signals / co-occurrence / gdelt

    def test_run_probe_does_not_raise(self):
        # Should complete without exception on a fully empty store.
        run_and_capture(self.conn)


if __name__ == "__main__":
    unittest.main()
