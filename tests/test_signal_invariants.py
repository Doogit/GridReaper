"""Cross-classifier R4.1 invariant tests (R4.1, R7.2).

R4.1 ("every signal MUST carry source_url, evidence_quality, confidence and
signal_scope; no untagged claims surface") is enforced by exactly one insert
path - ``app/classify/runner.py:_process_candidate``. The schema does not help:
``0001_initial.sql`` declares every one of those columns nullable with no CHECK,
so an empty ``source_url`` or headline would store silently. The per-classifier
test modules each assert their own semantics; nothing asserted the shared carry
contract across ALL of them.

This module does: it runs EVERY classifier over canned fixtures through the real
framework and asserts the contract on each emitted row.

NON-VACUITY. Each case asserts a minimum emitted-signal count BEFORE asserting
the per-signal invariants. Without that, a change that stops a classifier
emitting (e.g. retiring the security-press peer path) would leave a "for every
emitted signal ..." loop iterating over an empty set - green, and proving
nothing. A classifier that goes silent must turn this file RED and be
re-baselined deliberately.

Hermetic: real migrations against in-memory SQLite, FK on, canned payloads
mirroring the stored shapes, no network.
"""
import json
import sqlite3
import unittest

from app.classify import (company_statement, incident, leadership, ransomware,
                          regulatory, security_rss)
from app.classify.runner import run_classifier
from app.db.load_seeds import TRIGGER_SCOPES
from app.db.migrate import apply_migrations

EDGAR = "sec_edgar_submissions"
PRN = "presswire_prnewswire"
RANSOM = ransomware.SOURCE_ID
FR = "federal_register"
NERC = "nerc_pages"
RECORD = "the_record"
BLEEP = "bleepingcomputer"

SOURCE_RANKS = {EDGAR: 1, PRN: 2, RANSOM: 3, FR: 1, NERC: 1,
                RECORD: 2, BLEEP: 2}

# evidence_quality per seeds/triggers.csv (IR = independent regulator,
# PC = primary/company source).
TRIGGER_QUALITY = {"leadership_change": "PC", "nerc_enforcement": "IR",
                   "nerc_cip_revision": "IR", "tsa_security_directive": "IR",
                   "own_incident": "PC", "peer_incident": "PC",
                   "audit_consent_decree": "PC", "capital_project": "PC"}

# The R4.1 carry contract, as stored on `signals`.
R41_FIELDS = ("source_url", "headline", "evidence_snippet", "signal_scope",
              "trigger_id", "evidence_quality")

FERC_AGENCY = {"raw_name": "Federal Energy Regulatory Commission",
               "name": "Federal Energy Regulatory Commission",
               "id": 167, "slug": "federal-energy-regulatory-commission"}
TSA_AGENCY = {"raw_name": "TRANSPORTATION SECURITY ADMINISTRATION",
              "name": "Transportation Security Administration",
              "slug": "transportation-security-administration"}

PAGE_URL = "https://www.nerc.com/pa/Stand/Pages/CIPStandards.aspx"
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
    for source_id, rank in SOURCE_RANKS.items():
        conn.execute(
            "INSERT INTO source_policies (source_id, name, evidence_rank) "
            "VALUES (?, ?, ?)", (source_id, source_id, rank))
    for trigger_id, scopes in TRIGGER_SCOPES.items():
        conn.execute(
            "INSERT INTO triggers (trigger_id, name, base_strength, "
            " decay_half_life_days, mvp_flag, evidence_quality, "
            " allowed_scopes) VALUES (?, ?, 5, 270, 1, ?, ?)",
            (trigger_id, trigger_id, TRIGGER_QUALITY[trigger_id],
             json.dumps(scopes)))
    for entity_id, name, cik in [("E0001", "NextEra Energy", "0000753308"),
                                 ("EA1", "Acme Utilities", "")]:
        conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, cik) "
            "VALUES (?, ?, ?)", (entity_id, name, cik))
    conn.commit()
    return conn


def add_event(conn, raw_event_id, source_id, payload, url,
              event_date="2026-08-11", first_seen_at="2026-08-11T00:00:00Z"):
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
        " payload, url, first_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
        (raw_event_id, source_id, event_date,
         json.dumps(payload, sort_keys=True), url, first_seen_at))
    conn.commit()


def edgar_payload(**overrides):
    payload = {
        "accessionNumber": "0000753308-26-000061", "cik": "0000753308",
        "entity_id": "E0001", "filingDate": "2026-08-11", "form": "8-K",
        "items": ["1.05"], "primaryDocDescription": "8-K",
        "primaryDocument": "nee-20260811.htm", "reportDate": "2026-08-10"}
    payload.update(overrides)
    return payload


def fr_doc(**overrides):
    doc = {
        "document_number": "2026-00001", "title": "", "type": "Notice",
        "abstract": None, "publication_date": "2026-08-11",
        "agencies": [FERC_AGENCY], "agency_names": [], "docket_ids": [],
        "regulation_id_numbers": [], "cfr_references": [],
        "effective_on": None, "comments_close_on": None, "significant": None,
        "html_url": "https://www.federalregister.gov/documents/x"}
    doc.update(overrides)
    return doc


class SignalInvariantTestCase(unittest.TestCase):
    """Shared assertions. Subclass-free: every case calls ``check``."""

    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def emitted(self):
        return self.conn.execute(
            "SELECT * FROM signals ORDER BY signal_id").fetchall()

    def check(self, label, minimum):
        """Assert the classifier emitted at least ``minimum`` signals, then
        assert the R4.1 carry contract on every one of them.

        The count assertion is the guard against a vacuous pass - see the
        module docstring. Raise ``minimum`` only with evidence, never to make a
        newly-silent classifier green again."""
        rows = self.emitted()
        self.assertGreaterEqual(
            len(rows), minimum,
            f"{label} emitted {len(rows)} signals, expected at least "
            f"{minimum}. A classifier that stops emitting makes the R4.1 "
            f"invariants below assert over an empty set - fix the classifier "
            f"or re-baseline this count deliberately.")
        for sig in rows:
            for field in R41_FIELDS:
                value = sig[field]
                self.assertIsNotNone(
                    value, f"{label} {sig['signal_id']}: {field} is NULL "
                           f"(R4.1: every signal MUST carry it)")
                self.assertTrue(
                    str(value).strip(),
                    f"{label} {sig['signal_id']}: {field} is empty "
                    f"(R4.1: every signal MUST carry it)")
            confidence = sig["confidence"]
            self.assertIsNotNone(
                confidence, f"{label} {sig['signal_id']}: confidence is NULL")
            self.assertGreaterEqual(
                confidence, 0.0,
                f"{label} {sig['signal_id']}: confidence {confidence} < 0")
            self.assertLessEqual(
                confidence, 1.0,
                f"{label} {sig['signal_id']}: confidence {confidence} > 1")
            evidence = self.conn.execute(
                "SELECT evidence_text FROM signal_evidence "
                "WHERE signal_id = ?", (sig["signal_id"],)).fetchall()
            self.assertTrue(
                evidence, f"{label} {sig['signal_id']}: no signal_evidence "
                          f"row (R4.1: no evidence, no signal)")
            for row in evidence:
                self.assertTrue(
                    (row["evidence_text"] or "").strip(),
                    f"{label} {sig['signal_id']}: empty evidence_text")
        return rows


class TestIncidentInvariants(SignalInvariantTestCase):
    def test_edgar_8k_105_own_and_peer(self):
        add_event(self.conn, f"{EDGAR}:1", EDGAR, edgar_payload(),
                  "https://www.sec.gov/Archives/x.htm")
        run_classifier(self.conn, incident.CLASSIFIER_ID, EDGAR,
                       incident.classify_edgar_incident,
                       incident.PARSER_VERSION)
        self.check("incident/sec_edgar_submissions", 2)


class TestLeadershipInvariants(SignalInvariantTestCase):
    def test_presswire_appointment(self):
        add_event(self.conn, f"{PRN}:1", PRN,
                  {"title": "Acme Utilities Appoints Jane Doe as CISO",
                   "description": "", "link": "https://example.test/1"},
                  "https://example.test/1")
        run_classifier(self.conn, leadership.CLASSIFIER_ID, PRN,
                       leadership.classify_presswire,
                       leadership.PARSER_VERSION)
        self.check("leadership/presswire_prnewswire", 1)

    def test_edgar_502_with_security_title(self):
        add_event(self.conn, f"{EDGAR}:1", EDGAR,
                  edgar_payload(items=["5.02"],
                                primaryDocDescription="Appointment of Chief "
                                                      "Information Security "
                                                      "Officer"),
                  "https://www.sec.gov/Archives/x.htm")
        run_classifier(self.conn, leadership.CLASSIFIER_ID, EDGAR,
                       leadership.classify_edgar, leadership.PARSER_VERSION)
        self.check("leadership/sec_edgar_submissions", 1)


class TestCompanyStatementInvariants(SignalInvariantTestCase):
    def test_presswire_breach_disclosure(self):
        add_event(self.conn, f"{PRN}:1", PRN,
                  {"title": "Acme Utilities Confirms Data Breach Affecting "
                            "Customer Records",
                   "description": "", "link": "https://example.test/1"},
                  "https://example.test/1")
        run_classifier(self.conn, company_statement.CLASSIFIER_ID, PRN,
                       company_statement.classify_presswire,
                       company_statement.PARSER_VERSION)
        self.check("company_statement/presswire_prnewswire", 1)


class TestRansomwareInvariants(SignalInvariantTestCase):
    def test_own_and_peer_victims(self):
        for n, victim in ((1, "NextEra Energy"), (2, "Offlist Pipeline Co")):
            add_event(
                self.conn, f"{RANSOM}:{n}", RANSOM,
                {"victim": victim, "group": "AcmeLocker",
                 "discovered": "2026-08-05T09:00:00", "country": "US",
                 "activity": "Energy",
                 "url": f"https://www.ransomware.live/id/{n}"},
                f"https://www.ransomware.live/id/{n}",
                event_date="2026-08-05",
                first_seen_at=f"2026-08-05T00:00:0{n}Z")
        run_classifier(self.conn, ransomware.CLASSIFIER_ID, RANSOM,
                       ransomware.classify_ransomware,
                       ransomware.PARSER_VERSION)
        self.check("ransomware/ransomware_live", 2)


class TestRegulatoryInvariants(SignalInvariantTestCase):
    def test_federal_register_sector_and_named_account(self):
        add_event(self.conn, f"{FR}:1", FR, fr_doc(
            type="Rule", agencies=[TSA_AGENCY],
            title="Enhancing Surface Cyber Risk Management",
            abstract=("TSA is codifying security directive requirements for "
                      "pipeline and rail owner/operators. Owner/operators "
                      "must establish cybersecurity requirements and report "
                      "incidents."),
            effective_on="2026-10-01"),
            "https://www.federalregister.gov/documents/1")
        add_event(self.conn, f"{FR}:2", FR, fr_doc(
            type="Notice",
            title="Acme Utilities, Inc.; Notice of Proposed Civil Penalty",
            abstract=("The Office of Enforcement proposes a civil penalty for "
                      "violations of the Critical Infrastructure Protection "
                      "Reliability Standards. Comments are due within 30 "
                      "days."),
            comments_close_on="2026-09-10"),
            "https://www.federalregister.gov/documents/2",
            first_seen_at="2026-08-11T00:00:01Z")
        run_classifier(self.conn, regulatory.CLASSIFIER_ID, FR,
                       regulatory.classify_federal_register,
                       regulatory.PARSER_VERSION)
        # TSA sector card + enforcement sector card + enforcement account card
        self.check("regulatory/federal_register", 3)

    def test_nerc_page_diff(self):
        for n, (text, seen) in enumerate(
                ((OLD_PAGE_TEXT, "2026-08-01T00:00:00Z"),
                 (NEW_PAGE_TEXT, "2026-08-08T00:00:00Z")), start=1):
            add_event(self.conn, f"{NERC}:{n}", NERC,
                      {"page_url": PAGE_URL, "fetched_date": "2026-08-08",
                       "text": text}, PAGE_URL,
                      event_date="2026-08-08", first_seen_at=seen)
        run_classifier(self.conn, regulatory.CLASSIFIER_ID, NERC,
                       regulatory.classify_nerc_pages,
                       regulatory.PARSER_VERSION)
        self.check("regulatory/nerc_pages", 1)


class TestSecurityRssInvariants(SignalInvariantTestCase):
    def test_own_disclosure_and_leak_peer(self):
        add_event(self.conn, f"{RECORD}:1", RECORD,
                  {"title": "NextEra Energy discloses data breach affecting "
                            "customers",
                   "description": "",
                   "link": f"https://example.test/{RECORD}/1"},
                  f"https://example.test/{RECORD}/1", event_date="2026-08-12")
        add_event(self.conn, f"{BLEEP}:1", BLEEP,
                  {"title": "Ransomware gang claims to have breached Acme "
                            "Regional Supplier",
                   "description": "",
                   "link": f"https://example.test/{BLEEP}/1"},
                  f"https://example.test/{BLEEP}/1", event_date="2026-08-12")
        for source_id in (RECORD, BLEEP):
            run_classifier(self.conn, security_rss.CLASSIFIER_ID, source_id,
                           security_rss.SOURCES[source_id],
                           security_rss.PARSER_VERSION)
        # one own card (The Record) + one leak-adjacent peer (BleepingComputer)
        self.check("incident_security_rss", 2)


class TestSourceUrlIsNotEnforced(SignalInvariantTestCase):
    """CHARACTERIZATION of the gap this file cannot close from the test side.

    R4.1 makes source_url a MUST, but nothing enforces it: the column is
    nullable with no CHECK (0001_initial.sql), and the insert path stores
    ``raw["url"] or ""`` verbatim. A raw event fetched without a url therefore
    mints a signal carrying an empty source_url - unfalsifiable evidence on an
    otherwise valid card.

    This test pins TODAY'S behaviour so the hole is visible and measured. If
    schema-level or insert-level enforcement lands, this test MUST go red -
    delete it then; it is the record of the gap, not a defence of it.
    """

    def test_empty_raw_event_url_yields_empty_source_url(self):
        add_event(self.conn, f"{EDGAR}:1", EDGAR, edgar_payload(), "")
        run_classifier(self.conn, incident.CLASSIFIER_ID, EDGAR,
                       incident.classify_edgar_incident,
                       incident.PARSER_VERSION)
        rows = self.emitted()
        self.assertTrue(rows)
        self.assertEqual([r["source_url"] for r in rows], ["", ""])


if __name__ == "__main__":
    unittest.main()
