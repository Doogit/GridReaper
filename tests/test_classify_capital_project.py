"""USAspending capital_project classifier tests (R4.1, R5, R9.4).

Hermetic: real migrations against in-memory SQLite, FK on, canned
USAspending award payloads (shaped after app/spikes/usaspending_probe.py's
live-verified response shape) run through the real framework
(run_classifier).
"""
import json
import sqlite3
import unittest

from app.classify.capital_project import PARSER_VERSION, classify_usaspending
from app.classify.runner import run_classifier
from app.db.migrate import apply_migrations


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO source_policies (source_id, name, evidence_rank) "
        "VALUES ('usaspending', 'usaspending', 1)")
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        " decay_half_life_days, mvp_flag, evidence_quality, allowed_scopes) "
        "VALUES ('capital_project', 'capital_project', 3, 540, 1, "
        " 'PC', '[\"account\"]')")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name) "
        "VALUES ('E1', 'Duke Energy Indiana LLC')")
    conn.execute(
        "INSERT INTO entity_aliases (entity_id, alias, source) "
        "VALUES ('E1', 'Duke Energy Indiana', 'test')")
    conn.commit()
    return conn


def award_payload(**overrides):
    payload = {
        "Award ID": "AWD-0001",
        "Recipient Name": "Duke Energy Indiana LLC",
        "Awarding Agency": "Department of Energy",
        "Awarding Sub Agency": "",
        "Start Date": "2026-01-15",
        "Description": "Grant to fund substation hardening and control "
                       "room modernization across three sites.",
    }
    payload.update(overrides)
    return payload


def add_event(conn, i, payload, event_date="2026-01-15"):
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
        " payload, url, first_seen_at) "
        "VALUES (?, 'usaspending', ?, ?, '', ?)",
        (f"usaspending:{i}", event_date, json.dumps(payload),
         f"2026-01-15T00:00:0{i}Z"))
    conn.commit()


class ClassifyUsaspendingTest(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def run_clf(self, force=False):
        return run_classifier(self.conn, "capital_project", "usaspending",
                              classify_usaspending, PARSER_VERSION,
                              force=force)

    def signals(self):
        return self.conn.execute(
            "SELECT * FROM signals ORDER BY signal_id").fetchall()

    def test_happy_path_doe_award_resolves_to_entity(self):
        description = award_payload()["Description"]
        add_event(self.conn, 1, award_payload())
        s = self.run_clf()
        self.assertEqual((s["status"], s["signals_new"]), ("success", 1))
        sig = self.signals()[0]
        self.assertEqual(sig["trigger_id"], "capital_project")
        self.assertEqual(sig["signal_scope"], "account")
        self.assertEqual(sig["entity_id"], "E1")
        self.assertEqual(sig["event_date"], "2026-01-15")
        self.assertIn("Duke Energy Indiana LLC", sig["headline"])
        rows = self.conn.execute(
            "SELECT evidence_text, evidence_locator FROM signal_evidence "
            "WHERE signal_id = ?", (sig["signal_id"],)).fetchall()
        locators = {r["evidence_locator"] for r in rows}
        self.assertIn("description", locators)
        self.assertIn("awarding_agency", locators)
        # The award description reaches evidence verbatim -- a later unit's
        # keyword-absence check depends on this text being complete.
        desc_row = next(r for r in rows if r["evidence_locator"] == "description")
        self.assertIn(description, desc_row["evidence_text"])

    def test_dhs_toptier_qualifies_regardless_of_subagency(self):
        add_event(self.conn, 1, award_payload(
            **{"Awarding Agency": "Department of Homeland Security",
               "Awarding Sub Agency": "Federal Emergency Management Agency"}))
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 1)
        rows = self.conn.execute(
            "SELECT evidence_text FROM signal_evidence "
            "WHERE evidence_locator = 'awarding_agency'").fetchall()
        self.assertIn("Federal Emergency Management Agency",
                      rows[0]["evidence_text"])

    def test_event_date_falls_back_to_award_start_date(self):
        add_event(self.conn, 1, award_payload(**{"Start Date": "2026-03-01"}),
                  event_date="")
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 1)
        self.assertEqual(self.signals()[0]["event_date"], "2026-03-01")

    def test_missing_award_id_excluded(self):
        payload = award_payload()
        del payload["Award ID"]
        add_event(self.conn, 1, payload)
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 0)

    def test_cisa_subagency_qualifies_under_a_different_toptier_label(self):
        # Defensive branch: a future response could carry CISA under a
        # toptier label this classifier does not otherwise recognize --
        # the subagency marker check must still catch it.
        add_event(self.conn, 1, award_payload(
            **{"Awarding Agency": "Other Federal Agency",
               "Awarding Sub Agency":
                   "Cybersecurity and Infrastructure Security Agency"}))
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 1)

    def test_alias_variant_resolves_to_correct_entity(self):
        add_event(self.conn, 1, award_payload(
            **{"Recipient Name": "Duke Energy Indiana"}))
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 1)
        self.assertEqual(self.signals()[0]["entity_id"], "E1")

    def test_non_qualifying_agency_excluded(self):
        add_event(self.conn, 1, award_payload(
            **{"Awarding Agency": "Department of Agriculture"}))
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(self.signals(), [])

    def test_missing_recipient_name_excluded(self):
        payload = award_payload()
        del payload["Recipient Name"]
        add_event(self.conn, 1, payload)
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 0)

    def test_missing_awarding_agency_excluded(self):
        payload = award_payload()
        del payload["Awarding Agency"]
        add_event(self.conn, 1, payload)
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 0)

    def test_malformed_payload_skipped_not_crashed(self):
        conn = self.conn
        conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
            " payload, url, first_seen_at) "
            "VALUES ('usaspending:bad', 'usaspending', '2026-01-01', "
            " 'not json', '', '2026-01-01T00:00:00Z')")
        conn.commit()
        s = self.run_clf()
        self.assertEqual(s["status"], "success")
        self.assertEqual(s["signals_new"], 0)

    def test_non_dict_payload_skipped_not_crashed(self):
        conn = self.conn
        conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
            " payload, url, first_seen_at) "
            "VALUES ('usaspending:bad', 'usaspending', '2026-01-01', "
            " '[1, 2, 3]', '', '2026-01-01T00:00:00Z')")
        conn.commit()
        s = self.run_clf()
        self.assertEqual(s["status"], "success")
        self.assertEqual(s["signals_new"], 0)

    def test_reprocessing_is_idempotent_no_duplicate_signal(self):
        add_event(self.conn, 1, award_payload())
        self.run_clf()
        s2 = self.run_clf(force=True)
        self.assertEqual(s2["signals_new"], 0)
        self.assertEqual(s2["signals_existing"], 1)
        self.assertEqual(len(self.signals()), 1)


if __name__ == "__main__":
    unittest.main()
