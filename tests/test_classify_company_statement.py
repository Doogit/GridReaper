"""Company-statement incident classifier tests (R9.6, R10.5, R7.12, R4.1, R6.2).

Hermetic: real migrations against in-memory SQLite, FK on, canned press-wire
payloads mirroring the stored shape, run end-to-end through the classification
framework (run_classifier). Covers own-match (confirmed tier, outreach
allowed), the cyber-phrase and victim-verb precision gates (the fiduciary-duty
and vendor-report false positives), non-interference with the leadership
grammar, collision -> review-queue (no card), off-watchlist drop (no peer),
verbatim headline, and real second-pass / force idempotence.
"""
import json
import sqlite3
import unittest

from app.classify import company_statement as cs
from app.classify.runner import run_classifier
from app.db.migrate import apply_migrations

SRC = "presswire_prnewswire"


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO source_policies (source_id, name, evidence_rank) "
        "VALUES (?, 'PR Newswire RSS', 2)", (SRC,))
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        " decay_half_life_days, mvp_flag, evidence_quality, allowed_scopes) "
        "VALUES ('own_incident', 'own_incident', 5, 270, 0, 'PC', ?)",
        (json.dumps(["account"]),))
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector) "
        "VALUES ('E0001', 'NextEra Energy', 'Electric Utility')")
    # 'Dominion' is a bare collision term for a full name that never appears
    # verbatim, so a release naming only 'Dominion' can only go to review.
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector) "
        "VALUES ('E0002', 'Dominion Energy', 'Electric Utility')")
    conn.execute(
        "INSERT INTO entity_collision_terms (entity_id, term) "
        "VALUES ('E0002', 'Dominion')")
    conn.commit()
    return conn


def add_release(conn, n, title, description="", source_id=SRC):
    payload = {"title": title, "description": description,
               "url": f"https://newswire.example/{n}"}
    raw_event_id = f"{source_id}:{n}"
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
        " payload, url, first_seen_at) VALUES (?, ?, '2026-08-05', ?, ?, ?)",
        (raw_event_id, source_id, json.dumps(payload, sort_keys=True),
         payload["url"], f"2026-08-05T00:00:0{n}Z"))
    conn.commit()
    return raw_event_id


class CompanyStatementTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def run_it(self, source_id=SRC, **kw):
        return run_classifier(self.conn, cs.CLASSIFIER_ID, source_id,
                              cs.classify_presswire, cs.PARSER_VERSION, **kw)

    def signals(self):
        return self.conn.execute(
            "SELECT * FROM signals ORDER BY signal_id").fetchall()


class TestOwnMatch(CompanyStatementTestCase):
    def test_watchlist_breach_release_yields_one_confirmed_own_card(self):
        rid = add_release(
            self.conn, 1,
            "NextEra Energy Provides Update on Cybersecurity Incident")
        s = self.run_it()
        self.assertEqual((s["status"], s["signals_new"]), ("success", 1))
        sigs = self.signals()
        self.assertEqual(len(sigs), 1)                  # own only, no peer
        own = sigs[0]
        self.assertEqual(own["trigger_id"], "own_incident")
        self.assertEqual(own["signal_id"], f"own_incident:{rid}:E0001")
        self.assertEqual(own["entity_id"], "E0001")     # framework-resolved
        self.assertEqual(own["signal_scope"], "account")
        self.assertEqual(own["incident_evidence_level"], "confirmed")
        self.assertEqual(own["customer_facing_allowed"], 1)   # R7.12: allowed

    def test_headline_is_the_title_verbatim(self):
        """R4.1: the card quotes the release; no templated verb is added."""
        title = "Duke Energy Confirms Data Security Incident Affecting Customers"
        add_release(self.conn, 1, title)
        # Duke is off-watchlist here, so nothing fires - assert on a match
        # instead using a watchlist name.
        add_release(self.conn, 2,
                    "NextEra Energy Confirms Data Breach", source_id=SRC)
        self.run_it()
        own = self.signals()[0]
        self.assertEqual(own["headline"], "NextEra Energy Confirms Data Breach")

    def test_match_logs_a_decision(self):
        """R6.4: the framework logs the resolution the hint drove."""
        add_release(self.conn, 1,
                    "NextEra Energy Discloses Ransomware Attack")
        self.run_it()
        decided = self.conn.execute(
            "SELECT entity_id FROM entity_match_decisions").fetchall()
        self.assertEqual([r["entity_id"] for r in decided], ["E0001"])


class TestPrecisionGates(CompanyStatementTestCase):
    def test_fiduciary_breach_is_not_a_cyber_incident(self):
        """The backfill's real false positive: a securities-litigation
        'breaches of fiduciary duties' notice must never fire (bare 'breach'
        is excluded from the cyber-phrase set)."""
        add_release(
            self.conn, 1,
            "Rosen Law Firm Announces Investigation of Breaches of Fiduciary "
            "Duties by the Directors and Officers of NextEra Energy")
        s = self.run_it()
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(self.signals(), [])

    def test_vendor_report_release_does_not_fire(self):
        """A security vendor publishing a ransomware report is not that
        vendor's incident: 'releases'/'report' is not a victim verb."""
        add_release(self.conn, 1,
                    "NextEra Energy Releases 2026 Ransomware Threat Report")
        s = self.run_it()
        self.assertEqual(s["signals_new"], 0)

    def test_leadership_grammar_does_not_fire_here(self):
        """An appointment release (the leadership classifier's job) carries no
        cyber-incident phrase, so this classifier stays silent on it."""
        add_release(
            self.conn, 1,
            "NextEra Energy Appoints Jane Doe as Chief Information Security "
            "Officer")
        s = self.run_it()
        self.assertEqual(s["signals_new"], 0)

    def test_incident_phrase_before_the_verb_does_not_fire(self):
        """A company named for a cyber term ('... Cyberattack Defense ...')
        must not self-trigger: the phrase is required AFTER the victim verb."""
        add_release(self.conn, 1,
                    "Cyberattack Defense Inc Appoints New CEO")
        s = self.run_it()
        self.assertEqual(s["signals_new"], 0)


class TestAttribution(CompanyStatementTestCase):
    def test_offlist_company_is_dropped_no_peer(self):
        add_release(self.conn, 1,
                    "Obscure Regional Water Co Confirms Ransomware Attack")
        s = self.run_it()
        self.assertEqual((s["signals_new"], s["review_enqueued"]), (0, 0))
        self.assertEqual(self.signals(), [])

    def test_ambiguous_company_goes_to_review_no_card(self):
        """R6.2: a bare collision term never auto-fires; it queues for review
        and mints no card."""
        add_release(self.conn, 1, "Dominion Reports Cybersecurity Incident")
        s = self.run_it()
        self.assertEqual((s["signals_new"], s["review_enqueued"]), (0, 1))
        self.assertEqual(self.signals(), [])
        q = self.conn.execute(
            "SELECT candidate_entity_id FROM review_queue "
            "WHERE disposition = 'pending'").fetchall()
        self.assertIn("E0002", [r["candidate_entity_id"] for r in q])


class TestNegative(CompanyStatementTestCase):
    def test_empty_title_dropped(self):
        add_release(self.conn, 1, "")
        s = self.run_it()
        self.assertEqual((s["events_processed"], s["signals_new"]), (1, 0))

    def test_no_verb_no_fire(self):
        add_release(self.conn, 1, "NextEra Energy Cybersecurity Incident 2026")
        s = self.run_it()
        self.assertEqual(s["signals_new"], 0)


class TestIdempotence(CompanyStatementTestCase):
    def test_second_pass_emits_nothing_new(self):
        add_release(self.conn, 1,
                    "NextEra Energy Provides Update on Ransomware Attack")
        self.run_it()
        s2 = self.run_it()
        self.assertEqual((s2["events_processed"], s2["signals_new"]), (0, 0))
        self.assertEqual(len(self.signals()), 1)

    def test_force_reprocesses_without_new_signals(self):
        """--force re-runs the event but the deterministic signal_id means no
        new/duplicate card. (Decision-log dedupe under --force is a framework
        concern shared with the leadership path, not owned here.)"""
        add_release(self.conn, 1, "NextEra Energy Confirms Cyberattack")
        self.run_it()
        s2 = self.run_it(force=True)
        self.assertEqual((s2["events_processed"], s2["signals_new"],
                          s2["signals_existing"]), (1, 0, 1))
        self.assertEqual(len(self.signals()), 1)


if __name__ == "__main__":
    unittest.main()
