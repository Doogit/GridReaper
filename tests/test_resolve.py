"""Entity-resolution tests, including the R6.7 adversarial fixture set.

The fixture DB is hermetic: real migrations applied to an in-memory SQLite,
with a small entity set mirroring real watchlist rows (same ids/names) plus
synthetic near-twins for ambiguity cases. Adversarial cases live in
tests/fixtures/adversarial_cases.csv and run as subtests.
"""
import csv
import json
import os
import sqlite3
import unittest

from app.db.migrate import apply_migrations
from app.resolve import (EntityResolver, PARSER_VERSION, enqueue_review,
                         normalize, record_decision)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

ENTITIES = [
    # (entity_id, name, cik, ticker) - mirrors real watchlist rows
    ("E0004", "Dominion Energy", "0000715957", "D"),
    ("E0005", "American Electric Power", "0000004904", "AEP"),
    ("E0007", "Constellation Energy", "0001868275", "CEG"),
    ("E0008", "PG&E", "0001004980", "PCG"),
    ("E0028", "TXNM Energy (PNM)", "0001108426", "TXNM"),
    ("E0038", "Otter Tail", "0001466593", "OTTR"),
    ("E0046", "PNM Resources", "0000022767", "PNM"),
    ("E0092", "Range Resources", "0000315852", "RRC"),
    ("E0093", "Chord Energy", "0001486159", "CHRD"),
    # synthetic near-twins for fuzzy-ambiguity cases
    ("EX001", "Delta Utilities", "", ""),
    ("EX002", "Delta Utility Services", "", ""),
]

ALIASES = [
    ("E0005", "AEP"),
    ("E0008", "Pacific Gas and Electric"),
    ("E0008", "PG&E Corporation"),
    ("E0028", "TXNM Energy"),
    ("E0038", "Otter Tail Power"),
    ("E0038", "Otter Tail Corporation"),
    ("E0046", "Texas-New Mexico Power"),
]

COLLISIONS = [
    ("E0004", "Dominion"),
    ("E0093", "Chord"),
    ("E0092", "Range"),
    ("E0007", "Constellation"),
    ("E0038", "Otter Tail"),
    ("E0028", "PNM"),
    ("E0046", "PNM"),
]


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    for eid, name, cik, ticker in ENTITIES:
        conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, cik, ticker) "
            "VALUES (?, ?, ?, ?)", (eid, name, cik, ticker))
    for eid, alias in ALIASES:
        conn.execute(
            "INSERT INTO entity_aliases (entity_id, alias) VALUES (?, ?)",
            (eid, alias))
    for eid, term in COLLISIONS:
        conn.execute(
            "INSERT INTO entity_collision_terms (entity_id, term) "
            "VALUES (?, ?)", (eid, term))
    # stub raw events for FK-checked decision/review writes
    for rid in ("raw-1", "raw-2"):
        conn.execute(
            "INSERT INTO raw_events (raw_event_id) VALUES (?)", (rid,))
    conn.commit()
    return conn


class TestNormalize(unittest.TestCase):
    def test_suffixes_punctuation_ampersand(self):
        self.assertEqual(normalize("Duke Energy Corporation"), "duke energy")
        self.assertEqual(normalize("Pacific Gas & Electric Company"),
                         normalize("Pacific Gas and Electric"))
        self.assertEqual(normalize("  Otter Tail, Inc."), "otter tail")
        self.assertEqual(normalize(""), "")


class TestAdversarialFixtures(unittest.TestCase):
    """R6.7: every case in adversarial_cases.csv must resolve as annotated."""

    @classmethod
    def setUpClass(cls):
        cls.conn = fixture_conn()
        cls.resolver = EntityResolver(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_adversarial_cases(self):
        path = os.path.join(FIXTURES, "adversarial_cases.csv")
        with open(path, newline="", encoding="utf-8") as fh:
            cases = list(csv.DictReader(fh))
        self.assertGreater(len(cases), 0)
        for case in cases:
            with self.subTest(case=case["case"]):
                kwargs = {case["input_kind"]: case["input_value"]}
                if case["context_text"]:
                    kwargs["context_text"] = case["context_text"]
                res = self.resolver.resolve(**kwargs)
                self.assertEqual(res.status, case["expected_status"],
                                 f"{case['case']}: got {res}")
                if case["expected_entity"]:
                    self.assertEqual(res.entity_id, case["expected_entity"],
                                     f"{case['case']}: got {res}")


class TestResolverBehavior(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = fixture_conn()
        cls.resolver = EntityResolver(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_review_never_carries_entity(self):
        """R6.2: below-threshold results MUST NOT auto-fire - no entity_id."""
        res = self.resolver.resolve(name="Dominion")
        self.assertEqual(res.status, "review")
        self.assertIsNone(res.entity_id)
        self.assertTrue(res.candidates)

    def test_ambiguous_review_lists_all_candidates(self):
        res = self.resolver.resolve(name="PNM")
        self.assertEqual({e for e, _ in res.candidates}, {"E0028", "E0046"})

    def test_multi_candidate_context_stays_review(self):
        """Context only breaks a tie when it corroborates one candidate."""
        res = self.resolver.resolve(
            name="PNM",
            context_text="TXNM Energy and PNM Resources both appeared")
        self.assertEqual(res.status, "review")
        self.assertIsNone(res.entity_id)
        self.assertEqual(res.method, "ambiguous_context")
        self.assertEqual({e for e, _ in res.candidates}, {"E0028", "E0046"})

    def test_deterministic_beats_fuzzy(self):
        """CIK wins even when the name would go to review."""
        res = self.resolver.resolve(cik="715957", name="Dominion")
        self.assertEqual((res.status, res.entity_id), ("matched", "E0004"))
        self.assertEqual(res.method, "cik")

    def test_record_decision_writes_r64_fields(self):
        conn = fixture_conn()
        resolver = EntityResolver(conn)
        res = resolver.resolve(name="AEP")
        record_decision(conn, "raw-1", res)
        row = conn.execute("SELECT * FROM entity_match_decisions").fetchone()
        self.assertEqual(row["entity_id"], "E0005")
        self.assertEqual(row["decision"], "auto")
        self.assertEqual(row["parser_version"], PARSER_VERSION)
        self.assertEqual(json.loads(row["matched_terms"]), ["aep"])
        self.assertTrue(row["ts"])
        conn.close()

    def test_enqueue_review_one_row_per_candidate(self):
        conn = fixture_conn()
        resolver = EntityResolver(conn)
        res = resolver.resolve(name="PNM")
        enqueue_review(conn, "raw-2", res)
        rows = conn.execute(
            "SELECT candidate_entity_id, disposition FROM review_queue "
            "ORDER BY candidate_entity_id").fetchall()
        self.assertEqual([(r[0], r[1]) for r in rows],
                         [("E0028", "pending"), ("E0046", "pending")])
        conn.close()


class TestSoftDisable(unittest.TestCase):
    """R8.7: a soft-disabled (active=0) entity is excluded from resolution -
    identifiers, name, aliases, and collision terms all drop out. The resolver
    caches, so a rebuild after the edit is what applies the change."""

    def _disable(self, conn, eid):
        conn.execute("UPDATE watchlist_entities SET active = 0 WHERE entity_id = ?",
                     (eid,))
        conn.commit()

    def test_disabled_entity_excluded_from_id_name_and_alias(self):
        conn = fixture_conn()
        r = EntityResolver(conn)
        self.assertEqual(r.resolve(cik="0000004904").entity_id, "E0005")
        self.assertEqual(r.resolve(ticker="AEP").entity_id, "E0005")
        self.assertEqual(r.resolve(name="American Electric Power").entity_id, "E0005")
        self.assertEqual(r.resolve(name="AEP").entity_id, "E0005")   # via alias

        self._disable(conn, "E0005")
        r2 = EntityResolver(conn)
        self.assertIsNone(r2.resolve(cik="0000004904").entity_id)
        self.assertIsNone(r2.resolve(ticker="AEP").entity_id)
        self.assertIsNone(r2.resolve(name="American Electric Power").entity_id)
        self.assertIsNone(r2.resolve(name="AEP").entity_id)          # alias dropped

    def test_active_sibling_still_resolves(self):
        conn = fixture_conn()
        self._disable(conn, "E0005")
        r = EntityResolver(conn)
        self.assertEqual(r.resolve(cik="0001004980").entity_id, "E0008")  # PG&E

    def test_disabled_entity_collision_term_dropped(self):
        conn = fixture_conn()
        # 'Dominion' is a collision term for E0004 -> a bare term yields review.
        self.assertEqual(EntityResolver(conn).resolve(name="Dominion").status,
                         "review")
        self._disable(conn, "E0004")
        res = EntityResolver(conn).resolve(name="Dominion")
        self.assertNotEqual(res.status, "matched")
        self.assertNotIn("E0004", [eid for eid, _ in res.candidates])


if __name__ == "__main__":
    unittest.main()
