"""Precomputed aggregate refresh tests (R8.10).

Hermetic: real migrations (including 0012) against in-memory SQLite, FK on, no
network. Covers the 0012 schema, the refresh's replace-whole semantics, the
as-of stamp, and the basis fingerprint that makes staleness detectable — the
reader's half of that contract lives in tests/test_ui_data.py.
"""
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from app import aggregates
from app.db.migrate import apply_migrations

NOW = datetime(2026, 8, 15, 3, 0, 0, tzinfo=timezone.utc)


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_lead','Leadership',4,90)")
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_reg','Regulatory',5,600)")
    return conn


def add_signal(conn, signal_id, trigger_id, scope, status="active", tier=None):
    conn.execute(
        "INSERT INTO signals (signal_id, trigger_id, signal_scope, event_date, "
        " status, incident_evidence_level) VALUES (?,?,?,?,?,?)",
        (signal_id, trigger_id, scope, "2026-08-01", status, tier))


class AggregateSchemaTest(unittest.TestCase):
    """0012 applies cleanly on top of 0011 and gives the shape the CLI writes."""

    def test_0012_creates_both_tables_and_the_read_index(self):
        conn = fixture_conn()
        applied = conn.execute(
            "SELECT version FROM schema_migrations "
            "WHERE version = '0012_aggregates'").fetchone()
        self.assertIsNotNone(applied)
        self.assertEqual(
            {r["name"] for r in conn.execute(
                "PRAGMA table_info(aggregate_state)")},
            {"aggregate_name", "computed_at", "status_filter", "basis",
             "refresh_version"})
        self.assertEqual(
            {r["name"] for r in conn.execute(
                "PRAGMA table_info(signal_aggregates)")},
            {"aggregate_name", "dimension", "key", "label", "count"})
        self.assertIn("idx_signal_aggregates_name",
                      {r["name"] for r in conn.execute(
                          "PRAGMA index_list(signal_aggregates)")})

    def test_reapply_is_noop(self):
        conn = fixture_conn()
        self.assertEqual(apply_migrations(conn), [])

    def test_count_row_requires_a_state_row(self):
        # FK: a count row can never exist without the as-of stamp that
        # describes it, so the reader cannot find numbers with no provenance.
        conn = fixture_conn()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO signal_aggregates (aggregate_name, dimension, "
                " key, label, count) VALUES ('orphan','scope','account','a',1)")


class RefreshTest(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()
        add_signal(self.conn, "S1", "t_lead", "account", tier="confirmed")
        add_signal(self.conn, "S2", "t_lead", "sector")
        add_signal(self.conn, "S3", "t_reg", "sector")
        add_signal(self.conn, "S4", "t_reg", "sector", status="retracted")

    def test_refresh_materializes_exactly_the_live_computation(self):
        aggregates.refresh(self.conn, now=NOW)
        stored = {}
        for r in self.conn.execute(
                "SELECT dimension, key, label, count FROM signal_aggregates "
                "WHERE aggregate_name = ? ORDER BY dimension, count DESC, key",
                (aggregates.ANALYTICS_COUNTS,)):
            stored.setdefault(r["dimension"], []).append(
                {"key": r["key"], "label": r["label"], "count": r["count"]})
        self.assertEqual(stored, aggregates.compute_counts(self.conn))

    def test_refresh_counts_only_the_status_filter(self):
        # S4 is retracted; the active-only default must not count it.
        aggregates.refresh(self.conn, now=NOW)
        counts = {r["key"]: r["count"] for r in self.conn.execute(
            "SELECT key, count FROM signal_aggregates WHERE dimension='scope'")}
        self.assertEqual(counts, {"account": 1, "sector": 2})

    def test_refresh_stamps_as_of_status_filter_and_version(self):
        aggregates.refresh(self.conn, now=NOW)
        state = self.conn.execute(
            "SELECT * FROM aggregate_state WHERE aggregate_name = ?",
            (aggregates.ANALYTICS_COUNTS,)).fetchone()
        self.assertEqual(state["computed_at"], NOW.isoformat())
        self.assertTrue(state["computed_at"].endswith("+00:00"))
        self.assertEqual(json.loads(state["status_filter"]), ["active"])
        self.assertEqual(state["refresh_version"], aggregates.REFRESH_VERSION)

    def test_refresh_replaces_rather_than_accumulates(self):
        first = aggregates.refresh(self.conn, now=NOW)
        self.conn.execute("UPDATE signals SET status='retracted' "
                          "WHERE signal_id IN ('S2','S3')")
        second = aggregates.refresh(self.conn, now=NOW + timedelta(days=1))
        self.assertLess(second["rows"], first["rows"])
        rows = self.conn.execute(
            "SELECT COUNT(*) AS n FROM signal_aggregates").fetchone()["n"]
        self.assertEqual(rows, second["rows"])
        scopes = {r["key"] for r in self.conn.execute(
            "SELECT key FROM signal_aggregates WHERE dimension='scope'")}
        self.assertEqual(scopes, {"account"})

    def test_refresh_on_an_empty_store_records_a_stamp_with_no_rows(self):
        # "Computed and genuinely empty" must be distinguishable from "never
        # computed" — that distinction is the whole reason for two tables.
        conn = fixture_conn()
        result = aggregates.refresh(conn, now=NOW)
        self.assertEqual(result["rows"], 0)
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM aggregate_state WHERE aggregate_name = ?",
            (aggregates.ANALYTICS_COUNTS,)).fetchone())


class BasisFingerprintTest(unittest.TestCase):
    """The fingerprint has to move on every mutation that moves these counts."""

    def setUp(self):
        self.conn = fixture_conn()
        add_signal(self.conn, "S1", "t_lead", "account", tier="confirmed")
        self.before = aggregates.basis_fingerprint(self.conn)

    def test_new_signal_moves_the_fingerprint(self):
        add_signal(self.conn, "S2", "t_reg", "sector")
        self.assertNotEqual(aggregates.basis_fingerprint(self.conn),
                            self.before)

    def test_status_flip_moves_the_fingerprint(self):
        # The retraction case: the row count is unchanged, so a naive
        # COUNT(*) basis would miss it entirely.
        self.conn.execute(
            "UPDATE signals SET status='retracted' WHERE signal_id='S1'")
        self.assertNotEqual(aggregates.basis_fingerprint(self.conn),
                            self.before)

    def test_operator_retier_moves_the_fingerprint(self):
        # An R8.7 re-tier changes neither the row count nor any status, but it
        # does change the incident_tier bucket — the tier-edit high-water mark
        # is what catches it.
        self.conn.execute(
            "UPDATE signals SET incident_evidence_level='corroborated' "
            "WHERE signal_id='S1'")
        self.conn.execute(
            "INSERT INTO incident_tier_edits (signal_id, old_level, "
            " new_level, old_cfa, new_cfa, ts) VALUES "
            "('S1','confirmed','corroborated',1,0,?)", (NOW.isoformat(),))
        self.assertNotEqual(aggregates.basis_fingerprint(self.conn),
                            self.before)

    def test_rescore_does_not_move_the_fingerprint(self):
        # A score edit changes none of these counts, so it must not invalidate
        # the aggregate — otherwise every rescore forces a needless recompute.
        self.conn.execute(
            "UPDATE signals SET score=9.5, scored_at=? WHERE signal_id='S1'",
            (NOW.isoformat(),))
        self.assertEqual(aggregates.basis_fingerprint(self.conn), self.before)

    def test_fingerprint_is_deterministic(self):
        self.assertEqual(aggregates.basis_fingerprint(self.conn), self.before)


class StatusKeyTest(unittest.TestCase):
    def test_status_key_is_order_and_duplicate_insensitive(self):
        self.assertEqual(aggregates.status_key(("active", "decayed")),
                         aggregates.status_key(["decayed", "active", "active"]))
        self.assertNotEqual(aggregates.status_key(("active",)),
                            aggregates.status_key(("active", "decayed")))


if __name__ == "__main__":
    unittest.main()
