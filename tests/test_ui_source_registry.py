"""Source-registry helper tests (R9.5; R3.3).

Hermetic: real migrations against in-memory SQLite, FK on, fixture rows,
injected NOW. Covers add (origin='operator', one __add__ audit) + duplicate
guard, the disable-vs-remove policy (seeded refused), and the LOAD-BEARING FK
gate: remove_source must block legibly when a raw_event / run / signal
references it, never leak an IntegrityError or partially delete. The helpers
take a passed connection so DB assertions stay hermetic.
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
    # A seeded source (origin defaults to 'seed').
    conn.execute("INSERT INTO source_policies (source_id, name, enabled) "
                 "VALUES ('edgar','EDGAR',1)")
    conn.commit()
    return conn


class TestAddSource(unittest.TestCase):
    def test_add_sets_operator_origin_enabled_and_audits(self):
        conn = fixture_conn()
        res = data.add_source(conn, "myfeed", "My Feed",
                              access_method="rss", evidence_rank=2,
                              reason="new source", now=NOW)
        self.assertTrue(res["created"])
        row = conn.execute(
            "SELECT name, origin, enabled, access_method, evidence_rank "
            "FROM source_policies WHERE source_id='myfeed'").fetchone()
        self.assertEqual(row["name"], "My Feed")
        self.assertEqual(row["origin"], "operator")
        self.assertEqual(row["enabled"], 1)
        self.assertEqual(row["access_method"], "rss")
        self.assertEqual(row["evidence_rank"], 2)
        audit = conn.execute("SELECT * FROM config_audit").fetchall()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["table_name"], "source_policies")
        self.assertEqual(audit[0]["field"], "__add__")
        self.assertTrue(audit[0]["ts"].startswith("2026-08-01"))

    def test_duplicate_source_id_rejected(self):
        conn = fixture_conn()
        with self.assertRaises(ValueError):
            data.add_source(conn, "edgar", "Dup")
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM config_audit")
                         .fetchone()["n"], 0)

    def test_empty_name_rejected(self):
        conn = fixture_conn()
        with self.assertRaises(ValueError):
            data.add_source(conn, "x", "")

    def test_bad_evidence_rank_rejected(self):
        conn = fixture_conn()
        for rank in ("x", "0", "4"):
            with self.subTest(rank=rank):
                with self.assertRaises(ValueError):
                    data.add_source(conn, "myfeed", "My Feed",
                                    evidence_rank=rank)
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM source_policies WHERE source_id='myfeed'"
                    ).fetchone())


class TestRemoveSource(unittest.TestCase):
    def test_remove_seeded_source_refused(self):
        conn = fixture_conn()
        with self.assertRaises(ValueError) as ctx:
            data.remove_source(conn, "edgar")
        self.assertIn("disable it instead", str(ctx.exception).lower())
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM source_policies WHERE source_id='edgar'").fetchone())

    def test_remove_blocked_by_referencing_raw_event(self):
        conn = fixture_conn()
        data.add_source(conn, "myfeed", "My Feed")
        conn.execute("INSERT INTO raw_events (raw_event_id, source_id) "
                     "VALUES ('re1','myfeed')")
        conn.commit()
        with self.assertRaises(ValueError) as ctx:
            data.remove_source(conn, "myfeed")
        msg = str(ctx.exception)
        self.assertIn("raw_events: 1", msg)     # breakdown names the blocker
        # No partial delete; no IntegrityError leaked (ValueError only).
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM source_policies WHERE source_id='myfeed'").fetchone())

    def test_remove_blocked_by_referencing_facility_asset(self):
        conn = fixture_conn()
        data.add_source(conn, "myfeed", "My Feed")
        # facility_assets.source_id is a bare FK to source_policies(source_id);
        # only facility_id (PK) is required by the schema.
        conn.execute("INSERT INTO facility_assets (facility_id, source_id) "
                     "VALUES ('fac1','myfeed')")
        conn.commit()
        with self.assertRaises(ValueError) as ctx:
            data.remove_source(conn, "myfeed")
        msg = str(ctx.exception)
        self.assertIn("facility_assets: 1", msg)   # breakdown names the blocker
        # No partial delete; no raw IntegrityError leaked (ValueError only).
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM source_policies WHERE source_id='myfeed'").fetchone())

    def test_remove_zero_reference_operator_source_succeeds(self):
        conn = fixture_conn()
        data.add_source(conn, "myfeed", "My Feed", now=NOW)
        data.remove_source(conn, "myfeed", now=NOW)
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM source_policies WHERE source_id='myfeed'").fetchone())
        self.assertEqual(conn.execute(
            "SELECT field FROM config_audit ORDER BY audit_id DESC LIMIT 1")
            .fetchone()["field"], "__remove__")


class TestSourceReferenceBreakdown(unittest.TestCase):
    def test_counts_signals_via_raw_events(self):
        conn = fixture_conn()
        data.add_source(conn, "myfeed", "My Feed")
        conn.execute("INSERT INTO raw_events (raw_event_id, source_id) "
                     "VALUES ('re1','myfeed')")
        conn.execute("INSERT INTO source_runs (run_id, source_id) "
                     "VALUES ('run1','myfeed')")
        conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                     "decay_half_life_days) VALUES ('t','T',4,90)")
        conn.execute(
            "INSERT INTO signals (signal_id, raw_event_id, signal_scope, "
            " trigger_id) VALUES ('s1','re1','sector','t')")
        conn.commit()
        b = data.source_reference_breakdown(conn, "myfeed")
        self.assertEqual(b["raw_events"], 1)
        self.assertEqual(b["source_runs"], 1)
        self.assertEqual(b["signals"], 1)          # transitive via raw_events

    def test_empty_when_unreferenced(self):
        conn = fixture_conn()
        data.add_source(conn, "myfeed", "My Feed")
        self.assertEqual(data.source_reference_breakdown(conn, "myfeed"), {})


class TestSourcePolicyRows(unittest.TestCase):
    def test_rows_carry_origin_and_reference_count(self):
        conn = fixture_conn()
        data.add_source(conn, "myfeed", "My Feed")
        conn.execute("INSERT INTO raw_events (raw_event_id, source_id) "
                     "VALUES ('re1','myfeed')")
        conn.commit()
        rows = {r["source_id"]: r for r in data.source_policy_rows(conn, now=NOW)}
        self.assertEqual(rows["edgar"]["origin"], "seed")
        self.assertEqual(rows["edgar"]["reference_count"], 0)
        self.assertEqual(rows["myfeed"]["origin"], "operator")
        self.assertEqual(rows["myfeed"]["reference_count"], 1)


if __name__ == "__main__":
    unittest.main()
