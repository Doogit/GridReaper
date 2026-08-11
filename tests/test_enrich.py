"""Tests for enrichment acceptance logic and identifier fill precedence.

No network: fetchers are exercised in production runs; what must not regress
silently is (a) GLEIF candidates are accepted only on exact normalized-name
match, and (b) generated identifiers never overwrite hand-verified values.
"""
import csv
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from app.db.migrate import apply_migrations
from app.enrich_entities import accept_gleif
from app.db import load_seeds
from app.resolve import normalize


class TestAcceptGleif(unittest.TestCase):
    TERMS = {normalize("Otter Tail"), normalize("Otter Tail Power")}

    def test_exact_normalized_match_accepted(self):
        cands = [("LEI1", ["OTTER TAIL CORPORATION"], "US")]
        self.assertEqual(accept_gleif(self.TERMS, cands), "LEI1")

    def test_other_names_checked(self):
        cands = [("LEI2", ["OT HOLDCO LLC", "Otter Tail Power Company"], "US")]
        self.assertEqual(accept_gleif(self.TERMS, cands), "LEI2")

    def test_near_miss_rejected(self):
        cands = [("LEI3", ["Otter Tail Lakes Country Tourism"], "US")]
        self.assertIsNone(accept_gleif(self.TERMS, cands))

    def test_non_us_rejected(self):
        cands = [("LEI4", ["Otter Tail Corporation"], "CA")]
        self.assertIsNone(accept_gleif(self.TERMS, cands))

    def test_first_acceptable_wins(self):
        cands = [("LEI5", ["Otter Tail County"], "US"),
                 ("LEI6", ["Otter Tail, Inc."], "US")]
        self.assertEqual(accept_gleif(self.TERMS, cands), "LEI6")


class TestApplyIdentifiers(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, lei, wikidata_qid) "
            "VALUES ('E1', 'Empty Corp', '', '')")
        conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, lei, wikidata_qid) "
            "VALUES ('E2', 'Hand Corp', 'HANDLEI', 'Q999')")
        return conn

    def _run(self, conn, rows):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "entity_identifiers.csv")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["entity_id", "lei", "wikidata_qid", "method", "verified_at"])
            w.writerows(rows)
        with mock.patch.object(load_seeds, "SEEDS_DIR", tmp):
            return load_seeds.apply_identifiers(conn)

    def test_fills_empty_columns(self):
        conn = self._conn()
        stats = self._run(conn, [["E1", "GENLEI", "Q123", "wikidata_cik", "2026-08-11"]])
        self.assertEqual(stats, (1, 1, 1, 0))
        row = conn.execute(
            "SELECT lei, wikidata_qid FROM watchlist_entities "
            "WHERE entity_id='E1'").fetchone()
        self.assertEqual((row["lei"], row["wikidata_qid"]), ("GENLEI", "Q123"))

    def test_never_overwrites_hand_verified(self):
        conn = self._conn()
        stats = self._run(conn, [["E2", "GENLEI", "Q123", "wikidata_cik", "2026-08-11"]])
        self.assertEqual(stats, (0, 0, 1, 0))
        row = conn.execute(
            "SELECT lei, wikidata_qid FROM watchlist_entities "
            "WHERE entity_id='E2'").fetchone()
        self.assertEqual((row["lei"], row["wikidata_qid"]), ("HANDLEI", "Q999"))

    def test_unknown_entity_skipped(self):
        conn = self._conn()
        stats = self._run(conn, [["E9", "L", "Q", "m", "2026-08-11"]])
        self.assertEqual(stats, (0, 0, 1, 0))

    def test_duplicate_generated_identifiers_skipped(self):
        conn = self._conn()
        stats = self._run(
            conn,
            [["E1", "DUPLEI", "Q777", "gleif_exact_name", "2026-08-11"],
             ["E2", "DUPLEI", "Q777", "gleif_exact_name", "2026-08-11"]])
        self.assertEqual(stats, (0, 0, 2, 4))
        row = conn.execute(
            "SELECT lei, wikidata_qid FROM watchlist_entities "
            "WHERE entity_id='E1'").fetchone()
        self.assertEqual((row["lei"], row["wikidata_qid"]), ("", ""))


class TestLoadSeedRefresh(unittest.TestCase):
    def test_generated_relationships_refresh(self):
        with tempfile.TemporaryDirectory() as td:
            temp_seeds = os.path.join(td, "seeds")
            shutil.copytree(load_seeds.SEEDS_DIR, temp_seeds)
            db_path = os.path.join(td, "gridreaper.db")
            rel_path = os.path.join(temp_seeds, "entity_relationships.csv")
            old_seeds_dir = load_seeds.SEEDS_DIR
            load_seeds.SEEDS_DIR = temp_seeds
            try:
                self.assertEqual(load_seeds.load(db_path), 0)
                conn = sqlite3.connect(db_path)
                count = conn.execute(
                    "SELECT COUNT(*) FROM entity_relationships "
                    "WHERE source='gleif'").fetchone()[0]
                conn.close()
                self.assertEqual(count, 1)

                with open(rel_path, "w", newline="", encoding="utf-8") as fh:
                    writer = csv.writer(fh)
                    writer.writerow([
                        "parent_entity_id", "child_entity_id",
                        "relationship_type", "source", "verified_at"])
                self.assertEqual(load_seeds.load(db_path), 0)
                conn = sqlite3.connect(db_path)
                count = conn.execute(
                    "SELECT COUNT(*) FROM entity_relationships "
                    "WHERE source='gleif'").fetchone()[0]
                conn.close()
                self.assertEqual(count, 0)
            finally:
                load_seeds.SEEDS_DIR = old_seeds_dir


if __name__ == "__main__":
    unittest.main()
