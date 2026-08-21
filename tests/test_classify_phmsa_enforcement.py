"""PHMSA pipeline-enforcement classifier tests (R4.1, R6, R6.2, R6.4).

Hermetic: real migrations against in-memory SQLite, FK on, canned PHMSA
enforcement-feed row payloads (shaped after the live-verified 2026-08-18
feed header) run through the real framework (run_classifier).
"""
import json
import sqlite3
import unittest

from app.classify.phmsa_enforcement import (CASE_TYPE_INFO,
                                            PARSER_VERSION,
                                            classify_phmsa_enforcement)
from app.classify.runner import run_classifier
from app.db.migrate import apply_migrations


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO source_policies (source_id, name, evidence_rank) "
        "VALUES ('phmsa_enforcement', 'phmsa_enforcement', 1)")
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        " decay_half_life_days, mvp_flag, evidence_quality, allowed_scopes) "
        "VALUES ('pipeline_enforcement_action', 'pipeline_enforcement_action', "
        " 5, 365, 0, 'IR', '[\"account\"]')")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector) "
        "VALUES ('E0111', 'Kinder Morgan', 'midstream')")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector) "
        "VALUES ('E0999', 'NextEra Energy', 'iou_electric')")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector) "
        "VALUES ('E0118', 'Targa Resources', 'midstream')")
    conn.execute(
        "INSERT INTO entity_aliases (entity_id, alias, source) "
        "VALUES ('E0111', 'Kinder Morgan Utopia LLC', 'test')")
    conn.commit()
    return conn


def case_payload(**overrides):
    payload = {
        "CPF_Number": "52026001NOPV",
        "Operator_Name": "Kinder Morgan Utopia LLC",
        "Case_Type": "Notice of Probable Violation",
        "Violation_Category": "Integrity Management",
        "Cited_Regulations": "195.452(j)(3)",
        "Opened_Date": "1/1/26",
    }
    payload.update(overrides)
    return payload


def add_event(conn, i, payload, event_date="2026-01-01"):
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
        " payload, url, first_seen_at) "
        "VALUES (?, 'phmsa_enforcement', ?, ?, '', ?)",
        (f"phmsa_enforcement:{i}", event_date, json.dumps(payload),
         f"2026-01-01T00:00:0{i}Z"))
    conn.commit()


class ClassifyPhmsaEnforcementTest(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def run_clf(self, force=False):
        return run_classifier(self.conn, "phmsa_enforcement",
                              "phmsa_enforcement", classify_phmsa_enforcement,
                              PARSER_VERSION, force=force)

    def signals(self):
        return self.conn.execute(
            "SELECT * FROM signals ORDER BY signal_id").fetchall()

    def test_happy_path_resolves_to_midstream_entity(self):
        add_event(self.conn, 1, case_payload())
        s = self.run_clf()
        self.assertEqual((s["status"], s["signals_new"]), ("success", 1))
        sig = self.signals()[0]
        self.assertEqual(sig["trigger_id"], "pipeline_enforcement_action")
        self.assertEqual(sig["signal_scope"], "account")
        self.assertEqual(sig["entity_id"], "E0111")
        self.assertEqual(sig["event_date"], "2026-01-01")
        self.assertIn("Kinder Morgan Utopia LLC", sig["headline"])

    def test_severity_reflected_in_confidence_and_evidence(self):
        # CAO is the most severe qualifying case type -> highest confidence,
        # and a dedicated "severity" evidence locator names the tier.
        add_event(self.conn, 1, case_payload(
            CPF_Number="1CAO", Case_Type="Corrective Action Order"))
        add_event(self.conn, 2, case_payload(
            CPF_Number="2WL", Case_Type="Warning Letter"), event_date="2026-01-02")
        self.run_clf()
        sigs = {s["raw_event_id"]: s for s in self.signals()}
        cao = sigs["phmsa_enforcement:1"]
        wl = sigs["phmsa_enforcement:2"]
        self.assertGreater(cao["confidence"], wl["confidence"])
        self.assertAlmostEqual(
            cao["confidence"],
            CASE_TYPE_INFO["Corrective Action Order"].confidence)
        self.assertAlmostEqual(
            wl["confidence"], CASE_TYPE_INFO["Warning Letter"].confidence)
        severity_locators = {
            r["evidence_text"] for r in self.conn.execute(
                "SELECT evidence_text FROM signal_evidence "
                "WHERE signal_id = ? AND evidence_locator = 'severity'",
                (cao["signal_id"],))}
        self.assertTrue(
            any("Corrective Action Order" in t for t in severity_locators))

    def test_non_midstream_lng_entity_excluded(self):
        add_event(self.conn, 1, case_payload(
            CPF_Number="1CAO", Operator_Name="NextEra Energy",
            Case_Type="Corrective Action Order"))
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(self.signals(), [])

    def test_excluded_when_resolved_entity_rolls_up_to_a_non_midstream_parent(self):
        # A resolved entity can itself be midstream/LNG while the ACCOUNT it
        # rolls up to (watchlist_entities.parent_id, R6.5) is not -- the
        # framework always attributes the signal to the top-level account, so
        # the scope check must hold there, not on the intermediate child.
        self.conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, subsector, "
            " parent_id) VALUES ('E0201', 'Midstream Sub Co', 'midstream', "
            " 'E0999')")   # E0999 = NextEra Energy, iou_electric
        self.conn.commit()
        add_event(self.conn, 1, case_payload(
            CPF_Number="1CAO", Operator_Name="Midstream Sub Co",
            Case_Type="Corrective Action Order"))
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(self.signals(), [])

    def test_fires_when_resolved_entity_rolls_up_to_a_midstream_parent(self):
        # The mirror case: a non-midstream/LNG-subsector child row whose
        # account (parent_id) IS midstream/LNG still fires, because the card
        # is attributed to the parent account, not the child row's own
        # subsector value.
        self.conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, subsector, "
            " parent_id) VALUES ('E0202', 'Kinder Morgan Trucking Co', "
            " 'ofs', 'E0111')")   # E0111 = Kinder Morgan, midstream
        self.conn.commit()
        add_event(self.conn, 1, case_payload(
            CPF_Number="1WL", Operator_Name="Kinder Morgan Trucking Co",
            Case_Type="Warning Letter"))
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 1)
        self.assertEqual(self.signals()[0]["entity_id"], "E0111")

    def test_alias_resolves_subsidiary_name_to_parent_entity(self):
        # "Kinder Morgan Utopia LLC" is a curated alias of the watchlist
        # entity "Kinder Morgan" -- PHMSA names the operating subsidiary,
        # not the parent watchlist name.
        add_event(self.conn, 1, case_payload(
            Operator_Name="Kinder Morgan Utopia LLC"))
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 1)
        self.assertEqual(self.signals()[0]["entity_id"], "E0111")

    def test_off_list_operator_enqueues_nothing_and_mints_no_signal(self):
        add_event(self.conn, 1, case_payload(
            Operator_Name="Totally Unknown Pipeline Co"))
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(self.signals(), [])

    def test_subsidiary_name_below_auto_match_bar_is_review_queued(self):
        # "Targa Resources Operating LLC" against seeded "Targa Resources":
        # fuzzy ratio 0.75 -- below the 0.90 auto-match bar, above the 0.75
        # review bar (the same real-world shape confirmed live in
        # app/spikes/phmsa_probe.py's own equivalent test). Must land in
        # review_queue and mint no signal (R6.2), never silently auto-match
        # or silently drop.
        add_event(self.conn, 1, case_payload(
            CPF_Number="1WL", Case_Type="Warning Letter",
            Operator_Name="Targa Resources Operating LLC"))
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(self.signals(), [])
        review = self.conn.execute(
            "SELECT COUNT(*) FROM review_queue "
            "WHERE raw_event_id = 'phmsa_enforcement:1'").fetchone()[0]
        self.assertGreater(review, 0)

    def test_non_qualifying_case_type_reclassified_defensively_excluded(self):
        # Defensive re-check (matching environmental_enforcement's
        # convention): even if a raw_events row somehow carries a
        # non-qualifying Case_Type, the classifier itself excludes it.
        add_event(self.conn, 1, case_payload(
            CPF_Number="1NOA", Case_Type="Notice of Amendment"))
        s = self.run_clf()
        self.assertEqual(s["signals_new"], 0)

    def test_malformed_payload_skipped_not_crashed(self):
        conn = self.conn
        conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
            " payload, url, first_seen_at) "
            "VALUES ('phmsa_enforcement:bad', 'phmsa_enforcement', "
            " '2026-01-01', 'not json', '', '2026-01-01T00:00:00Z')")
        conn.commit()
        s = self.run_clf()
        self.assertEqual(s["status"], "success")
        self.assertEqual(s["signals_new"], 0)

    def test_missing_operator_name_skipped_not_crashed(self):
        add_event(self.conn, 1, case_payload(Operator_Name=""))
        s = self.run_clf()
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
