"""Regulatory classifier tests (R4.1, R7.2, R8.4, R9.4).

Hermetic: real migrations against in-memory SQLite, FK on, canned Federal
Register payloads and synthetic NERC page snapshots run through the real
framework (run_classifier). Focus is rule precision: the per-trigger accept
rules, the durable-clock anchor gate, the named-entity enforcement path,
and token-level page diffing. Idempotency/bookkeeping mechanics are covered
by tests/test_classify_runner.py.
"""
import json
import sqlite3
import unittest

from app.classify.regulatory import (PARSER_VERSION,
                                     classify_federal_register,
                                     classify_nerc_pages)
from app.classify.runner import run_classifier
from app.db.migrate import apply_migrations

FERC_AGENCY = {"raw_name": "Federal Energy Regulatory Commission",
               "name": "Federal Energy Regulatory Commission",
               "id": 167, "slug": "federal-energy-regulatory-commission"}
TSA_AGENCY = {"raw_name": "TRANSPORTATION SECURITY ADMINISTRATION",
              "name": "Transportation Security Administration",
              "slug": "transportation-security-administration"}
DOE_AGENCY = {"raw_name": "DEPARTMENT OF ENERGY", "name": "Energy Department",
              "id": 136}    # deliberately slug-less

TRIGGERS = {
    "nerc_cip_revision": ["sector", "regulatory_calendar"],
    "tsa_security_directive": ["sector", "regulatory_calendar"],
    "nerc_enforcement": ["account", "sector"],
}

PAGE_URL = "https://www.nerc.com/pa/Stand/Pages/CIPStandards.aspx"
OTHER_PAGE_URL = ("https://www.nerc.com/pa/Stand/Pages/"
                  "Standards-Under-Development.aspx")

OLD_PAGE_TEXT = ("CIP Standards Currently Enforced CIP-002-5.1a BES Cyber "
                 "System Categorization Approved CIP-013-1 Supply Chain "
                 "Risk Management Approved Filed with FERC")
NEW_PAGE_TEXT = OLD_PAGE_TEXT.replace(
    "CIP-013-1 Supply Chain Risk Management Approved",
    "CIP-013-2 Supply Chain Risk Management Posted for Ballot")


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    for source_id in ("federal_register", "nerc_pages"):
        conn.execute(
            "INSERT INTO source_policies (source_id, name, evidence_rank) "
            "VALUES (?, ?, 1)", (source_id, source_id))
    for trigger_id, scopes in TRIGGERS.items():
        conn.execute(
            "INSERT INTO triggers (trigger_id, name, base_strength, "
            " decay_half_life_days, mvp_flag, evidence_quality, "
            " allowed_scopes) VALUES (?, ?, 5, 600, 1, 'IR', ?)",
            (trigger_id, trigger_id, json.dumps(scopes)))
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name) "
        "VALUES ('ACME1', 'Acme Utilities')")
    conn.commit()
    return conn


def fr_doc(**overrides):
    doc = {
        "document_number": "2026-00001",
        "title": "", "type": "Notice", "abstract": None,
        "publication_date": "2026-08-11",
        "agencies": [FERC_AGENCY], "agency_names": [],
        "docket_ids": [], "regulation_id_numbers": [], "cfr_references": [],
        "effective_on": None, "comments_close_on": None, "significant": None,
        "html_url": "https://www.federalregister.gov/documents/x",
    }
    doc.update(overrides)
    return doc


def add_fr_event(conn, i, doc):
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
        " payload, url, first_seen_at) "
        "VALUES (?, 'federal_register', ?, ?, ?, ?)",
        (f"federal_register:{i}", doc.get("publication_date", ""),
         json.dumps(doc), doc.get("html_url", ""),
         f"2026-08-01T00:00:0{i}Z"))
    conn.commit()


def add_page_event(conn, i, text, first_seen, fetched, url=PAGE_URL):
    payload = {"page_url": url, "fetched_date": fetched, "text": text}
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
        " payload, url, first_seen_at) VALUES (?, 'nerc_pages', ?, ?, ?, ?)",
        (f"nerc_pages:{i}", fetched, json.dumps(payload), url, first_seen))
    conn.commit()


class TestFederalRegister(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def run_fr(self):
        return run_classifier(self.conn, "regulatory", "federal_register",
                              classify_federal_register, PARSER_VERSION)

    def signals(self):
        return self.conn.execute(
            "SELECT * FROM signals ORDER BY signal_id").fetchall()

    def test_tsa_security_directive_with_effective_date_is_sector(self):
        add_fr_event(self.conn, 1, fr_doc(
            type="Rule", agencies=[TSA_AGENCY, DOE_AGENCY],
            title="Enhancing Surface Cyber Risk Management",
            abstract=("TSA is codifying security directive requirements "
                      "for pipeline and rail owner/operators. "
                      "Owner/operators must establish cybersecurity "
                      "requirements and report incidents."),
            effective_on="2026-10-01"))
        s = self.run_fr()
        self.assertEqual((s["status"], s["signals_new"]), ("success", 1))
        sig = self.signals()[0]
        self.assertEqual(sig["trigger_id"], "tsa_security_directive")
        self.assertEqual(sig["signal_scope"], "sector")
        self.assertIsNone(sig["entity_id"])
        self.assertEqual(sig["event_date"], "2026-08-11")
        locators = {r["evidence_locator"] for r in self.conn.execute(
            "SELECT evidence_locator FROM signal_evidence "
            "WHERE signal_id = ?", (sig["signal_id"],))}
        self.assertEqual(locators, {"title", "abstract", "effective_on"})

    def test_ferc_cip_nopr_is_regulatory_calendar(self):
        add_fr_event(self.conn, 1, fr_doc(
            type="Proposed Rule",
            title=("Critical Infrastructure Protection Reliability "
                   "Standard CIP-003-11 - Cyber Security - Security "
                   "Management Controls"),
            abstract=("The Commission proposes to approve Reliability "
                      "Standard CIP-003-11 submitted by NERC."),
            docket_ids=["RM26-7-000"],
            comments_close_on="2026-09-30"))
        s = self.run_fr()
        self.assertEqual(s["signals_new"], 1)
        sig = self.signals()[0]
        self.assertEqual(sig["trigger_id"], "nerc_cip_revision")
        self.assertEqual(sig["signal_scope"], "regulatory_calendar")
        self.assertIsNone(sig["entity_id"])
        self.assertIn("proposes", sig["headline"])

    def test_enforcement_notice_emits_sector_and_named_account(self):
        add_fr_event(self.conn, 1, fr_doc(
            type="Notice",
            title="Acme Utilities, Inc.; Notice of Proposed Civil Penalty",
            abstract=("The Office of Enforcement proposes a civil penalty "
                      "for violations of the Critical Infrastructure "
                      "Protection Reliability Standards. Comments are due "
                      "within 30 days."),
            comments_close_on="2026-09-10"))
        s = self.run_fr()
        self.assertEqual(s["signals_new"], 2)
        sigs = {sig["signal_scope"]: sig for sig in self.signals()}
        self.assertEqual(set(sigs), {"account", "sector"})
        self.assertEqual(sigs["sector"]["trigger_id"], "nerc_enforcement")
        self.assertIsNone(sigs["sector"]["entity_id"])
        self.assertNotIn("Acme", sigs["sector"]["headline"])
        self.assertEqual(sigs["account"]["entity_id"], "ACME1")
        self.assertEqual(sigs["account"]["trigger_id"], "nerc_enforcement")

    def test_routine_hydro_notice_no_signal(self):
        # The real document shape: FERC Notice, null abstract, no anchors.
        add_fr_event(self.conn, 1, fr_doc(
            document_number="2026-16327",
            title=("Marlow Hydro, LLC; Notice of Reasonable Period of Time "
                   "for Water Quality Certification Application"),
            type="Notice", agencies=[DOE_AGENCY, FERC_AGENCY],
            docket_ids=["Project No. 15331-001"]))
        s = self.run_fr()
        self.assertEqual((s["events_processed"], s["signals_new"]), (1, 0))
        self.assertEqual(self.signals(), [])

    def test_routine_rate_notice_with_anchor_no_signal(self):
        # Anchored but no enforcement/reliability terms -> still nothing.
        add_fr_event(self.conn, 1, fr_doc(
            type="Notice",
            title="PJM Interconnection, L.L.C.; Notice of Filing",
            abstract=("Take notice that PJM Interconnection submitted its "
                      "annual formula rate update."),
            comments_close_on="2026-09-01"))
        s = self.run_fr()
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(self.signals(), [])

    def test_reliability_doc_without_anchor_no_signal(self):
        # Durable-clock gate (R7.2/R8.4): matches every CIP-revision term
        # but has no effective date, comment deadline, or compliance
        # wording -> skipped.
        add_fr_event(self.conn, 1, fr_doc(
            type="Proposed Rule",
            title="Critical Infrastructure Protection Reliability Standards",
            abstract=("The Commission seeks comment on Reliability "
                      "Standard CIP-003-11."),
            docket_ids=["RM26-7-000"]))
        s = self.run_fr()
        self.assertEqual((s["events_processed"], s["signals_new"]), (1, 0))
        self.assertEqual(self.signals(), [])

    def test_tsa_non_security_doc_no_signal(self):
        add_fr_event(self.conn, 1, fr_doc(
            type="Rule", agencies=[TSA_AGENCY],
            title="Air Cargo Screening Program Fee Adjustment",
            abstract="TSA adjusts program fees for the coming fiscal year.",
            effective_on="2026-09-01"))
        s = self.run_fr()
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(self.signals(), [])

    def test_malformed_payload_yields_nothing(self):
        self.assertEqual(classify_federal_register(
            self.conn, {"payload": "not json", "event_date": ""}), [])


class TestNercPages(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def run_nerc(self):
        return run_classifier(self.conn, "regulatory", "nerc_pages",
                              classify_nerc_pages, PARSER_VERSION)

    def signals(self):
        return self.conn.execute(
            "SELECT * FROM signals ORDER BY signal_id").fetchall()

    def test_first_snapshot_no_signal(self):
        add_page_event(self.conn, 1, OLD_PAGE_TEXT,
                       "2026-08-01T00:00:00Z", "2026-08-01")
        s = self.run_nerc()
        self.assertEqual((s["events_processed"], s["signals_new"]), (1, 0))
        self.assertEqual(self.signals(), [])

    def test_changed_cip_standard_emits_one_sector_signal(self):
        # An earlier snapshot of a DIFFERENT page must not be used as the
        # diff baseline (pairing is by page_url).
        add_page_event(self.conn, 1, "Standards Under Development none",
                       "2026-08-01T00:00:00Z", "2026-08-01",
                       url=OTHER_PAGE_URL)
        add_page_event(self.conn, 2, OLD_PAGE_TEXT,
                       "2026-08-02T00:00:00Z", "2026-08-02")
        add_page_event(self.conn, 3, NEW_PAGE_TEXT,
                       "2026-08-10T00:00:00Z", "2026-08-10")
        s = self.run_nerc()
        self.assertEqual((s["events_processed"], s["signals_new"]), (3, 1))
        sig = self.signals()[0]
        self.assertEqual(sig["trigger_id"], "nerc_cip_revision")
        self.assertEqual(sig["signal_scope"], "sector")
        self.assertIsNone(sig["entity_id"])
        self.assertIn("CIP-013", sig["headline"])
        self.assertEqual(sig["event_date"], "2026-08-10")
        ev = self.conn.execute(
            "SELECT * FROM signal_evidence WHERE signal_id = ?",
            (sig["signal_id"],)).fetchall()
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["evidence_locator"], "page_diff")
        self.assertIn("CIP-013-2", ev[0]["evidence_text"])

    def test_unchanged_snapshot_no_signal(self):
        add_page_event(self.conn, 1, OLD_PAGE_TEXT,
                       "2026-08-01T00:00:00Z", "2026-08-01")
        add_page_event(self.conn, 2, OLD_PAGE_TEXT,
                       "2026-08-05T00:00:00Z", "2026-08-05")
        s = self.run_nerc()
        self.assertEqual((s["events_processed"], s["signals_new"]), (2, 0))
        self.assertEqual(self.signals(), [])


if __name__ == "__main__":
    unittest.main()
