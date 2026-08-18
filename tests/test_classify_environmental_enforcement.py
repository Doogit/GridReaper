"""EPA ECHO environmental enforcement classifier tests (R4.1, R7, R9.4).

Hermetic: real migrations against in-memory SQLite, FK on, canned EPA ECHO
case payloads (shaped after live-verified 2026-08-18 API output) run through
the real framework (run_classifier).
"""
import json
import sqlite3
import unittest

from app.classify.environmental_enforcement import (PARSER_VERSION,
                                                     classify_epa_echo)
from app.classify.runner import run_classifier
from app.db.migrate import apply_migrations


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO source_policies (source_id, name, evidence_rank) "
        "VALUES ('epa_echo', 'epa_echo', 1)")
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        " decay_half_life_days, mvp_flag, evidence_quality, allowed_scopes) "
        "VALUES ('audit_consent_decree', 'audit_consent_decree', 4, 540, 0, "
        " 'PC', '[\"account\"]')")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name) "
        "VALUES ('OGE1', 'Hilcorp Energy Company')")
    conn.commit()
    return conn


def case_payload(**overrides):
    payload = {
        "CaseNumber": "03-2022-7006",
        "CaseName": "HILCORP ENERGY COMPANY",
        "CaseCategoryCode": "JDC",
        "CivilCriminalIndicator": "CI",
        "PrimaryLaw": "CAA",
        "DateFiled": "01/14/2025",
        "SettlementDate": "01/14/2025",
        "EnfOutcome": "Final Order With Penalty",
    }
    payload.update(overrides)
    return payload


def add_event(conn, i, payload, event_date="2025-01-14"):
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
        " payload, url, first_seen_at) "
        "VALUES (?, 'epa_echo', ?, ?, '', ?)",
        (f"epa_echo:{i}", event_date, json.dumps(payload),
         f"2025-01-14T00:00:0{i}Z"))
    conn.commit()


class ClassifyEpaEchoTest(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def run_clf(self, force=False):
        return run_classifier(self.conn, "environmental_enforcement",
                              "epa_echo", classify_epa_echo, PARSER_VERSION,
                              force=force)

    def signals(self):
        return self.conn.execute(
            "SELECT * FROM signals ORDER BY signal_id").fetchall()

    def test_happy_path_genuine_consent_decree_resolves_to_entity(self):
        add_event(self.conn, 1, case_payload())
        s = self.run_clf()
        self.assertEqual((s["status"], s["signals_new"]), ("success", 1))
        sig = self.signals()[0]
        self.assertEqual(sig["trigger_id"], "audit_consent_decree")
        self.assertEqual(sig["signal_scope"], "account")
        self.assertEqual(sig["entity_id"], "OGE1")
        self.assertEqual(sig["event_date"], "2025-01-14")
        self.assertIn("HILCORP ENERGY COMPANY", sig["headline"])
        locators = {r["evidence_locator"] for r in self.conn.execute(
            "SELECT evidence_locator FROM signal_evidence "
            "WHERE signal_id = ?", (sig["signal_id"],))}
        self.assertIn("classifier_note", locators)
        self.assertIn("settlement_date", locators)

    def test_case_name_annotations_are_stripped_before_resolution(self):
        add_event(self.conn, 1, case_payload(
            CaseName="HILCORP ENERGY COMPANY (LEAD) (NATIONAL CASE)"))
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 1)
        self.assertEqual(self.signals()[0]["entity_id"], "OGE1")

    def test_nested_parenthetical_annotations_fully_stripped(self):
        add_event(self.conn, 1, case_payload(
            CaseName="HILCORP ENERGY COMPANY (LEAD (NATIONAL))"))
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 1)
        self.assertEqual(self.signals()[0]["entity_id"], "OGE1")

    def test_trailing_et_al_co_defendant_marker_stripped(self):
        add_event(self.conn, 1, case_payload(
            CaseName="HILCORP ENERGY COMPANY, ET AL."))
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 1)
        self.assertEqual(self.signals()[0]["entity_id"], "OGE1")

    def test_administrative_case_excluded_no_consent_decree(self):
        # AFR (Administrative - Formal) cases are penalties/orders EPA
        # settles under its own authority -- never a consent decree.
        add_event(self.conn, 1, case_payload(CaseCategoryCode="AFR"))
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(self.signals(), [])

    def test_criminal_case_excluded(self):
        add_event(self.conn, 1, case_payload(CivilCriminalIndicator="CR"))
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 0)

    def test_unsettled_case_excluded_no_decree_yet(self):
        add_event(self.conn, 1, case_payload(SettlementDate=""))
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 0)

    def test_malformed_payload_skipped_not_crashed(self):
        conn = self.conn
        conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
            " payload, url, first_seen_at) "
            "VALUES ('epa_echo:bad', 'epa_echo', '2025-01-01', 'not json', "
            " '', '2025-01-01T00:00:00Z')")
        conn.commit()
        s = self.run_clf()
        self.assertEqual(s["status"], "success")
        self.assertEqual(s["signals_new"], 0)

    def test_reprocessing_is_idempotent_no_duplicate_signal(self):
        add_event(self.conn, 1, case_payload())
        self.run_clf()
        s2 = self.run_clf(force=True)
        self.assertEqual(s2["signals_new"], 0)
        self.assertEqual(s2["signals_existing"], 1)
        self.assertEqual(len(self.signals()), 1)


if __name__ == "__main__":
    unittest.main()
