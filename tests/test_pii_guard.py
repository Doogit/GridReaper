"""R10.6 evidence-provenance guard tests.

R10.6 allows executive names taken from public filings/press and forbids
person-level enrichment. The guard in ``app/classify/runner.py`` therefore
asserts PROVENANCE rather than name shape: every word of a candidate's headline
and evidence must come from the raw event it cites or from the classifier's own
source file. A name-shape heuristic was measured and rejected - over the real
store a person-name regex matches every non-peer card ("Virtualization
Reliability", "Order No"), so it cannot be a gate.

Covered here: a synthesized evidence text is quarantined to the review queue
while the run completes and its sibling signals still land; the same for a
synthesized headline; payload-verbatim and classifier-authored text pass; a real
leadership signal naming a CISO passes; quarantine is idempotent under --force;
the guard fails OPEN when it cannot read the classifier source; and it also
judges the text a version bump re-mints onto an ALREADY STORED card, leaving
the stored wording standing when the replacement is unprovenanced.

Hermetic: real migrations against in-memory SQLite, FK on, no network.
"""
import json
import sqlite3
import unittest
from datetime import datetime, timezone

from app.classify import leadership, runner
from app.classify.runner import run_classifier
from app.db.migrate import apply_migrations
from app.ui import data

SOURCE = "test_source"
PRN = "presswire_prnewswire"

PAYLOAD_TITLE = "Northwind Grid Operator files a corrected outage report"

# Assembled at runtime ON PURPOSE. The guard reads the source file of the module
# that defines the classifier - here, this test module - so a hard-coded
# violating word would be provenanced by this very file and pass. Splitting the
# literal keeps the assembled word out of the file's token set - which is
# exactly what a runtime-synthesized (LLM-summarized, enriched) evidence text
# looks like. Do not spell the joined word anywhere in this file.
SYNTHESIZED = "".join(["Verdan", "hollow"])

# A literal that IS in this file and NOT in any payload: classifier-authored
# text, which the guard must accept (this is how every name-free peer card is
# written).
AUTHORED = "Sector peer disclosed an incident - reported by the operator feed"


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    for source_id in (SOURCE, PRN):
        conn.execute(
            "INSERT INTO source_policies (source_id, name, evidence_rank) "
            "VALUES (?, ?, 2)", (source_id, source_id))
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        " decay_half_life_days, mvp_flag, evidence_quality, allowed_scopes) "
        "VALUES ('peer_incident', 'Peer incident', 3, 135, 1, 'PC', "
        " '[\"sector\"]')")
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        " decay_half_life_days, mvp_flag, evidence_quality, allowed_scopes) "
        "VALUES ('leadership_change', 'Leadership change', 4, 90, 1, 'PC', "
        " '[\"account\"]')")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name) "
        "VALUES ('EA1', 'Acme Utilities')")
    for i in (1, 2):
        conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
            " payload, url, first_seen_at) VALUES (?, ?, '2026-08-01', ?, ?, ?)",
            (f"{SOURCE}:{i}", SOURCE,
             json.dumps({"title": PAYLOAD_TITLE, "id": i}),
             f"https://example.test/{i}", f"2026-08-0{i}T00:00:00Z"))
    conn.commit()
    return conn


def peer_candidate(headline, evidence_text):
    return {"trigger_id": "peer_incident", "signal_scope": "sector",
            "entity_id": None, "entity_name_hint": None,
            "event_date": "2026-08-01", "headline": headline,
            "evidence": [{"text": evidence_text, "locator": "title"}],
            "confidence": 0.5}


def make_classifier(candidates_by_event):
    def classify(conn, raw):
        return candidates_by_event.get(raw["raw_event_id"], [])
    return classify


class GuardTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def run_fake(self, candidates_by_event, version="clf/1.0", **kwargs):
        return run_classifier(self.conn, "clf_test", SOURCE,
                              make_classifier(candidates_by_event),
                              version, **kwargs)

    def signals(self):
        return self.conn.execute(
            "SELECT * FROM signals ORDER BY signal_id").fetchall()

    def quarantine_rows(self):
        return self.conn.execute(
            "SELECT * FROM review_queue WHERE disposition = 'pending' "
            "AND reason LIKE 'provenance\\_guard%' ESCAPE '\\' "
            "ORDER BY rowid").fetchall()


class TestQuarantine(GuardTestCase):
    def test_synthesized_evidence_is_queued_and_the_run_continues(self):
        """The offending candidate is quarantined; its sibling still lands and
        the run reports success - a guard that can stop a cron-driven pipeline
        is worse than the risk it prevents."""
        s = self.run_fake({
            f"{SOURCE}:1": [peer_candidate(
                "Sector peer filed a corrected outage report",
                f"{SYNTHESIZED} Kilbride, the chief information security "
                f"officer, described the incident.")],
            f"{SOURCE}:2": [peer_candidate(
                "Sector peer filed a corrected outage report",
                PAYLOAD_TITLE)]})

        self.assertEqual((s["status"], s["events_processed"]), ("success", 2))
        self.assertEqual((s["quarantined"], s["signals_new"]), (1, 1))
        # Only the clean sibling became a card.
        self.assertEqual([sig["raw_event_id"] for sig in self.signals()],
                         [f"{SOURCE}:2"])

        rows = self.quarantine_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["raw_event_id"], f"{SOURCE}:1")
        self.assertIsNone(rows[0]["candidate_entity_id"])
        self.assertIn("evidence[0]", rows[0]["reason"])
        self.assertIn(SYNTHESIZED.lower(), rows[0]["reason"])

    def test_synthesized_headline_is_quarantined_too(self):
        """The headline is written by the same INSERT and rendered more
        prominently than the evidence, so it is in scope."""
        s = self.run_fake({
            f"{SOURCE}:1": [peer_candidate(
                f"Sector peer {SYNTHESIZED} disclosed an incident",
                PAYLOAD_TITLE)]})
        self.assertEqual((s["quarantined"], s["signals_new"]), (1, 0))
        self.assertEqual(self.signals(), [])
        self.assertIn("headline", self.quarantine_rows()[0]["reason"])

    def test_quarantine_is_idempotent_under_force(self):
        cands = {f"{SOURCE}:1": [peer_candidate(
            "Sector peer filed a corrected outage report",
            f"{SYNTHESIZED} described the incident.")]}
        self.run_fake(cands)
        self.run_fake(cands, force=True)
        self.assertEqual(len(self.quarantine_rows()), 1)

    def test_quarantine_row_can_be_disposed_without_an_entity_id(self):
        cands = {f"{SOURCE}:1": [peer_candidate(
            "Sector peer filed a corrected outage report",
            f"{SYNTHESIZED} described the incident.")]}
        self.run_fake(cands)

        data.triage_decision(
            self.conn, f"{SOURCE}:1", None, accept=False,
            now=datetime(2026, 8, 2, tzinfo=timezone.utc))

        self.assertEqual(self.quarantine_rows(), [])
        row = self.conn.execute(
            "SELECT disposition, disposed_at FROM review_queue "
            "WHERE raw_event_id = ?", (f"{SOURCE}:1",)).fetchone()
        self.assertEqual(row["disposition"], "rejected")
        self.assertEqual(row["disposed_at"], "2026-08-02T00:00:00+00:00")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM entity_match_decisions").fetchone()[0], 0)


class TestRefreshIsGuarded(GuardTestCase):
    """A version bump re-mints the TEXT of a card the rule still emits, so the
    guard runs on that rewrite too. Before the refresh path, a stored card was
    returned on its signal_id before the guard and never judged - which is why
    cards minted before the guard existed had never been checked at all."""

    def test_unprovenanced_replacement_leaves_the_stored_text_standing(self):
        """The old wording is worse than the new one; unprovenanced text is
        worse than both. The card survives - it is still emitted, so it is not
        retracted either."""
        self.run_fake({f"{SOURCE}:1": [peer_candidate(
            "Sector peer filed a corrected outage report", PAYLOAD_TITLE)]})
        stored = self.signals()[0]

        s = self.run_fake({f"{SOURCE}:1": [peer_candidate(
            f"Sector peer {SYNTHESIZED} disclosed an incident",
            PAYLOAD_TITLE)]}, version="clf/1.1")

        self.assertEqual((s["quarantined"], s["signals_refreshed"]), (1, 0))
        self.assertEqual(s["signals_retracted"], 0)
        now = self.signals()[0]
        self.assertEqual(now["headline"], stored["headline"])
        self.assertEqual(now["status"], "active")
        rows = self.quarantine_rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("headline", rows[0]["reason"])
        self.assertIn(SYNTHESIZED.lower(), rows[0]["reason"])
        # Stale text keeps the R3.7 provenance drift visible on the card.
        self.assertEqual(self.conn.execute(
            "SELECT DISTINCT extraction_version FROM signal_evidence "
            "WHERE signal_id = ?", (now["signal_id"],)).fetchone()[0],
            "clf/1.0")

    def test_a_provenanced_rewording_reaches_the_stored_card(self):
        self.run_fake({f"{SOURCE}:1": [peer_candidate(
            "Sector peer filed a corrected outage report", PAYLOAD_TITLE)]})
        s = self.run_fake({f"{SOURCE}:1": [peer_candidate(
            AUTHORED, PAYLOAD_TITLE)]}, version="clf/1.1")
        self.assertEqual((s["quarantined"], s["signals_refreshed"]), (0, 1))
        self.assertEqual(self.signals()[0]["headline"], AUTHORED)


class TestProvenancedTextPasses(GuardTestCase):
    def test_payload_verbatim_text_passes(self):
        """Built from the payload at runtime, so it is not a literal in this
        file - it passes on provenance alone."""
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM raw_events WHERE raw_event_id = ?",
            (f"{SOURCE}:1",)).fetchone()["payload"])
        s = self.run_fake({f"{SOURCE}:1": [peer_candidate(
            payload["title"], payload["title"])]})
        self.assertEqual((s["signals_new"], s["quarantined"]), (1, 0))

    def test_classifier_authored_text_passes(self):
        """AUTHORED appears nowhere in the payload; it is accepted because it is
        a literal in the classifier's own source. This is what keeps every
        name-free peer card (synthesized by design, but written in the diff)
        out of the queue - and it proves the guard really loads the source
        rather than silently failing open."""
        self.assertNotIn("disclosed", PAYLOAD_TITLE)
        s = self.run_fake({f"{SOURCE}:1": [peer_candidate(AUTHORED, AUTHORED)]})
        self.assertEqual((s["signals_new"], s["quarantined"]), (1, 0))
        self.assertEqual(self.quarantine_rows(), [])

    def test_truncated_final_word_passes(self):
        """Classifiers cap long text (the 140-char headline cap, the page-diff
        snippet cap). A word cut in half by that cap is still sourced text -
        without the prefix carve-out every truncated regulatory headline would
        quarantine itself (measured: 2 of the 104 stored signals)."""
        cut = PAYLOAD_TITLE[:len(PAYLOAD_TITLE) - 3] + "…"
        self.assertTrue(cut.endswith("rep…"))
        s = self.run_fake({f"{SOURCE}:1": [peer_candidate(cut, cut)]})
        self.assertEqual((s["signals_new"], s["quarantined"]), (1, 0))

    def test_leadership_signal_naming_a_ciso_passes(self):
        """R10.6 permits executive names carried by public press: the real
        leadership classifier quotes the wire title verbatim, so the guard
        passes it."""
        self.conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
            " payload, url, first_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
            (f"{PRN}:1", PRN, "2026-08-11",
             json.dumps({"title": "Acme Utilities Appoints Jane Doe as Chief "
                                  "Information Security Officer",
                         "description": ""}),
             "https://example.test/prn/1", "2026-08-11T00:00:00Z"))
        self.conn.commit()
        s = run_classifier(self.conn, leadership.CLASSIFIER_ID, PRN,
                           leadership.classify_presswire,
                           leadership.PARSER_VERSION)
        self.assertEqual((s["signals_new"], s["quarantined"]), (1, 0))
        self.assertIn("Jane Doe", self.signals()[0]["headline"])
        self.assertEqual(self.quarantine_rows(), [])


class TestFailsOpen(unittest.TestCase):
    """A guard that cannot load its baseline must not quarantine everything."""

    def test_authored_tokens_is_none_without_a_readable_source(self):
        def orphan(conn, raw):
            return []
        orphan.__module__ = "no.such.module"
        self.assertIsNone(runner.authored_tokens(orphan))

    def test_none_vocabulary_disables_the_check(self):
        self.assertIsNone(runner.provenance_vocabulary({"payload": "{}",
                                                        "url": ""}, None))
        self.assertIsNone(runner.unprovenanced_text(
            peer_candidate("anything at all", "anything at all"),
            [{"text": "anything at all"}], None))


if __name__ == "__main__":
    unittest.main()
