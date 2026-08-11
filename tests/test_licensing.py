"""Tests for the license facts / play candidates transform (R7.6, R7.7).

Hermetic: in-memory SQLite via apply_migrations with FK enforcement ON —
the FK is exactly what guards the SKU->family mapping. What must not
regress: the gcc_high segment-mapping rule (conservative, lossless), NULL
product_id for baseline suites, hard errors on unmapped SKUs, conditional
discovery questions that never assert the account's tier, and idempotency.
One test loads the real seed CSVs to pin mapping completeness to the
shipped license_matrix.
"""
import json
import os
import sqlite3
import tempfile
import unittest

from app.db.migrate import apply_migrations
from app.db import load_seeds
from app import licensing
from app.licensing import (
    CONFIG_VERSION, FAMILY_PLAY_SKU, SKU_FAMILY, gcc_high_note, rebuild)


class TestGccHighNote(unittest.TestCase):
    def test_yes_variants_available(self):
        self.assertEqual(gcc_high_note("yes"), "available — yes")
        self.assertEqual(gcc_high_note("yes (G5)"), "available — yes (G5)")
        self.assertEqual(gcc_high_note("YES"), "available — YES")

    def test_no_variants_unavailable(self):
        self.assertEqual(gcc_high_note("NO"), "not available — NO")
        self.assertEqual(
            gcc_high_note("NO (not in GCC/GCC-H/DoD)"),
            "not available — NO (not in GCC/GCC-H/DoD)")
        self.assertEqual(
            gcc_high_note("no (IRM N/A GCC-H)"),
            "not available — no (IRM N/A GCC-H)")

    def test_everything_else_needs_curation(self):
        for cell in ["limited", "partial", "unknown", "unverified for gov",
                     "n/a", "Azure Gov path only", "tbd"]:
            self.assertEqual(gcc_high_note(cell), f"needs_curation — {cell}",
                             cell)

    def test_empty_cell_no_fact(self):
        self.assertIsNone(gcc_high_note(""))
        self.assertIsNone(gcc_high_note("   "))
        self.assertIsNone(gcc_high_note(None))

    def test_lossless_raw_preserved(self):
        raw = "Azure Gov path only (commercial SKU not sold GCC-H)"
        self.assertTrue(gcc_high_note(raw).endswith(raw))


class LicensingTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        for pid, name in [("def_endpoint", "Defender for Endpoint (P1/P2)"),
                          ("def_identity", "Defender for Identity")]:
            self.conn.execute(
                "INSERT INTO products (product_id, name) VALUES (?, ?)",
                (pid, name))
        self.conn.execute(
            "INSERT INTO triggers (trigger_id, name, base_strength, "
            " decay_half_life_days) VALUES ('own_incident', 'Own incident', "
            " 5, 30)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def add_matrix(self, sku, tier, gcc_high="yes", price="$1/user/mo",
                   path="E3 -> E5 Security addon", included="addon"):
        self.conn.execute(
            "INSERT INTO license_matrix (product_id, tier, included_or_addon, "
            " addon_price_note, upgrade_path, gcc_high, source_quality, "
            " verified_date, source_url) VALUES (?, ?, ?, ?, ?, ?, "
            " 'primary', '2026-08-11', 'https://example.com')",
            (sku, tier, included, price, path, gcc_high))

    def facts(self):
        return self.conn.execute(
            "SELECT * FROM license_facts ORDER BY fact_id").fetchall()


class TestBuildFacts(LicensingTestCase):
    def test_commercial_and_gcc_fact_per_row(self):
        self.add_matrix("defender_endpoint_p2", "e5", gcc_high="yes (not full parity)")
        counts = rebuild(self.conn)
        self.assertEqual(counts["facts"], 2)
        self.assertEqual(counts["facts_by_segment"],
                         {"commercial": 1, "gcc_high": 1})
        rows = {r["segment"]: r for r in self.facts()}
        com, gcc = rows["commercial"], rows["gcc_high"]
        self.assertEqual(com["fact_id"], "defender_endpoint_p2:e5:commercial")
        self.assertEqual(com["sku_or_plan"], "defender_endpoint_p2:e5")
        self.assertEqual(com["product_id"], "def_endpoint")
        self.assertEqual(com["price_note"], "$1/user/mo")
        self.assertEqual(com["included_or_addon"], "addon")
        self.assertEqual(com["source_quality"], "primary")
        self.assertEqual(gcc["fact_id"], "defender_endpoint_p2:e5:gcc_high")
        self.assertEqual(gcc["price_note"],
                         "available — yes (not full parity)")

    def test_empty_gcc_cell_yields_commercial_only(self):
        self.add_matrix("defender_endpoint_p1", "e3", gcc_high="")
        counts = rebuild(self.conn)
        self.assertEqual(counts["facts_by_segment"], {"commercial": 1})

    def test_suite_rows_get_null_product_id(self):
        self.add_matrix("suite_m365_e3", "baseline", gcc_high="yes (G3)",
                        included="suite")
        rebuild(self.conn)   # FK on: NULL must be accepted
        row = self.facts()[0]
        self.assertIsNone(row["product_id"])
        self.assertEqual(row["sku_or_plan"], "suite_m365_e3:baseline")

    def test_unmapped_sku_raises(self):
        self.add_matrix("mystery_sku", "e5")
        with self.assertRaises(ValueError) as ctx:
            rebuild(self.conn)
        self.assertIn("mystery_sku", str(ctx.exception))
        # nothing partially written
        self.assertEqual(len(self.facts()), 0)

    def test_prerequisite_from_unambiguous_tiers(self):
        self.add_matrix("suite_e5_security_addon", "addon_to_e3")
        self.add_matrix("defender_vuln_mgmt", "addon_to_mdep2")
        self.add_matrix("defender_endpoint_p2", "e5")
        rebuild(self.conn)
        prereqs = {r["sku_or_plan"]: r["prerequisite"] for r in self.facts()}
        self.assertEqual(prereqs["suite_e5_security_addon:addon_to_e3"], "E3")
        self.assertEqual(prereqs["defender_vuln_mgmt:addon_to_mdep2"], "MDE P2")
        self.assertEqual(prereqs["defender_endpoint_p2:e5"], "")


class TestPlayCandidates(LicensingTestCase):
    def _seed_play(self):
        self.add_matrix("defender_endpoint_p2", "e5",
                        path="E3 -> E5 Security addon OR full E5")
        self.conn.execute(
            "INSERT INTO indicator_map (trigger_id, product_id) "
            "VALUES ('own_incident', 'def_endpoint')")
        self.conn.commit()

    def candidates(self):
        return self.conn.execute(
            "SELECT * FROM license_play_candidates ORDER BY play_id").fetchall()

    def test_one_candidate_per_indicator_row(self):
        self._seed_play()
        counts = rebuild(self.conn)
        self.assertEqual(counts["candidates"], 1)
        row = self.candidates()[0]
        self.assertEqual(row["play_id"], "own_incident:def_endpoint")
        self.assertEqual(row["trigger_id"], "own_incident")
        self.assertEqual(row["product_id"], "def_endpoint")
        self.assertEqual(row["assumed_baseline"], "E3")
        self.assertEqual(row["recommended_path"],
                         "E3 -> E5 Security addon OR full E5")
        self.assertEqual(row["config_version"], CONFIG_VERSION)

    def test_discovery_question_is_conditional(self):
        self._seed_play()
        rebuild(self.conn)
        q = self.candidates()[0]["discovery_question"]
        self.assertNotIn("you are on", q.lower())   # never asserts tier (R7.7)
        self.assertIn("If E3", q)
        self.assertIn("E3 -> E5 Security addon OR full E5", q)
        self.assertIn("Defender for Endpoint (P1/P2)", q)

    def _add_snapshot(self, signal_id, play_id, fact_ids):
        """Minimal FK-satisfying snapshot: raw_event -> signal -> snapshot."""
        self.conn.execute(
            "INSERT INTO raw_events (raw_event_id) VALUES ('raw-l1') "
            "ON CONFLICT DO NOTHING")
        self.conn.execute(
            "INSERT INTO signals (signal_id, raw_event_id, signal_scope, "
            " trigger_id) VALUES (?, 'raw-l1', 'sector', 'own_incident') "
            "ON CONFLICT DO NOTHING", (signal_id,))
        self.conn.execute(
            "INSERT INTO license_play_snapshots (signal_id, play_id, "
            " fact_ids, generated_at, generation_version, display_text, "
            " outreach_safe_text) VALUES (?, ?, ?, 't', 'plays/1.0', '', '')",
            (signal_id, play_id, json.dumps(fact_ids)))
        self.conn.commit()

    def test_rebuild_survives_existing_snapshots(self):
        """Regression: the quarterly refresh must not FK-crash once a card
        exists (snapshots reference play_ids); candidates upsert in place."""
        self._seed_play()
        rebuild(self.conn)
        self._add_snapshot("sig-1", "own_incident:def_endpoint",
                           ["defender_endpoint_p2:e5:commercial"])
        # simulate the quarterly licensing edit
        self.conn.execute(
            "UPDATE license_matrix SET upgrade_path = 'E5 direct' "
            "WHERE product_id = 'defender_endpoint_p2'")
        counts = rebuild(self.conn)   # must not raise IntegrityError
        self.assertEqual(counts["candidates"], 1)
        self.assertEqual(counts["stale_kept"], [])
        self.assertEqual(counts["orphaned_fact_refs"], 0)
        row = self.candidates()[0]
        self.assertEqual(row["recommended_path"], "E5 direct")   # refreshed
        snap = self.conn.execute(
            "SELECT * FROM license_play_snapshots").fetchone()
        self.assertEqual(snap["play_id"], "own_incident:def_endpoint")

    def test_rebuild_keeps_snapshot_referenced_stale_candidate(self):
        """A candidate that stops being generated survives (pinned history)
        when a snapshot references it, and is reported in stale_kept."""
        self._seed_play()
        rebuild(self.conn)
        self._add_snapshot("sig-1", "own_incident:def_endpoint",
                           ["defender_endpoint_p2:e5:commercial"])
        self.conn.execute(
            "DELETE FROM indicator_map WHERE trigger_id = 'own_incident'")
        self.conn.commit()
        counts = rebuild(self.conn)
        self.assertEqual(counts["candidates"], 0)
        self.assertEqual(counts["stale_kept"], ["own_incident:def_endpoint"])
        self.assertEqual(len(self.candidates()), 1)   # kept, not deleted

    def test_rebuild_reports_orphaned_snapshot_fact_refs(self):
        """A SKU/tier rename must be loud: pinned fact_ids that no longer
        resolve are counted, never silently orphaned."""
        self._seed_play()
        self.add_matrix("defender_endpoint_p1", "e3")
        rebuild(self.conn)
        self._add_snapshot("sig-1", "own_incident:def_endpoint",
                           ["defender_endpoint_p1:e3:commercial",
                            "defender_endpoint_p1:e3:gcc_high"])
        # rename the tier (a normal quarterly Microsoft move)
        self.conn.execute(
            "UPDATE license_matrix SET tier = 'e3_new' "
            "WHERE product_id = 'defender_endpoint_p1'")
        self.conn.commit()
        counts = rebuild(self.conn)
        self.assertEqual(counts["orphaned_fact_refs"], 2)

    def test_missing_canonical_sku_row_raises(self):
        # indicator family present but its canonical play SKU row is absent
        self.conn.execute(
            "INSERT INTO indicator_map (trigger_id, product_id) "
            "VALUES ('own_incident', 'def_endpoint')")
        self.conn.commit()
        with self.assertRaises(ValueError) as ctx:
            rebuild(self.conn)
        self.assertIn("defender_endpoint_p2", str(ctx.exception))

    def test_idempotent_rebuild(self):
        self._seed_play()
        first = rebuild(self.conn)
        second = rebuild(self.conn)
        self.assertEqual(first, second)
        self.assertEqual(len(self.facts()), first["facts"])
        self.assertEqual(len(self.candidates()), first["candidates"])


class TestRealSeeds(unittest.TestCase):
    def test_mapping_complete_against_shipped_matrix(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "gridsignals.db")
            self.assertEqual(load_seeds.load(db_path), 0)
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON;")
            try:
                matrix_skus = {r["product_id"] for r in conn.execute(
                    "SELECT DISTINCT product_id FROM license_matrix")}
                self.assertEqual(matrix_skus, set(SKU_FAMILY))
                indicator_families = {r["product_id"] for r in conn.execute(
                    "SELECT DISTINCT product_id FROM indicator_map")}
                self.assertTrue(indicator_families <= set(FAMILY_PLAY_SKU))
                counts = rebuild(conn)
                self.assertGreaterEqual(counts["facts"], 56)
                indicator_rows = conn.execute(
                    "SELECT COUNT(*) FROM indicator_map").fetchone()[0]
                self.assertEqual(counts["candidates"], indicator_rows)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
