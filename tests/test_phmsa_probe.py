"""PHMSA pipeline-enforcement spike tests (R9.6 combo-engine plan, U2).

Hermetic: real migrations against SQLite, canned tab-delimited fixtures, no
network. ``TestLiveFetch`` monkeypatches ``urllib.request.urlopen`` to prove
a 403 is reported explicitly (endpoint, status, body) rather than crashing
or silently returning zero, without ever touching the network.
"""
import io
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from app.db.migrate import apply_migrations
from app.spikes import phmsa_probe as probe

REPO = Path(__file__).resolve().parent.parent
PIPELINE = REPO / "deploy" / "ingest_pipeline.sh"
MODULE_SRC = Path(probe.__file__).read_text(encoding="utf-8")

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)   # cutoff 2024-08-18

# 5 watchlist entities: 4 midstream/LNG (for the build-threshold scenarios)
# plus 1 non-midstream/LNG entity (for the subsector-exclusion scenario).
ENTITIES = [
    ("E0001", "Kinder Morgan", "midstream"),
    ("E0002", "Williams Pipeline", "midstream"),
    ("E0003", "Cheniere Energy", "lng"),
    ("E0004", "Targa Resources", "midstream"),
    ("E0005", "NextEra Energy", "iou_electric"),
]


def seed(conn):
    for eid, name, subsector in ENTITIES:
        conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, subsector) "
            "VALUES (?, ?, ?)", (eid, name, subsector))
    conn.commit()


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    seed(conn)
    return conn


def tsv(rows):
    """rows: list of (operator_name, case_type, opened_date) -> tab-delimited
    text with the real feed's header."""
    lines = ["Operator_Name\tCase_Type\tOpened_Date"]
    for org, case_type, opened in rows:
        lines.append(f"{org}\t{case_type}\t{opened}")
    return "\n".join(lines) + "\n"


# 4 distinct midstream/LNG accounts, each with one qualifying case type,
# all inside the trailing-24-month window (cutoff 2024-08-18).
FIXTURE_4_HITS = tsv([
    ("Kinder Morgan", "Corrective Action Order", "3/1/26"),
    ("Williams Pipeline", "Notice of Probable Violation", "2/1/25"),
    ("Cheniere Energy", "Warning Letter", "6/1/25"),
    ("Targa Resources", "Corrective Action Order", "1/1/26"),
])

# Only 2 distinct accounts.
FIXTURE_2_HITS = tsv([
    ("Kinder Morgan", "Corrective Action Order", "3/1/26"),
    ("Williams Pipeline", "Notice of Probable Violation", "2/1/25"),
])


class TestParsers(unittest.TestCase):
    def test_parses_operator_case_type_and_opened_date(self):
        records = probe.parse_records(FIXTURE_4_HITS)
        self.assertEqual(len(records), 4)
        self.assertEqual(records[0], probe.PhmsaRecord(
            "Kinder Morgan", "Corrective Action Order", "2026-03-01"))
        self.assertEqual(records[0]._fields,
                         ("operator_name", "case_type", "opened_date"))

    def test_row_missing_operator_name_is_dropped(self):
        records = probe.parse_records(
            "Operator_Name\tCase_Type\tOpened_Date\n"
            "\tWarning Letter\t1/1/26\n")
        self.assertEqual(records, [])

    def test_row_missing_case_type_is_dropped(self):
        records = probe.parse_records(
            "Operator_Name\tCase_Type\tOpened_Date\n"
            "Acme Pipeline\t\t1/1/26\n")
        self.assertEqual(records, [])

    def test_unparseable_date_is_kept_but_carries_no_opened_date(self):
        records = probe.parse_records(
            "Operator_Name\tCase_Type\tOpened_Date\n"
            "Acme Pipeline\tWarning Letter\tnot-a-date\n")
        self.assertEqual(records, [probe.PhmsaRecord(
            "Acme Pipeline", "Warning Letter", "")])

    def test_two_digit_year_pivots_to_2000s(self):
        self.assertEqual(probe._iso_date("3/1/26"), "2026-03-01")
        self.assertEqual(probe._iso_date("1/10/02"), "2002-01-10")


class TestWindow(unittest.TestCase):
    def test_cutoff_is_the_24_month_anniversary(self):
        self.assertEqual(probe.window_cutoff(NOW), "2024-08-18")

    def test_leap_day_cutoff_falls_back_to_feb_28(self):
        leap = datetime(2028, 2, 29, tzinfo=timezone.utc)
        self.assertEqual(probe.window_cutoff(leap), "2026-02-28")


class TestAnalysis(unittest.TestCase):
    """The 5 scenarios pinned by the unit brief."""

    @classmethod
    def setUpClass(cls):
        cls.conn = fixture_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_4_distinct_accounts_counts_4_and_recommends_build(self):
        records = probe.parse_records(FIXTURE_4_HITS)
        result = probe.analyze(self.conn, records,
                               source_status={"phmsa_enforcement_data": "ok"},
                               now=NOW)
        self.assertEqual(result["hit_count"], 4)
        self.assertEqual(
            sorted(h["entity_id"] for h in result["hits"]),
            ["E0001", "E0002", "E0003", "E0004"])
        self.assertIn("BUILD U5", result["recommendation"])

    def test_2_distinct_accounts_recommends_do_not_build(self):
        records = probe.parse_records(FIXTURE_2_HITS)
        result = probe.analyze(self.conn, records,
                               source_status={"phmsa_enforcement_data": "ok"},
                               now=NOW)
        self.assertEqual(result["hit_count"], 2)
        self.assertIn("DO NOT BUILD U5", result["recommendation"])

    def test_non_midstream_lng_entity_record_is_excluded(self):
        records = probe.parse_records(tsv([
            ("Kinder Morgan", "Corrective Action Order", "3/1/26"),
            ("NextEra Energy", "Warning Letter", "3/1/26"),
        ]))
        result = probe.analyze(self.conn, records, now=NOW)
        self.assertEqual(result["hit_count"], 1)
        self.assertNotIn("E0005",
                         [h["entity_id"] for h in result["hits"]])
        self.assertEqual(result["excluded_non_midstream_count"], 1)

    def test_enforcement_action_older_than_24_months_is_excluded(self):
        records = probe.parse_records(tsv([
            ("Kinder Morgan", "Corrective Action Order", "1/1/22"),
        ]))
        result = probe.analyze(self.conn, records, now=NOW)
        self.assertEqual(result["hit_count"], 0)
        self.assertEqual(result["qualifying_records"], 0)
        self.assertEqual(result["total_records"], 1)

    def test_non_qualifying_case_type_is_excluded(self):
        records = probe.parse_records(tsv([
            ("Kinder Morgan", "Notice of Amendment", "3/1/26"),
            ("Williams Pipeline", "Safety Order", "3/1/26"),
        ]))
        result = probe.analyze(self.conn, records, now=NOW)
        self.assertEqual(result["hit_count"], 0)
        self.assertEqual(result["qualifying_records"], 0)

    def test_subsidiary_operator_name_is_review_queued_not_matched(self):
        """PHMSA names the operating subsidiary, not the parent watchlist
        name (module docstring's KTD5 discipline) -- "TARGA RESOURCES
        OPERATING LLC" against the seeded "Targa Resources" is a real shape
        observed live (fuzzy ratio 0.75, confirmed against app.resolve
        directly): below the 0.90 auto-match bar, above the 0.75 review
        bar. It must land in the review bucket, never silently auto-matched
        and never silently dropped as unmatched."""
        records = probe.parse_records(tsv([
            ("Targa Resources Operating LLC", "Warning Letter", "3/1/26"),
        ]))
        result = probe.analyze(self.conn, records, now=NOW)
        self.assertEqual(result["hit_count"], 0)
        self.assertEqual(result["unmatched_count"], 0)
        self.assertEqual(len(result["review"]), 1)
        entry = result["review"][0]
        self.assertEqual(entry["operator_name"], "Targa Resources Operating LLC")
        self.assertEqual(entry["reason"], "fuzzy_below_threshold")
        self.assertIn("E0004", [eid for eid, _, _ in entry["candidates"]])

    def test_every_record_undated_is_a_parse_anomaly_not_a_measured_zero(self):
        """A renamed/reshaped Opened_Date column would leave org+case_type
        intact but every opened_date == "" -- total_records still looks
        like a believable full fetch, so this must be flagged explicitly
        rather than reading as a genuine 0-hit measurement."""
        records = probe.parse_records(
            "Operator_Name\tCase_Type\tSome_Other_Date_Column\n"
            "Kinder Morgan\tCorrective Action Order\t3/1/26\n"
            "Williams Pipeline\tWarning Letter\t2/1/25\n")
        result = probe.analyze(self.conn, records, now=NOW)
        self.assertEqual(result["total_records"], 2)
        self.assertEqual(result["undated_count"], 2)
        self.assertEqual(result["hit_count"], 0)
        self.assertIn("PARSE ANOMALY", result["recommendation"])


class TestRecommendation(unittest.TestCase):
    OK = {"phmsa_enforcement_data": "ok"}
    BLOCKED = {"phmsa_enforcement_data":
               "BLOCKED (https://primis.phmsa.dot.gov/... -> 403 Forbidden: "
               "access denied)"}

    def test_at_or_above_threshold_recommends_build(self):
        self.assertIn("BUILD U5", probe.recommendation(3, self.OK))
        self.assertIn("BUILD U5", probe.recommendation(4, self.OK))

    def test_below_threshold_recommends_do_not_build(self):
        self.assertIn("DO NOT BUILD U5", probe.recommendation(0, self.OK))
        self.assertIn("DO NOT BUILD U5", probe.recommendation(2, self.OK))

    def test_blocked_source_is_not_a_measured_zero(self):
        text = probe.recommendation(0, self.BLOCKED)
        self.assertIn("BLOCKED MEASUREMENT", text)
        self.assertIn("phmsa_enforcement_data", text)
        self.assertIn("not because a completed search found nothing", text)

    def test_all_undated_is_a_parse_anomaly_not_a_measured_zero(self):
        text = probe.recommendation(0, self.OK, undated_count=50,
                                    total_records=50)
        self.assertIn("PARSE ANOMALY", text)
        self.assertIn("50", text)

    def test_some_undated_is_not_flagged_as_an_anomaly(self):
        """A handful of genuinely undated rows in an otherwise-parsing feed
        is normal and must not trip the all-undated guard."""
        text = probe.recommendation(3, self.OK, undated_count=2,
                                    total_records=50)
        self.assertNotIn("PARSE ANOMALY", text)


class TestLiveFetch(unittest.TestCase):
    """probe_enforcement_feed()/collect() are the only places a live request
    happens. Hermetic: urllib.request.urlopen is replaced with a fake that
    raises HTTPError(403), and no real socket is opened."""

    def _patch_urlopen(self, fake):
        original = probe.urllib.request.urlopen
        probe.urllib.request.urlopen = fake
        self.addCleanup(setattr, probe.urllib.request, "urlopen", original)

    def test_403_is_reported_as_blocked_with_endpoint_and_detail(self):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 403, "Forbidden", {},
                io.BytesIO(b"access denied"))
        self._patch_urlopen(fake_urlopen)
        status, body = probe.probe_enforcement_feed()
        self.assertIsNone(body)
        self.assertIn("BLOCKED", status)
        self.assertIn("403", status)
        self.assertIn(probe.RAW_DATA_URL, status)
        self.assertIn("access denied", status)

    def test_collect_never_yields_records_when_the_fetch_is_blocked(self):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 403, "Forbidden", {}, io.BytesIO(b""))
        self._patch_urlopen(fake_urlopen)
        records, status = probe.collect()
        self.assertEqual(records, [])
        self.assertIn("BLOCKED", status["phmsa_enforcement_data"])

    def test_a_403_never_crashes_report(self):
        """The block must be reported explicitly, not raised past the
        caller -- report() still prints a full response for a blocked
        fetch, it just prints hit_count=0 with the reason attached."""
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 403, "Forbidden", {}, io.BytesIO(b""))
        self._patch_urlopen(fake_urlopen)
        records, status = probe.collect()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "probe.db")
            conn = sqlite3.connect(db_path)
            apply_migrations(conn)
            seed(conn)
            conn.close()
            out = io.StringIO()
            result = probe.report(db_path, records, source_status=status,
                                  now=NOW, out=out)
        self.assertEqual(result["hit_count"], 0)
        self.assertIn("BLOCKED MEASUREMENT", out.getvalue())


class TestContainment(unittest.TestCase):
    """Same #74 precedent as ferc_elibrary_probe/breach_registry_probe: a
    measurement harness that could write is not a measurement, and one
    wired into the pipeline is a fetcher."""

    def test_report_opens_the_store_read_only_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "probe.db")
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON;")
            apply_migrations(conn)
            seed(conn)
            conn.close()

            before = os.path.getsize(db_path)
            out = io.StringIO()
            records = probe.parse_records(FIXTURE_4_HITS)
            result = probe.report(db_path, records,
                                  source_status={"phmsa_enforcement_data":
                                                 "ok"},
                                  now=NOW, out=out)
            self.assertEqual(result["hit_count"], 4)
            self.assertIn("HIT COUNT (distinct midstream/LNG watchlist "
                          "accounts): 4", out.getvalue())
            self.assertEqual(os.path.getsize(db_path), before)

            check = sqlite3.connect(db_path)
            try:
                for table in ("review_queue", "entity_match_decisions",
                              "signals", "raw_events"):
                    self.assertEqual(
                        check.execute(f"SELECT COUNT(*) FROM {table}")
                        .fetchone()[0], 0, f"{table} was written by the probe")
            finally:
                check.close()

    def test_read_only_connect_survives_a_uri_fragment_in_the_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "gridsignals#1.db")
            conn = sqlite3.connect(db_path)
            apply_migrations(conn)
            seed(conn)
            conn.close()

            ro = probe.read_only_connect(db_path)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    ro.execute("INSERT INTO watchlist_entities "
                               "(entity_id, name) VALUES ('EZZZ', 'Written')")
            finally:
                ro.close()

    def test_module_uses_a_read_only_connection_and_no_dml(self):
        self.assertIn("mode=ro", MODULE_SRC)
        for verb in ("INSERT ", "UPDATE ", "DELETE "):
            self.assertNotIn(verb, MODULE_SRC.upper())

    def test_probe_is_absent_from_the_ingest_pipeline(self):
        pipeline = PIPELINE.read_text(encoding="utf-8")
        self.assertNotIn("phmsa_probe", pipeline)
        self.assertNotIn("app.spikes", pipeline)

    def test_module_has_no_runner_cli_entry_point(self):
        self.assertNotIn("runner.cli", MODULE_SRC)


if __name__ == "__main__":
    unittest.main()
