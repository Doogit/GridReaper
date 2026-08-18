"""TSA Security Directive spike tests (R3, R9.6).

Hermetic: real migrations against in-memory SQLite, canned federal_register
raw_events fixture rows, no network at all -- this probe only ever queries
the local store, so there is nothing to mock.
"""
import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.db.migrate import apply_migrations
from app.spikes import tsa_sd_probe as probe

REPO = Path(__file__).resolve().parent.parent
PIPELINE = REPO / "deploy" / "ingest_pipeline.sh"
MODULE_SRC = Path(probe.__file__).read_text(encoding="utf-8")

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)   # cutoff 2024-08-18

TSA = ["Transportation Security Administration"]
FERC = ["Federal Energy Regulatory Commission"]


def seed_source(conn):
    conn.execute(
        "INSERT INTO source_policies (source_id, name) VALUES (?, ?)",
        ("federal_register", "Federal Register API"))
    conn.commit()


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = None
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    seed_source(conn)
    return conn


def insert_raw_event(conn, raw_event_id, event_date, payload):
    """``payload`` may be a dict (JSON-encoded here, mirroring
    ``app/ingest/federal_register.py``'s ``json.dumps(doc)``) or a raw string
    (to exercise the malformed-payload path)."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
        "payload) VALUES (?, ?, ?, ?)",
        (raw_event_id, "federal_register", event_date, text))
    conn.commit()


def doc(agency_names, title, abstract, document_number="2026-00001"):
    return {
        "document_number": document_number,
        "title": title,
        "type": "Notice",
        "abstract": abstract,
        "agency_names": agency_names,
        "action": "",
    }


class TestRelevanceHeuristic(unittest.TestCase):
    """(a)+(b)+(c): TSA agency AND pipeline/LNG term AND security-directive
    term -- all three, or the document does not count."""

    def test_tsa_pipeline_cybersecurity_document_is_relevant(self):
        payload = doc(TSA, "Pipeline Cybersecurity Requirements",
                     "Security directive requirements for pipeline owners "
                     "and operators.")
        self.assertTrue(probe.is_tsa_sd_relevant(payload))

    def test_tsa_lng_security_measures_document_is_relevant(self):
        payload = doc(TSA, "LNG Facility Security Measures",
                     "Cyber security requirements for LNG facility owners.")
        self.assertTrue(probe.is_tsa_sd_relevant(payload))

    def test_tsa_document_without_pipeline_or_lng_is_not_relevant(self):
        """TSA regulates far beyond pipelines (aviation, rail, mass transit)
        -- the agency filter alone must not admit those topics."""
        payload = doc(TSA, "Air Cargo Screening Requirements",
                     "Security requirements for air carriers.")
        self.assertFalse(probe.is_tsa_sd_relevant(payload))

    def test_pipeline_security_document_from_a_non_tsa_agency_is_not_relevant(self):
        payload = doc(FERC, "Pipeline Security Notice",
                     "Cybersecurity requirements for pipeline operators.")
        self.assertFalse(probe.is_tsa_sd_relevant(payload))

    def test_tsa_pipeline_document_without_a_security_term_is_not_relevant(self):
        payload = doc(TSA, "Pipeline Safety Inspection Schedule",
                     "Routine inspection schedule for pipeline facilities.")
        self.assertFalse(probe.is_tsa_sd_relevant(payload))

    def test_string_agency_names_field_is_tolerated(self):
        """The real API returns a list, but the heuristic must not crash on a
        differently-shaped payload."""
        payload = doc("Transportation Security Administration",
                     "Pipeline Security Directive",
                     "Cybersecurity directive for pipeline owners.")
        self.assertTrue(probe.is_tsa_sd_relevant(payload))


class TestAnalyze(unittest.TestCase):
    def test_mixed_fixture_counts_only_the_relevant_midstream_lng_subset(self):
        conn = fixture_conn()
        insert_raw_event(conn, "raw-1", "2025-01-15", doc(
            TSA, "Pipeline Cybersecurity Requirements",
            "Security directive requirements for pipeline owners.",
            "2025-00001"))
        insert_raw_event(conn, "raw-2", "2025-02-01", doc(
            TSA, "Air Cargo Screening Requirements",
            "Security requirements for air carriers.", "2025-00002"))
        insert_raw_event(conn, "raw-3", "2025-03-01", doc(
            FERC, "Pipeline Security Notice", "Cybersecurity requirements.",
            "2025-00003"))
        insert_raw_event(conn, "raw-4", "2025-04-01", doc(
            TSA, "LNG Facility Security Measures",
            "Cyber security requirements for LNG facility owners.",
            "2025-00004"))

        result = probe.analyze(conn, now=NOW)
        conn.close()
        self.assertEqual(result["match_count"], 2)
        self.assertEqual(result["total_raw_events"], 4)
        numbers = {m["document_number"] for m in result["matches"]}
        self.assertEqual(numbers, {"2025-00001", "2025-00004"})

    def test_zero_match_fixture_reports_zero_and_recommends_against_reuse(self):
        conn = fixture_conn()
        insert_raw_event(conn, "raw-1", "2025-02-01", doc(
            TSA, "Air Cargo Screening Requirements",
            "Security requirements for air carriers.", "2025-00002"))
        insert_raw_event(conn, "raw-2", "2025-03-01", doc(
            FERC, "Pipeline Security Notice", "Cybersecurity requirements.",
            "2025-00003"))

        result = probe.analyze(conn, now=NOW)
        conn.close()
        self.assertEqual(result["match_count"], 0)
        self.assertIn("do NOT reuse federal_register", result["recommendation"])
        self.assertIn("NEITHER source is supported", result["recommendation"])

    def test_match_outside_the_24_month_window_is_excluded(self):
        conn = fixture_conn()
        # cutoff is 2024-08-18; this document is well outside the window.
        insert_raw_event(conn, "raw-1", "2022-01-01", doc(
            TSA, "LNG Facility Security Directive",
            "Security directive requirements for LNG facility owners.",
            "2022-00001"))

        result = probe.analyze(conn, now=NOW)
        conn.close()
        self.assertEqual(result["match_count"], 0)
        self.assertEqual(result["out_of_window_matches"], 1)
        self.assertEqual(result["undated_matches"], 0)

    def test_non_iso_event_date_is_treated_as_undated_not_lexically_compared(self):
        """A garbage event_date must never win a raw string comparison
        against cutoff -- that could silently misclassify it as in- or
        out-of-window and corrupt the recommendation this probe exists to
        produce."""
        conn = fixture_conn()
        insert_raw_event(conn, "raw-1", "garbage-date", doc(
            TSA, "Pipeline Cybersecurity Requirements",
            "Security directive requirements for pipeline owners.",
            "2025-00001"))

        result = probe.analyze(conn, now=NOW)
        conn.close()
        self.assertEqual(result["match_count"], 0)
        self.assertEqual(result["undated_matches"], 1)
        self.assertEqual(result["out_of_window_matches"], 0)

    def test_boundary_date_at_exactly_the_cutoff_is_in_window(self):
        conn = fixture_conn()
        insert_raw_event(conn, "raw-1", "2024-08-18", doc(
            TSA, "Pipeline Security Directive",
            "Security directive for pipeline owners.", "2024-00001"))

        result = probe.analyze(conn, now=NOW)
        conn.close()
        self.assertEqual(result["match_count"], 1)

    def test_malformed_payload_is_skipped_not_fabricated(self):
        conn = fixture_conn()
        insert_raw_event(conn, "raw-1", "2025-01-15", "{not valid json")
        insert_raw_event(conn, "raw-2", "2025-01-16", doc(
            TSA, "Pipeline Cybersecurity Requirements",
            "Security directive requirements for pipeline owners.",
            "2025-00001"))

        result = probe.analyze(conn, now=NOW)
        conn.close()
        self.assertEqual(result["malformed_payloads"], 1)
        self.assertEqual(result["match_count"], 1)

    def test_valid_json_non_dict_payload_is_malformed(self):
        """A distinct code path from invalid JSON syntax: the payload parses
        cleanly but isn't a document object."""
        conn = fixture_conn()
        insert_raw_event(conn, "raw-1", "2025-01-15", "[]")

        result = probe.analyze(conn, now=NOW)
        conn.close()
        self.assertEqual(result["malformed_payloads"], 1)
        self.assertEqual(result["match_count"], 0)

    def test_undated_relevant_document_is_excluded_not_counted_in_window(self):
        conn = fixture_conn()
        insert_raw_event(conn, "raw-1", "", doc(
            TSA, "Pipeline Cybersecurity Requirements",
            "Security directive requirements for pipeline owners.",
            "2025-00001"))

        result = probe.analyze(conn, now=NOW)
        conn.close()
        self.assertEqual(result["match_count"], 0)
        self.assertEqual(result["undated_matches"], 1)
        self.assertEqual(result["out_of_window_matches"], 0)

    def test_matches_are_sorted_newest_first(self):
        conn = fixture_conn()
        insert_raw_event(conn, "raw-1", "2025-01-15", doc(
            TSA, "Pipeline Cybersecurity Requirements",
            "Security directive requirements for pipeline owners.",
            "2025-00001"))
        insert_raw_event(conn, "raw-2", "2025-06-01", doc(
            TSA, "LNG Facility Security Measures",
            "Cyber security requirements for LNG facility owners.",
            "2025-00002"))

        result = probe.analyze(conn, now=NOW)
        conn.close()
        self.assertEqual(
            [m["document_number"] for m in result["matches"]],
            ["2025-00002", "2025-00001"])


class TestWindowCutoff(unittest.TestCase):
    def test_leap_day_anniversary_falls_back_to_feb_28(self):
        now = datetime(2028, 2, 29, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(probe.window_cutoff(now), "2026-02-28")


class TestRecommendation(unittest.TestCase):
    def test_zero_states_the_sensitive_security_information_caveat(self):
        text = probe.recommendation(0)
        self.assertIn("Sensitive Security Information", text)
        self.assertIn("NEITHER", text)

    def test_nonzero_recommends_reuse_of_federal_register(self):
        text = probe.recommendation(3)
        self.assertIn("REUSE federal_register", text)
        self.assertIn("dedicated TSA.gov fetcher is not needed", text)

    def test_zero_with_out_of_window_matches_notes_the_corpus_format_works(self):
        """A 0 in-window count must not read as 'this corpus can't carry TSA
        SD content at all' when a relevant document exists just outside the
        window -- that's a stronger, false claim (correctness finding)."""
        text = probe.recommendation(0, out_of_window_count=2)
        self.assertIn("NOTE:", text)
        self.assertIn("2 relevant document(s)", text)
        self.assertIn("OUTSIDE the trailing window", text)

    def test_zero_without_out_of_window_matches_has_no_note(self):
        text = probe.recommendation(0)
        self.assertNotIn("NOTE:", text)

    def test_report_states_the_source_strategy_plainly(self):
        conn = fixture_conn()
        insert_raw_event(conn, "raw-1", "2025-01-15", doc(
            TSA, "Pipeline Cybersecurity Requirements",
            "Security directive requirements for pipeline owners.",
            "2025-00001"))
        result = probe.analyze(conn, now=NOW)
        conn.close()
        report_text = probe.format_report(result)
        self.assertIn("RECOMMENDATION", report_text)
        self.assertIn("REUSE federal_register", report_text)
        self.assertIn(
            "2025-01-15  2025-00001  Pipeline Cybersecurity Requirements",
            report_text)


class TestContainment(unittest.TestCase):
    """Same containment discipline as breach_registry_probe/
    ferc_elibrary_probe: read-only connection, no DML in the module, and
    absent from the packaged ingestion pipeline."""

    def test_module_uses_a_read_only_connection_and_no_dml(self):
        self.assertIn("mode=ro", MODULE_SRC)
        for verb in ("INSERT ", "UPDATE ", "DELETE "):
            self.assertNotIn(verb, MODULE_SRC.upper())

    def test_probe_is_absent_from_the_ingest_pipeline(self):
        pipeline = PIPELINE.read_text(encoding="utf-8")
        self.assertNotIn("tsa_sd_probe", pipeline)
        self.assertNotIn("app.spikes", pipeline)

    def test_report_opens_the_store_read_only_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "probe.db")
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA foreign_keys=ON;")
            apply_migrations(conn)
            seed_source(conn)
            insert_raw_event(conn, "raw-1", "2025-01-15", doc(
                TSA, "Pipeline Cybersecurity Requirements",
                "Security directive requirements for pipeline owners.",
                "2025-00001"))
            conn.close()

            before = os.path.getsize(db_path)
            out = io.StringIO()
            result = probe.report(db_path, now=NOW, out=out)
            self.assertEqual(result["match_count"], 1)
            self.assertIn("MATCH COUNT (in-window, TSA-SD-relevant): 1",
                          out.getvalue())
            self.assertEqual(os.path.getsize(db_path), before)

            check = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    check.execute("SELECT COUNT(*) FROM raw_events")
                    .fetchone()[0], 1, "the probe wrote to raw_events")
            finally:
                check.close()

    def test_read_only_connect_survives_a_uri_fragment_in_the_path(self):
        """A '#' starts the fragment in a SQLite URI, so pasting the path into
        an f-string would truncate it and take ``?mode=ro`` with it -- the
        connection would then open READ-WRITE, silently."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "gridsignals#1.db")
            conn = sqlite3.connect(db_path)
            apply_migrations(conn)
            seed_source(conn)
            conn.close()

            ro = probe.read_only_connect(db_path)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    ro.execute("INSERT INTO source_policies (source_id, name) "
                               "VALUES ('x', 'x')")
            finally:
                ro.close()


if __name__ == "__main__":
    unittest.main()
