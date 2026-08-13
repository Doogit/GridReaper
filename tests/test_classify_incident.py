"""Incident classifier tests: SEC 8-K Item 1.05 (R9.6, R10.5, R7.12, R4.1).

Hermetic: real migrations against in-memory SQLite, FK on, canned EDGAR
submissions payloads mirroring the stored shape, run end-to-end through the
classification framework (run_classifier). Covers the own+peer fan-out, item
gating, form gating, evidence-tier/outreach wiring, name-free peer headlines,
disabled-filer handling, and a real second-pass idempotence check.
"""
import json
import sqlite3
import unittest

from app.classify import incident
from app.classify.runner import run_classifier
from app.db.migrate import apply_migrations

EDGAR = incident.SOURCE_ID


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO source_policies (source_id, name, evidence_rank) "
        "VALUES (?, 'SEC EDGAR submissions API', 1)", (EDGAR,))
    for trigger_id, scopes, strength, hl in [
            ("own_incident", ["account"], 5, 270),
            ("peer_incident", ["sector"], 3, 135)]:
        conn.execute(
            "INSERT INTO triggers (trigger_id, name, base_strength, "
            " decay_half_life_days, mvp_flag, evidence_quality, "
            " allowed_scopes) VALUES (?, ?, ?, ?, 0, 'PC', ?)",
            (trigger_id, trigger_id, strength, hl, json.dumps(scopes)))
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, cik, subsector) "
        "VALUES ('E0001', 'NextEra Energy', '0000753308', 'Electric Utility')")
    conn.commit()
    return conn


def add_edgar_event(conn, n, **overrides):
    payload = {
        "accessionNumber": f"0000753308-26-00006{n}", "cik": "0000753308",
        "entity_id": "E0001", "filingDate": "2026-08-11", "form": "8-K",
        "items": ["1.05"], "primaryDocDescription": "8-K",
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


class IncidentTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def run_edgar(self):
        return run_classifier(self.conn, incident.CLASSIFIER_ID, EDGAR,
                              incident.classify_edgar_incident,
                              incident.PARSER_VERSION)

    def signals(self):
        return self.conn.execute(
            "SELECT * FROM signals ORDER BY signal_id").fetchall()


class TestPositive(IncidentTestCase):
    def test_105_yields_own_and_peer(self):
        add_edgar_event(self.conn, 1, items=["1.05", "9.01"])
        s = self.run_edgar()
        self.assertEqual((s["status"], s["signals_new"]), ("success", 2))
        by_trigger = {sig["trigger_id"]: sig for sig in self.signals()}
        self.assertEqual(set(by_trigger), {"own_incident", "peer_incident"})

        own = by_trigger["own_incident"]
        self.assertEqual(own["signal_id"], f"own_incident:{EDGAR}:1:E0001")
        self.assertEqual(own["entity_id"], "E0001")     # pre-attributed
        self.assertEqual(own["signal_scope"], "account")
        self.assertEqual(own["event_date"], "2026-08-10")   # reportDate
        self.assertEqual(own["incident_evidence_level"], "confirmed")
        self.assertEqual(own["customer_facing_allowed"], 1)  # R7.12
        self.assertAlmostEqual(own["confidence"], 0.9)

        peer = by_trigger["peer_incident"]
        self.assertEqual(peer["signal_id"], f"peer_incident:{EDGAR}:1:sector")
        self.assertIsNone(peer["entity_id"])
        self.assertEqual(peer["signal_scope"], "sector")
        self.assertEqual(peer["incident_evidence_level"], "confirmed")

    def test_peer_headline_is_name_free(self):
        """A sector card names no company and asserts no unsourced class label
        (R4.1); it is class-phrased and sector-wide."""
        add_edgar_event(self.conn, 1)
        self.run_edgar()
        peer = self.conn.execute(
            "SELECT headline FROM signals WHERE trigger_id = 'peer_incident'"
        ).fetchone()["headline"]
        self.assertNotIn("NextEra", peer)
        self.assertIn("Sector peer", peer)
        self.assertIn("Item 1.05", peer)

    def test_event_date_uses_filing_date_when_no_report_date(self):
        add_edgar_event(self.conn, 1, reportDate="")
        self.run_edgar()
        self.assertEqual(self.signals()[0]["event_date"], "2026-08-11")

    def test_event_date_falls_back_to_raw_event_date(self):
        add_edgar_event(self.conn, 1, reportDate="", filingDate="")
        self.run_edgar()
        # raw_events.event_date is '2026-08-11' (set by add_edgar_event)
        self.assertEqual(self.signals()[0]["event_date"], "2026-08-11")

    def test_doc_description_equal_to_form_is_skipped(self):
        """The desc de-dup guard covers 8-K/A too, not just the literal 8-K."""
        add_edgar_event(self.conn, 1, form="8-K/A",
                        primaryDocDescription="8-K/A")
        self.run_edgar()
        locators = [r["evidence_locator"] for r in self.conn.execute(
            "SELECT evidence_locator FROM signal_evidence WHERE signal_id = ?",
            (f"own_incident:{EDGAR}:1:E0001",))]
        self.assertEqual(locators, ["items"])

    def test_evidence_cites_the_filing(self):
        add_edgar_event(self.conn, 1)
        self.run_edgar()
        own = f"own_incident:{EDGAR}:1:E0001"
        ev = self.conn.execute(
            "SELECT * FROM signal_evidence WHERE signal_id = ?",
            (own,)).fetchall()
        self.assertEqual(len(ev), 1)              # default desc "8-K" skipped
        self.assertEqual(ev[0]["evidence_locator"], "items")
        self.assertEqual(ev[0]["evidence_rank"], 1)   # from source_policies
        self.assertIn("Item 1.05", ev[0]["evidence_text"])
        self.assertIn("Material Cybersecurity Incident", ev[0]["evidence_text"])
        self.assertIn("accession 0000753308-26-000061",
                      ev[0]["evidence_text"])
        self.assertEqual(ev[0]["extraction_version"], incident.PARSER_VERSION)

    def test_meaningful_doc_description_added_as_evidence(self):
        add_edgar_event(
            self.conn, 1,
            primaryDocDescription="Material Cybersecurity Incident")
        self.run_edgar()
        locators = sorted(r["evidence_locator"] for r in self.conn.execute(
            "SELECT evidence_locator FROM signal_evidence WHERE signal_id = ?",
            (f"own_incident:{EDGAR}:1:E0001",)))
        self.assertEqual(locators, ["items", "primaryDocDescription"])

    def test_8ka_amendment_fires(self):
        add_edgar_event(self.conn, 1, form="8-K/A")
        s = self.run_edgar()
        self.assertEqual(s["signals_new"], 2)
        self.assertIn("Form 8-K/A", self.conn.execute(
            "SELECT headline FROM signals WHERE trigger_id = 'own_incident'"
        ).fetchone()["headline"])


class TestNegative(IncidentTestCase):
    def test_8k_without_105_dropped(self):
        add_edgar_event(self.conn, 1, items=["2.02", "9.01"])
        s = self.run_edgar()
        self.assertEqual((s["events_processed"], s["signals_new"]), (1, 0))
        self.assertEqual(self.signals(), [])

    def test_non_8k_form_with_105_dropped(self):
        add_edgar_event(self.conn, 1, form="10-K")
        s = self.run_edgar()
        self.assertEqual(s["signals_new"], 0)

    def test_missing_entity_id_dropped(self):
        add_edgar_event(self.conn, 1, entity_id="")
        s = self.run_edgar()
        self.assertEqual((s["events_processed"], s["signals_new"]), (1, 0))
        self.assertEqual(self.signals(), [])

    def test_disabled_filer_drops_own_keeps_peer(self):
        """R8.7: a disabled filer mints no account card, but the class-level
        peer card still fires (it references no entity)."""
        self.conn.execute(
            "UPDATE watchlist_entities SET active = 0 WHERE entity_id='E0001'")
        self.conn.commit()
        add_edgar_event(self.conn, 1)
        s = self.run_edgar()
        self.assertEqual((s["signals_new"], s["dropped_no_entity"]), (1, 1))
        sigs = self.signals()
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["trigger_id"], "peer_incident")


class TestIdempotence(IncidentTestCase):
    def test_second_pass_emits_nothing_new(self):
        add_edgar_event(self.conn, 1)
        self.run_edgar()
        s2 = self.run_edgar()               # a real second pass
        self.assertEqual((s2["events_processed"], s2["signals_new"]), (0, 0))
        self.assertEqual(len(self.signals()), 2)

    def test_force_reprocesses_without_duplicating(self):
        add_edgar_event(self.conn, 1)
        self.run_edgar()
        s2 = run_classifier(self.conn, incident.CLASSIFIER_ID, EDGAR,
                            incident.classify_edgar_incident,
                            incident.PARSER_VERSION, force=True)
        self.assertEqual((s2["events_processed"], s2["signals_new"],
                          s2["signals_existing"]), (1, 0, 2))
        self.assertEqual(len(self.signals()), 2)


if __name__ == "__main__":
    unittest.main()
