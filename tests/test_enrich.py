"""Tests for enrichment acceptance logic and identifier fill precedence.

No network: fetchers are exercised in production runs; what must not regress
silently is (a) GLEIF candidates are accepted only on exact normalized-name
match, (b) an EDGAR CIK is accepted only on an unambiguous exact-name match
(R5.5), and (c) generated identifiers never overwrite hand-verified values.
"""
import csv
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from app.db.migrate import apply_migrations
from app.enrich_entities import accept_cik, accept_gleif, parse_cik_lookup
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


# EDGAR's cik-lookup-data.txt is 'COMPANY NAME:0000012345:' per line. The CIKs
# below are deliberately synthetic — no live SEC fill has been observed from
# this host, so no fixture here may be mistaken for a verified identifier.
CIK_LOOKUP_FIXTURE = "\n".join([
    "OGLETHORPE POWER CORP:0000000101:",
    "BASIN ELECTRIC POWER COOPERATIVE:0000000102:",
    "SEMINOLE ELECTRIC COOPERATIVE INC:0000000103:",
    "OLD DOMINION ELECTRIC COOPERATIVE:0000000104:",
    "DAIRYLAND POWER COOPERATIVE:0000000105:",
    "BUCKEYE POWER INC:0000000106:",
    "NEXTERA ENERGY INC:0000000107:",          # already CIK-bearing: not a fill
    "AMBIGUOUS POWER CO:0000000108:",
    "AMBIGUOUS POWER LLC:0000000109:",         # same normalized name, 2 CIKs
    "",
])


class TestCikLookup(unittest.TestCase):
    def test_parse_groups_ciks_by_normalized_name(self):
        lookup = parse_cik_lookup(CIK_LOOKUP_FIXTURE)
        self.assertEqual(lookup[normalize("Oglethorpe Power")], {"0000000101"})
        self.assertEqual(lookup[normalize("Ambiguous Power")],
                         {"0000000108", "0000000109"})

    def test_zero_pads_to_ten_digits(self):
        lookup = parse_cik_lookup("SOME UTILITY:12345:")
        self.assertEqual(lookup[normalize("Some Utility")], {"0000012345"})

    def test_exact_single_match_accepted(self):
        lookup = parse_cik_lookup(CIK_LOOKUP_FIXTURE)
        self.assertEqual(
            accept_cik({normalize("Oglethorpe Power")}, lookup), "0000000101")

    def test_ambiguous_name_rejected(self):
        lookup = parse_cik_lookup(CIK_LOOKUP_FIXTURE)
        self.assertIsNone(accept_cik({normalize("Ambiguous Power")}, lookup))

    def test_terms_pointing_at_different_filers_rejected(self):
        lookup = parse_cik_lookup(CIK_LOOKUP_FIXTURE)
        terms = {normalize("Oglethorpe Power"), normalize("Buckeye Power")}
        self.assertIsNone(accept_cik(terms, lookup))

    def test_absent_name_rejected(self):
        lookup = parse_cik_lookup(CIK_LOOKUP_FIXTURE)
        self.assertIsNone(accept_cik({normalize("ERCOT")}, lookup))


class TestCikCoverage(unittest.TestCase):
    """R5.5: app.ingest.edgar iterates CIK-bearing entities only, so CIK
    coverage over the real watchlist is the account-reach ceiling for the
    richest account-scoped source. Pins today's measured number and proves a
    canned EDGAR index lifts it."""

    def _watchlist(self):
        path = os.path.join(load_seeds.SEEDS_DIR, "watchlist_entities.csv")
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def test_measured_baseline_coverage(self):
        rows = self._watchlist()
        with_cik = [r for r in rows if (r["cik"] or "").strip()]
        self.assertEqual((len(with_cik), len(rows)), (130, 171))

    def test_canned_edgar_index_raises_coverage(self):
        rows = self._watchlist()
        lookup = parse_cik_lookup(CIK_LOOKUP_FIXTURE)
        before = sum(1 for r in rows if (r["cik"] or "").strip())
        filled = {}
        for r in rows:
            if (r["cik"] or "").strip():
                continue
            terms = {normalize(r["name"])}
            terms |= {normalize(a) for a in load_seeds.parse_list(r["aliases"])}
            cik = accept_cik(terms, lookup)
            if cik:
                filled[r["entity_id"]] = cik
        self.assertEqual(
            filled,
            {"E0047": "0000000102",   # Basin Electric Power Cooperative
             "E0049": "0000000101",   # Oglethorpe Power
             "E0054": "0000000103",   # Seminole Electric Cooperative
             "E0055": "0000000104",   # Old Dominion Electric Cooperative
             "E0056": "0000000106",   # Buckeye Power
             "E0057": "0000000105"})  # Dairyland Power Cooperative
        self.assertEqual(before + len(filled), 136)


class TestApplyIdentifiers(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, cik, lei, "
            "wikidata_qid) VALUES ('E1', 'Empty Corp', '', '', '')")
        conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, cik, lei, "
            "wikidata_qid) VALUES ('E2', 'Hand Corp', '0000000042', 'HANDLEI', "
            "'Q999')")
        return conn

    def _run(self, conn, rows):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "entity_identifiers.csv")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["entity_id", "cik", "lei", "wikidata_qid", "method",
                        "verified_at"])
            w.writerows(rows)
        with mock.patch.object(load_seeds, "SEEDS_DIR", tmp):
            return load_seeds.apply_identifiers(conn)

    def test_fills_empty_columns(self):
        conn = self._conn()
        stats = self._run(conn, [["E1", "0000000777", "GENLEI", "Q123",
                                  "sec_cik_lookup+wikidata_cik", "2026-08-16"]])
        self.assertEqual(stats, (1, 1, 1, 1, 0))
        row = conn.execute(
            "SELECT cik, lei, wikidata_qid FROM watchlist_entities "
            "WHERE entity_id='E1'").fetchone()
        self.assertEqual((row["cik"], row["lei"], row["wikidata_qid"]),
                         ("0000000777", "GENLEI", "Q123"))

    def test_never_overwrites_hand_verified(self):
        conn = self._conn()
        stats = self._run(conn, [["E2", "0000000777", "GENLEI", "Q123",
                                  "sec_cik_lookup+wikidata_cik", "2026-08-16"]])
        self.assertEqual(stats, (0, 0, 0, 1, 0))
        row = conn.execute(
            "SELECT cik, lei, wikidata_qid FROM watchlist_entities "
            "WHERE entity_id='E2'").fetchone()
        self.assertEqual((row["cik"], row["lei"], row["wikidata_qid"]),
                         ("0000000042", "HANDLEI", "Q999"))

    def test_hand_verified_cik_never_overwritten(self):
        """A generated CIK that contradicts the hand-verified watchlist value
        must lose: a silently rewritten CIK re-points EDGAR ingestion at
        another filer's 8-Ks under this account's name."""
        conn = self._conn()
        stats = self._run(conn, [["E2", "0009999999", "", "", "sec_cik_lookup",
                                  "2026-08-16"]])
        self.assertEqual(stats, (0, 0, 0, 1, 0))
        row = conn.execute(
            "SELECT cik FROM watchlist_entities WHERE entity_id='E2'").fetchone()
        self.assertEqual(row["cik"], "0000000042")

    def test_unknown_entity_skipped(self):
        conn = self._conn()
        stats = self._run(conn, [["E9", "0000000777", "L", "Q", "m", "2026-08-16"]])
        self.assertEqual(stats, (0, 0, 0, 1, 0))

    def test_duplicate_generated_identifiers_skipped(self):
        conn = self._conn()
        stats = self._run(
            conn,
            [["E1", "0000000777", "DUPLEI", "Q777", "sec_cik_lookup", "2026-08-16"],
             ["E2", "0000000777", "DUPLEI", "Q777", "sec_cik_lookup", "2026-08-16"]])
        self.assertEqual(stats, (0, 0, 0, 2, 6))
        row = conn.execute(
            "SELECT cik, lei, wikidata_qid FROM watchlist_entities "
            "WHERE entity_id='E1'").fetchone()
        self.assertEqual((row["cik"], row["lei"], row["wikidata_qid"]),
                         ("", "", ""))


class TestLoadSeedRefresh(unittest.TestCase):
    def test_generated_relationships_refresh(self):
        with tempfile.TemporaryDirectory() as td:
            temp_seeds = os.path.join(td, "seeds")
            shutil.copytree(load_seeds.SEEDS_DIR, temp_seeds)
            db_path = os.path.join(td, "gridsignals.db")
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
