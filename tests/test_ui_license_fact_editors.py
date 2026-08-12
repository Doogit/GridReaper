"""License-fact editor helper tests (R8.7; R3.3, R7.6).

Hermetic: real migrations against in-memory SQLite, FK on, fixture rows,
injected NOW. Covers update / add validation and provenance, the JSON-scan
citation gate, and the LOAD-BEARING frozen-snapshot contract: a fact edit must
NOT regenerate a play snapshot (cards read frozen snapshots, R7.6). The helpers
take a passed connection so DB assertions stay hermetic.
"""
import json
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
    conn.execute("INSERT INTO products (product_id, name) "
                 "VALUES ('def_endpoint','Defender for Endpoint')")
    # A transform-style fact (sku_or_plan set) and its identity columns.
    conn.execute(
        "INSERT INTO license_facts (fact_id, product_id, sku_or_plan, segment, "
        " price_note, included_or_addon, prerequisite, effective_date, "
        " verified_date, source_quality, source_url) VALUES "
        "('F1','def_endpoint','defender_endpoint_p2:e5','commercial', "
        " 'addon $x', 'addon', '', '', '2025-01-01', 'primary', 'http://a')")
    conn.commit()
    return conn


def _seed_snapshot(conn, signal_id, fact_ids, display_text="TEXT",
                   generated_at="2026-01-01T00:00:00+00:00"):
    """Minimal FK-satisfying snapshot: raw_event -> signal -> candidate ->
    snapshot, citing fact_ids (JSON array)."""
    conn.execute("INSERT INTO raw_events (raw_event_id) VALUES ('re1') "
                 "ON CONFLICT DO NOTHING")
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t','Trig',4,90) "
                 "ON CONFLICT DO NOTHING")
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, signal_scope, trigger_id) "
        "VALUES (?, 're1', 'sector', 't') ON CONFLICT DO NOTHING", (signal_id,))
    conn.execute("INSERT INTO license_play_candidates (play_id, trigger_id) "
                 "VALUES ('p1','t') ON CONFLICT DO NOTHING")
    conn.execute(
        "INSERT INTO license_play_snapshots (signal_id, play_id, fact_ids, "
        " generated_at, generation_version, display_text, outreach_safe_text) "
        "VALUES (?, 'p1', ?, ?, 'plays/1.0', ?, '')",
        (signal_id, json.dumps(fact_ids), generated_at, display_text))
    conn.commit()


class TestUpdateFact(unittest.TestCase):
    def test_edit_writes_one_audit_row(self):
        conn = fixture_conn()
        res = data.update_license_fact(conn, "F1", "price_note", "addon $y",
                                       reason="repriced", now=NOW)
        self.assertTrue(res["changed"])
        self.assertEqual(conn.execute(
            "SELECT price_note FROM license_facts WHERE fact_id='F1'")
            .fetchone()["price_note"], "addon $y")
        audit = conn.execute("SELECT * FROM config_audit").fetchall()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["table_name"], "license_facts")
        self.assertEqual(audit[0]["pk"], "F1")
        self.assertEqual(audit[0]["field"], "price_note")
        self.assertEqual(audit[0]["old_value"], "addon $x")
        self.assertEqual(audit[0]["new_value"], "addon $y")
        self.assertTrue(audit[0]["ts"].startswith("2026-08-01"))

    def test_bad_source_quality_rejected(self):
        conn = fixture_conn()
        with self.assertRaises(ValueError):
            data.update_license_fact(conn, "F1", "source_quality", "gold")
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM config_audit")
                         .fetchone()["n"], 0)

    def test_non_editable_column_rejected(self):
        conn = fixture_conn()
        for col in ("segment", "sku_or_plan", "product_id", "fact_id"):
            with self.assertRaises(ValueError):
                data.update_license_fact(conn, "F1", col, "x")

    def test_bad_date_rejected(self):
        conn = fixture_conn()
        with self.assertRaises(ValueError):
            data.update_license_fact(conn, "F1", "verified_date", "not-a-date")

    def test_trailing_junk_date_rejected(self):
        conn = fixture_conn()
        with self.assertRaises(ValueError):
            data.update_license_fact(conn, "F1", "verified_date",
                                     "2026-08-01junk")

    def test_future_verified_date_rejected(self):
        # A future verified_date would compute a negative age and hide the fact
        # from the R10.7 staleness list. NOW is 2026-08-01.
        conn = fixture_conn()
        with self.assertRaises(ValueError) as ctx:
            data.update_license_fact(conn, "F1", "verified_date", "2099-12-31",
                                     now=NOW)
        self.assertIn("future", str(ctx.exception).lower())
        # nothing written, and the stored date is unchanged
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM config_audit")
                         .fetchone()["n"], 0)
        self.assertEqual(conn.execute(
            "SELECT verified_date FROM license_facts WHERE fact_id='F1'")
            .fetchone()["verified_date"], "2025-01-01")

    def test_future_effective_date_allowed(self):
        # Control: effective_date may legitimately be in the future (a rule
        # effective 2026-10-01) - the future-check is verified_date-only.
        conn = fixture_conn()
        res = data.update_license_fact(conn, "F1", "effective_date",
                                       "2099-12-31", now=NOW)
        self.assertTrue(res["changed"])
        self.assertEqual(conn.execute(
            "SELECT effective_date FROM license_facts WHERE fact_id='F1'")
            .fetchone()["effective_date"], "2099-12-31")

    def test_noop_writes_nothing(self):
        conn = fixture_conn()
        res = data.update_license_fact(conn, "F1", "price_note", "addon $x")
        self.assertFalse(res["changed"])
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM config_audit")
                         .fetchone()["n"], 0)

    def test_unknown_fact_rejected(self):
        conn = fixture_conn()
        with self.assertRaises(ValueError):
            data.update_license_fact(conn, "NOPE", "price_note", "x")


class TestAddFact(unittest.TestCase):
    def test_happy_add_and_audit(self):
        conn = fixture_conn()
        res = data.add_license_fact(
            conn, "F_OP", "def_endpoint",
            fields={"price_note": "manual", "source_quality": "non-primary",
                    "segment": "gcc_high"}, reason="manual source", now=NOW)
        self.assertTrue(res["created"])
        row = conn.execute(
            "SELECT product_id, sku_or_plan, segment, price_note, source_quality "
            "FROM license_facts WHERE fact_id='F_OP'").fetchone()
        self.assertEqual(row["product_id"], "def_endpoint")
        self.assertIsNone(row["sku_or_plan"])            # operator marker
        self.assertEqual(row["segment"], "gcc_high")
        self.assertEqual(row["price_note"], "manual")
        audit = conn.execute("SELECT field, new_value FROM config_audit").fetchone()
        self.assertEqual(audit["field"], "__add__")
        # new_value records the created fact_id (not product_id, which may be
        # None for a product-less operator fact) so the trail says what was made.
        self.assertEqual(audit["new_value"], "F_OP")

    def test_add_future_verified_date_rejected(self):
        conn = fixture_conn()
        with self.assertRaises(ValueError) as ctx:
            data.add_license_fact(conn, "F_OP", "def_endpoint",
                                  fields={"verified_date": "2099-12-31"}, now=NOW)
        self.assertIn("future", str(ctx.exception).lower())
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM license_facts WHERE fact_id='F_OP'").fetchone())

    def test_add_future_effective_date_allowed(self):
        conn = fixture_conn()
        data.add_license_fact(conn, "F_OP", None,
                              fields={"effective_date": "2099-12-31"}, now=NOW)
        self.assertEqual(conn.execute(
            "SELECT effective_date FROM license_facts WHERE fact_id='F_OP'")
            .fetchone()["effective_date"], "2099-12-31")

    def test_duplicate_fact_id_rejected(self):
        conn = fixture_conn()
        with self.assertRaises(ValueError):
            data.add_license_fact(conn, "F1", "def_endpoint")

    def test_unknown_product_rejected(self):
        conn = fixture_conn()
        with self.assertRaises(ValueError):
            data.add_license_fact(conn, "F_OP", "no_such_product")

    def test_bad_segment_rejected(self):
        conn = fixture_conn()
        with self.assertRaises(ValueError):
            data.add_license_fact(conn, "F_OP", None, fields={"segment": "bogus"})

    def test_null_product_allowed(self):
        conn = fixture_conn()
        data.add_license_fact(conn, "F_OP", "", fields={"price_note": "x"})
        self.assertIsNone(conn.execute(
            "SELECT product_id FROM license_facts WHERE fact_id='F_OP'")
            .fetchone()["product_id"])


class TestFrozenSnapshotContract(unittest.TestCase):
    def test_fact_edit_does_not_regenerate_snapshot(self):
        conn = fixture_conn()
        _seed_snapshot(conn, "sig-1", ["F1"], display_text="FROZEN TEXT",
                       generated_at="2026-01-01T00:00:00+00:00")
        before = conn.execute(
            "SELECT fact_ids, display_text, generated_at "
            "FROM license_play_snapshots WHERE signal_id='sig-1'").fetchone()
        data.update_license_fact(conn, "F1", "price_note", "changed", now=NOW)
        after = conn.execute(
            "SELECT fact_ids, display_text, generated_at "
            "FROM license_play_snapshots WHERE signal_id='sig-1'").fetchone()
        # Byte-identical: the edit never fired a snapshot regenerate (R7.6).
        self.assertEqual(after["fact_ids"], before["fact_ids"])
        self.assertEqual(after["display_text"], before["display_text"])
        self.assertEqual(after["generated_at"], before["generated_at"])


class TestSnapshotCitations(unittest.TestCase):
    def test_json_gate_counts_membership(self):
        conn = fixture_conn()
        _seed_snapshot(conn, "sig-1", ["F1", "F2"])
        self.assertEqual(data.fact_snapshot_citations(conn, "F1"), 1)
        self.assertEqual(data.fact_snapshot_citations(conn, "F2"), 1)
        self.assertEqual(data.fact_snapshot_citations(conn, "F_UNCITED"), 0)

    def test_non_array_json_is_not_a_citation(self):
        conn = fixture_conn()
        _seed_snapshot(conn, "sig-1", "F1")
        self.assertEqual(data.fact_snapshot_citations(conn, "F1"), 0)


if __name__ == "__main__":
    unittest.main()
