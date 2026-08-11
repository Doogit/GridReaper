"""Leadership-change classifier tests (R7.1, R7.2, R4.1, R6.2, R6.3).

Hermetic: real migrations against in-memory SQLite, FK on, canned payloads
mirroring real source shapes, run end-to-end through the classification
framework (run_classifier). Includes the adversarial set: CFO appointment,
director-retirement 8-K, off-watchlist CISO, collision-term company name,
bare-CSO ambiguity, and former-CISO-hired-as-CFO.
"""
import json
import sqlite3
import unittest

from app.classify import leadership
from app.classify.runner import run_classifier
from app.db.migrate import apply_migrations

EDGAR = "sec_edgar_submissions"
PRN = "presswire_prnewswire"
GNW = "presswire_globenewswire"


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    for source_id, name, rank in [
            (EDGAR, "SEC EDGAR submissions API", 1),
            (PRN, "PR Newswire RSS", 2),
            (GNW, "GlobeNewswire RSS", 2)]:
        conn.execute(
            "INSERT INTO source_policies (source_id, name, evidence_rank) "
            "VALUES (?, ?, ?)", (source_id, name, rank))
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        " decay_half_life_days, mvp_flag, evidence_quality, allowed_scopes) "
        "VALUES ('leadership_change', 'Leadership change', 4, 90, 1, 'PC', "
        " '[\"account\"]')")
    for eid, name, cik in [("E0001", "NextEra Energy", "0000753308"),
                           ("EA1", "Acme Utilities", ""),
                           ("E0004", "Dominion Energy", "0000715957")]:
        conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, cik) "
            "VALUES (?, ?, ?)", (eid, name, cik))
    # "Dominion" bare is a collision term (R6.3) - mirrors the real seed
    conn.execute(
        "INSERT INTO entity_collision_terms (entity_id, term) "
        "VALUES ('E0004', 'Dominion')")
    conn.commit()
    return conn


def add_press_event(conn, source_id, n, title, description="",
                    event_date="2026-08-11T22:09:00+00:00"):
    raw_event_id = f"{source_id}:{n}"
    payload = json.dumps({
        "title": title, "description": description,
        "link": f"https://example.test/{n}",
        "guid": f"https://example.test/{n}",
        "pubDate": "Tue, 11 Aug 2026 22:09:00 +0000", "categories": []})
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
        " payload, url, first_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
        (raw_event_id, source_id, event_date, payload,
         f"https://example.test/{n}", f"2026-08-11T00:00:0{n}Z"))
    conn.commit()
    return raw_event_id


def add_edgar_event(conn, n, **overrides):
    payload = {
        "accessionNumber": f"0000753308-26-00006{n}", "cik": "0000753308",
        "entity_id": "E0001", "filingDate": "2026-08-11", "form": "8-K",
        "items": ["5.02"], "primaryDocDescription": "8-K",
        "primaryDocument": f"nee-2026081{n}.htm", "reportDate": "2026-08-10"}
    payload.update(overrides)
    raw_event_id = f"{EDGAR}:{n}"
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
        " payload, url, first_seen_at) VALUES (?, ?, '2026-08-11', ?, "
        " 'https://www.sec.gov/Archives/x.htm', ?)",
        (raw_event_id, EDGAR, json.dumps(payload, sort_keys=True),
         f"2026-08-11T00:00:0{n}Z"))
    conn.commit()
    return raw_event_id


class LeadershipTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def run_press(self, source_id=PRN):
        return run_classifier(self.conn, leadership.CLASSIFIER_ID, source_id,
                              leadership.classify_presswire,
                              leadership.PARSER_VERSION)

    def run_edgar(self):
        return run_classifier(self.conn, leadership.CLASSIFIER_ID, EDGAR,
                              leadership.classify_edgar,
                              leadership.PARSER_VERSION)

    def signals(self):
        return self.conn.execute(
            "SELECT * FROM signals ORDER BY signal_id").fetchall()

    def review_rows(self):
        return self.conn.execute(
            "SELECT * FROM review_queue ORDER BY candidate_entity_id"
        ).fetchall()


class TestPresswire(LeadershipTestCase):
    def test_positive_ciso_appointment_resolves_to_signal(self):
        add_press_event(self.conn, PRN, 1,
                        "Acme Utilities Appoints Jane Doe as CISO")
        s = self.run_press()
        self.assertEqual((s["status"], s["signals_new"]), ("success", 1))
        sig = self.signals()[0]
        self.assertEqual(sig["signal_id"], f"leadership_change:{PRN}:1:EA1")
        self.assertEqual(sig["entity_id"], "EA1")
        self.assertEqual(sig["signal_scope"], "account")
        self.assertEqual(sig["trigger_id"], "leadership_change")
        self.assertEqual(sig["event_date"], "2026-08-11T22:09:00+00:00")
        self.assertEqual(sig["evidence_quality"], "PC")
        self.assertAlmostEqual(sig["confidence"], 0.9)
        ev = self.conn.execute("SELECT * FROM signal_evidence").fetchall()
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["evidence_locator"], "title")
        self.assertEqual(ev[0]["evidence_rank"], 2)
        self.assertEqual(ev[0]["extraction_version"],
                         leadership.PARSER_VERSION)
        self.assertEqual(self.review_rows(), [])

    def test_globenewswire_same_rules(self):
        add_press_event(
            self.conn, GNW, 1,
            "Acme Utilities Names Maria Ruiz Chief Information "
            "Security Officer")
        s = self.run_press(GNW)
        self.assertEqual(s["signals_new"], 1)
        self.assertEqual(self.signals()[0]["entity_id"], "EA1")

    def test_joins_as_pattern(self):
        add_press_event(
            self.conn, PRN, 1,
            "Jane Doe Joins Acme Utilities as Chief Information "
            "Security Officer")
        s = self.run_press()
        self.assertEqual(s["signals_new"], 1)
        self.assertEqual(self.signals()[0]["entity_id"], "EA1")

    def test_cio_gets_lower_confidence(self):
        add_press_event(self.conn, PRN, 1,
                        "Acme Utilities Names Maria Ruiz as CIO")
        s = self.run_press()
        self.assertEqual(s["signals_new"], 1)
        self.assertAlmostEqual(self.signals()[0]["confidence"], 0.75)

    def test_cfo_appointment_is_not_a_signal(self):
        add_press_event(
            self.conn, PRN, 1,
            "Acme Utilities Appoints Jane Doe as Chief Financial Officer")
        s = self.run_press()
        self.assertEqual((s["events_processed"], s["signals_new"]), (1, 0))
        self.assertEqual(self.signals(), [])
        self.assertEqual(self.review_rows(), [])

    def test_former_ciso_hired_as_cfo_is_not_a_signal(self):
        add_press_event(
            self.conn, PRN, 1,
            "Jane Doe, Former CISO of MegaCorp, Joins Acme Utilities as "
            "Chief Financial Officer")
        s = self.run_press()
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(self.signals(), [])

    def test_offlist_company_yields_nothing(self):
        """CISO appointment for a non-watchlist company: resolution 'none',
        no signal AND nothing enqueued for review."""
        add_press_event(self.conn, PRN, 1,
                        "Quantumline Robotics Appoints Bob Roe as CISO")
        s = self.run_press()
        self.assertEqual((s["signals_new"], s["review_enqueued"],
                          s["dropped_no_entity"]), (0, 0, 1))
        self.assertEqual(self.signals(), [])
        self.assertEqual(self.review_rows(), [])

    def test_collision_bare_name_goes_to_review_not_signal(self):
        """R6.2/R6.3: bare 'Dominion' is a collision term - review_queue,
        never an auto-fired signal."""
        add_press_event(self.conn, PRN, 1,
                        "Dominion Appoints Jane Doe as CISO")
        s = self.run_press()
        self.assertEqual((s["signals_new"], s["review_enqueued"]), (0, 1))
        self.assertEqual(self.signals(), [])
        rq = self.review_rows()
        self.assertEqual([r["candidate_entity_id"] for r in rq], ["E0004"])
        self.assertEqual(rq[0]["disposition"], "pending")

    def test_bare_cso_without_security_context_is_dropped(self):
        add_press_event(
            self.conn, PRN, 1,
            "Acme Utilities Appoints Jane Doe as CSO",
            description="<p>Jane Doe will lead sustainability strategy "
                        "across the company's renewable portfolio.</p>")
        s = self.run_press()
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(self.signals(), [])

    def test_bare_cso_with_security_context_fires_with_desc_evidence(self):
        add_press_event(
            self.conn, PRN, 1,
            "Acme Utilities Appoints Jane Doe as CSO",
            description="<p>Jane Doe brings 20 years of information "
                        "security leadership. PHOENIX, Aug. 11, 2026 "
                        "/PRNewswire/ -- Acme Utilities...</p>")
        s = self.run_press()
        self.assertEqual(s["signals_new"], 1)
        self.assertAlmostEqual(self.signals()[0]["confidence"], 0.9)
        locators = [r["evidence_locator"] for r in self.conn.execute(
            "SELECT evidence_locator FROM signal_evidence")]
        self.assertEqual(sorted(locators), ["description", "title"])

    def test_no_appointment_verb_is_dropped(self):
        add_press_event(
            self.conn, PRN, 1,
            "Acme Utilities CISO to Speak at Grid Security Conference")
        s = self.run_press()
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(self.signals(), [])


class TestEdgar(LeadershipTestCase):
    def test_positive_502_with_title_token_preattributed(self):
        add_edgar_event(
            self.conn, 1, items=["5.02", "9.01"],
            primaryDocDescription="Appointment of Chief Information "
                                  "Security Officer")
        s = self.run_edgar()
        self.assertEqual(s["signals_new"], 1)
        sig = self.signals()[0]
        self.assertEqual(sig["signal_id"], f"leadership_change:{EDGAR}:1:E0001")
        self.assertEqual(sig["entity_id"], "E0001")   # pre-attributed
        self.assertEqual(sig["event_date"], "2026-08-10")   # reportDate
        self.assertAlmostEqual(sig["confidence"], 0.9)
        ev = self.conn.execute("SELECT * FROM signal_evidence").fetchall()
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["evidence_locator"], "primaryDocDescription")
        self.assertEqual(ev[0]["evidence_rank"], 1)
        # no name resolution happened on this path
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM entity_match_decisions").fetchone()[0], 0)

    def test_director_retirement_502_without_title_token_dropped(self):
        add_edgar_event(
            self.conn, 1,
            primaryDocDescription="Departure of Directors; Retirement of "
                                  "Board Member")
        s = self.run_edgar()
        self.assertEqual((s["events_processed"], s["signals_new"]), (1, 0))
        self.assertEqual(self.signals(), [])

    def test_bare_502_default_description_dropped(self):
        """The realistic case: primaryDocDescription is just '8-K'."""
        add_edgar_event(self.conn, 1)
        s = self.run_edgar()
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(self.signals(), [])

    def test_title_token_without_item_502_dropped(self):
        add_edgar_event(
            self.conn, 1, items=["8.01"],
            primaryDocDescription="Update on Chief Information Security "
                                  "Officer transition")
        s = self.run_edgar()
        self.assertEqual(s["signals_new"], 0)

    def test_non_8k_form_dropped(self):
        add_edgar_event(
            self.conn, 1, form="10-K", items=["5.02"],
            primaryDocDescription="Chief Information Security Officer")
        s = self.run_edgar()
        self.assertEqual(s["signals_new"], 0)


if __name__ == "__main__":
    unittest.main()
