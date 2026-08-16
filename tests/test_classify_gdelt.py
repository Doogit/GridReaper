"""GDELT silent-trial classifier tests (R9.4, R9.6, R6.3, R7.2, R4.1, R10.6).

Hermetic: real migrations against in-memory SQLite, FK on, canned GDELT DOC API
article payloads (titles lifted verbatim from the 250 stored raw_events, with
GDELT's space-padded punctuation preserved). No network.

Two groups. The GRAMMAR tests pin the strict conjunction the silent trial was
measured on — entity term AND corporate-action predicate, with "deal" alone and
market-commentary headlines refused. The CONTAINMENT tests pin the thing that
makes this a *silent* trial: with the scopes the repo actually seeds, the
framework drops every GDELT candidate, and the classifier is absent from the
real pipeline chain. Those two are the tests a future session would trip over
before "fixing" the missing pipeline entry.
"""
import io
import json
import os
import sqlite3
import tempfile
import unittest

from app.classify import gdelt
from app.classify.runner import run_classifier
from app.db.load_seeds import TRIGGER_SCOPES
from app.db.migrate import apply_migrations

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINE = os.path.join(REPO_ROOT, "deploy", "ingest_pipeline.sh")

# Verbatim titles from the stored corpus.
MERGER = ("NextEra Energy to buy Dominion Energy , combining two of the "
          "country largest utilities")
MERGER_TXNM = "Public weighs in on $11 . 5 billion Blackstone - TXNM Energy merger"
BARE_NAMES = "NextEra - Dominion merger targets soaring AI - driven power demand"
FRANCHISE = "Clearwater Weighs City - Owned Electric Service as Duke Energy Deal Expires"
ANALYST = ("Dominion Energy Stock Soars on $67 Billion Deal to Be Acquired "
           "by NextEra")
LISTICLE = "3 NextEra Energy Dividend Stocks to Buy and Hold for the Next Decade"


def fixture_conn(scopes=None):
    """Store with the gdelt source policy, the watchlist entities the trial's
    fired set names, and ma_divestiture seeded with ``scopes`` (default: the
    empty allowed_scopes the repo really seeds)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO source_policies (source_id, name, evidence_rank) "
        "VALUES ('gdelt', 'GDELT 2.0 DOC API', 3)")
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        " decay_half_life_days, mvp_flag, evidence_quality, allowed_scopes) "
        "VALUES ('ma_divestiture', 'M&A / divestiture', 4, 300, 0, 'PC', ?)",
        (json.dumps(scopes) if scopes else "",))
    for entity_id, name in [("E0001", "NextEra Energy"),
                            ("E0004", "Dominion Energy"),
                            ("E0028", "TXNM Energy (PNM)"),
                            ("E0002", "Duke Energy")]:
        conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, subsector) "
            "VALUES (?, ?, 'iou_electric')", (entity_id, name))
    # R6.3: the bare name is a collision term, so it is never a query term and
    # never a match term here either.
    conn.execute("INSERT INTO entity_collision_terms (entity_id, term) "
                 "VALUES ('E0004', 'Dominion')")
    conn.commit()
    return conn


def add_article(conn, n, title, event_date="2026-05-18", **payload):
    url = f"https://example.test/gdelt/{n}"
    payload = {"title": title, "domain": "example.test", "language": "English",
               "seendate": "20260518T120000Z", "url": url, **payload}
    raw_event_id = f"gdelt:{url}"
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
        " payload, url, first_seen_at) VALUES (?, 'gdelt', ?, ?, ?, ?)",
        (raw_event_id, event_date, json.dumps(payload, sort_keys=True), url,
         f"2026-05-18T00:00:0{n}Z"))
    conn.commit()
    return raw_event_id


def classify(conn, title, **kw):
    raw_event_id = add_article(conn, kw.pop("n", 1), title, **kw)
    raw = conn.execute("SELECT * FROM raw_events WHERE raw_event_id = ?",
                       (raw_event_id,)).fetchone()
    return gdelt.classify_gdelt(conn, raw)


class GrammarTest(unittest.TestCase):
    """The strict conjunction the trial's precision number was measured on."""

    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def test_merger_emits_one_candidate_per_named_party(self):
        cands = classify(self.conn, MERGER)
        self.assertEqual([c["entity_name_hint"] for c in cands],
                         ["NextEra Energy", "Dominion Energy"])
        for c in cands:
            self.assertEqual(c["trigger_id"], "ma_divestiture")
            self.assertEqual(c["signal_scope"], "account")
            self.assertIsNone(c["entity_id"])       # the framework attributes
            self.assertEqual(c["confidence"], gdelt.GDELT_CONFIDENCE)
            self.assertEqual(c["evidence"], [{"text": MERGER,
                                              "locator": "title"}])
            self.assertIn(c["entity_name_hint"], c["headline"])
            # R10.5: an M&A signal is not an incident, so no tier is claimed.
            self.assertNotIn("incident_evidence_level", c)

    def test_acquisition_of_a_single_named_party(self):
        cands = classify(self.conn, MERGER_TXNM)
        self.assertEqual([c["entity_name_hint"] for c in cands],
                         ["TXNM Energy"])

    def test_deal_alone_is_not_a_predicate(self):
        # Franchise-renewal coverage: a watchlist name plus "Deal", no action.
        self.assertEqual(classify(self.conn, FRANCHISE), [])

    def test_market_commentary_is_vetoed(self):
        # Both carry a real predicate; both are refused for precision.
        self.assertEqual(classify(self.conn, ANALYST, n=1), [])
        self.assertEqual(classify(self.conn, LISTICLE, n=2), [])

    def test_bare_names_do_not_match(self):
        # "NextEra" is not a watchlist term and "Dominion" is a collision term
        # (R6.3), so a real merger headline naming neither in full is refused
        # rather than guessed at.
        self.assertEqual(classify(self.conn, BARE_NAMES), [])

    def test_untitled_and_malformed_payloads_yield_nothing(self):
        self.assertEqual(classify(self.conn, "", n=1), [])
        raw_event_id = add_article(self.conn, 2, MERGER)
        self.conn.execute("UPDATE raw_events SET payload = 'not json' "
                          "WHERE raw_event_id = ?", (raw_event_id,))
        raw = self.conn.execute(
            "SELECT * FROM raw_events WHERE raw_event_id = ?",
            (raw_event_id,)).fetchone()
        self.assertEqual(gdelt.classify_gdelt(self.conn, raw), [])

    def test_headline_is_capped(self):
        long_title = MERGER + " " + "x" * 200
        cands = classify(self.conn, long_title)
        self.assertTrue(cands)
        for c in cands:
            self.assertLessEqual(len(c["headline"]),
                                 gdelt.MAX_HEADLINE_CHARS)

    def test_empty_watchlist_matches_nothing(self):
        # An empty alternation would otherwise match at every position.
        self.assertIsNone(gdelt._terms_pattern([]))
        self.assertEqual(gdelt.matched_terms(MERGER, None), [])


class ContainmentTest(unittest.TestCase):
    """Silent means silent: no seeded scope, no pipeline entry, no writes."""

    def test_seeded_scopes_drop_every_candidate(self):
        # THE trial invariant. ma_divestiture has no allowed_scopes in the
        # seeds, so the framework refuses every GDELT candidate (R7.2) even if
        # someone hand-runs the classifier over the real store.
        self.assertNotIn("ma_divestiture", TRIGGER_SCOPES)
        conn = fixture_conn()
        self.addCleanup(conn.close)
        add_article(conn, 1, MERGER)
        summary = run_classifier(conn, gdelt.CLASSIFIER_ID, "gdelt",
                                 gdelt.classify_gdelt, gdelt.PARSER_VERSION)
        self.assertEqual(summary["signals_new"], 0)
        self.assertEqual(summary["dropped_scope"], 2)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 0)

    def test_candidates_are_framework_shaped_when_scopes_are_granted(self):
        # Not a promotion: proves the candidate dict would survive resolution,
        # rollup and the R10.6 provenance guard, so the measured precision is a
        # precision over real cards and not over dicts the framework would
        # reject for unrelated reasons.
        conn = fixture_conn(scopes=["account"])
        self.addCleanup(conn.close)
        add_article(conn, 1, MERGER)
        summary = run_classifier(conn, gdelt.CLASSIFIER_ID, "gdelt",
                                 gdelt.classify_gdelt, gdelt.PARSER_VERSION)
        self.assertEqual(summary["signals_new"], 2)
        self.assertEqual(summary["quarantined"], 0)
        self.assertEqual(summary["review_enqueued"], 0)
        rows = conn.execute(
            "SELECT entity_id, signal_scope, evidence_snippet FROM signals "
            "ORDER BY entity_id").fetchall()
        self.assertEqual([r["entity_id"] for r in rows], ["E0001", "E0004"])
        for r in rows:
            self.assertEqual(r["signal_scope"], "account")
            self.assertEqual(r["evidence_snippet"], MERGER)

    def test_classifier_is_absent_from_the_pipeline(self):
        # The whole point of the unit: a silent trial must not reach the feed.
        # deploy/ingest_pipeline.sh is the real chain (tests/test_packaging.py
        # pins its module list) - GDELT classification stays out of it until
        # the operator promotes the source.
        with open(PIPELINE, encoding="utf-8") as fh:
            script = fh.read()
        self.assertNotIn("app.classify.gdelt", script)

    def test_report_harness_reads_only(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        path = os.path.join(tmpdir.name, "trial.db")
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO source_policies (source_id, name, evidence_rank) "
            "VALUES ('gdelt', 'GDELT 2.0 DOC API', 3)")
        for entity_id, name in [("E0001", "NextEra Energy"),
                                ("E0004", "Dominion Energy")]:
            conn.execute(
                "INSERT INTO watchlist_entities (entity_id, name, subsector) "
                "VALUES (?, ?, 'iou_electric')", (entity_id, name))
        add_article(conn, 1, MERGER)
        add_article(conn, 2, FRANCHISE)
        conn.close()

        out = io.StringIO()
        fired = gdelt.report(path, out=out)
        self.assertEqual(len(fired), 2)
        self.assertIn("raw_events=2 events_fired=1 candidates=2",
                      out.getvalue())

        after = sqlite3.connect(path)
        try:
            self.assertEqual(
                after.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 0)
            self.assertEqual(
                after.execute(
                    "SELECT COUNT(*) FROM classified_events").fetchone()[0], 0)
        finally:
            after.close()


if __name__ == "__main__":
    unittest.main()
