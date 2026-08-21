"""Tests for the PHMSA pipeline-enforcement fetcher (R6, R10.4).

Hermetic: in-memory SQLite via apply_migrations, FK on; the HTTP layer is
mocked by patching ``phmsa._fetch_feed_text`` directly (a single GET
returning the whole tab-delimited feed as text -- there is no pagination or
per-record request to intercept, unlike EDGAR/EPA ECHO).
"""
import json
import sqlite3
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.db.migrate import apply_migrations
from app.ingest import phmsa, runner

TODAY = datetime.now(timezone.utc).date()

HEADER = ("CPF_Number\tOperator_ID\tOperator_Name\tOperator_Searchable_Name\t"
          "Region\tPipeline_Type\tCase_Type\tViolation_Category\t"
          "Cited_Regulations\tOpened_Date")


def recent_mdy(days_ago):
    """days_ago -> PHMSA's own M/D/YY string for that date (portable: no
    platform-specific strftime no-leading-zero flag)."""
    d = TODAY - timedelta(days=days_ago)
    return f"{d.month}/{d.day}/{d.strftime('%y')}"


def recent_iso(days_ago):
    return (TODAY - timedelta(days=days_ago)).isoformat()


def row(cpf="1CAO", operator="Acme Pipeline LLC",
        case_type="Corrective Action Order", opened=None,
        violation="Integrity Management", regs="195.452(j)(3)"):
    opened = opened if opened is not None else recent_mdy(10)
    return "\t".join([cpf, "1", operator, operator, "Central",
                      "INTERSTATE LIQUID", case_type, violation, regs,
                      opened])


def feed_text(rows):
    return HEADER + "\n" + "\n".join(rows) + "\n"


class ParseRecordsTest(unittest.TestCase):
    def test_parses_full_row_shape(self):
        records = phmsa.parse_records(feed_text([row()]))
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r["CPF_Number"], "1CAO")
        self.assertEqual(r["Operator_Name"], "Acme Pipeline LLC")
        self.assertEqual(r["Case_Type"], "Corrective Action Order")
        self.assertEqual(r["Violation_Category"], "Integrity Management")

    def test_row_missing_cpf_number_is_dropped(self):
        records = phmsa.parse_records(feed_text([row(cpf="")]))
        self.assertEqual(records, [])

    def test_row_missing_operator_name_is_dropped(self):
        records = phmsa.parse_records(feed_text([row(operator="")]))
        self.assertEqual(records, [])

    def test_row_missing_case_type_is_dropped(self):
        records = phmsa.parse_records(feed_text([row(case_type="")]))
        self.assertEqual(records, [])

    def test_two_digit_year_pivots_to_2000s(self):
        self.assertEqual(phmsa._iso_date("3/1/26"), "2026-03-01")
        self.assertEqual(phmsa._iso_date("1/10/02"), "2002-01-10")

    def test_unparseable_date_returns_empty(self):
        self.assertEqual(phmsa._iso_date("not-a-date"), "")
        self.assertEqual(phmsa._iso_date(""), "")


class FetchEventsTest(unittest.TestCase):
    def _events(self, text, window_days=365, limit=None):
        with mock.patch.object(phmsa, "_fetch_feed_text", return_value=text):
            return list(phmsa.fetch_events(None, window_days, limit))

    def test_qualifying_case_type_in_window_yields_event(self):
        events = self._events(feed_text([row(opened=recent_mdy(10))]))
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["source_native_id"], "1CAO")
        self.assertEqual(e["event_date"], recent_iso(10))

    def test_event_dict_shape(self):
        events = self._events(feed_text([row(cpf="2NOPV",
                                              case_type="Notice of Probable Violation")]))
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["source_native_id"], "2NOPV")
        payload = json.loads(e["payload"])
        self.assertEqual(payload["Operator_Name"], "Acme Pipeline LLC")
        self.assertEqual(payload["Case_Type"], "Notice of Probable Violation")
        self.assertRegex(e["event_date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_non_qualifying_case_type_excluded(self):
        events = self._events(feed_text([
            row(cpf="3NOA", case_type="Notice of Amendment"),
            row(cpf="4SO", case_type="Safety Order"),
        ]))
        self.assertEqual(events, [])

    def test_out_of_window_case_excluded(self):
        old = recent_mdy(800)
        events = self._events(feed_text([row(opened=old)]), window_days=365)
        self.assertEqual(events, [])

    def test_unparseable_opened_date_excluded_regardless_of_window(self):
        # An unknown age is treated as out-of-window (matching
        # app.ingest.edgar/epa_echo's convention), not "always keep", even
        # with an effectively unbounded window.
        events = self._events(feed_text([row(opened="not-a-date")]),
                              window_days=36500)
        self.assertEqual(events, [])

    def test_malformed_row_skipped_not_crashed(self):
        text = feed_text([row(), row(cpf="")])   # second row malformed
        events = self._events(text)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source_native_id"], "1CAO")

    def test_limit_stops_early(self):
        rows = [row(cpf=f"{i}CAO") for i in range(1, 6)]
        events = self._events(feed_text(rows), limit=2)
        self.assertEqual(len(events), 2)

    def test_limit_zero_yields_nothing_and_never_fetches(self):
        with mock.patch.object(phmsa, "_fetch_feed_text") as fake_fetch:
            events = list(phmsa.fetch_events(None, 365, 0))
        self.assertEqual(events, [])
        fake_fetch.assert_not_called()

    def test_fetch_failure_propagates(self):
        # R10.3: a fetch error must propagate so run_source records it, not
        # be swallowed into a silent empty yield.
        with mock.patch.object(
                phmsa, "_fetch_feed_text",
                side_effect=urllib.error.URLError("boom")):
            with self.assertRaises(urllib.error.URLError):
                list(phmsa.fetch_events(None, 365, None))

    def test_parse_anomaly_raises_when_a_large_response_yields_zero_rows(self):
        # A WAF challenge / maintenance interstitial can return HTTP 200 with
        # a substantial body that simply doesn't tab-delimit into the
        # expected columns -- must raise (R10.3), never read as a genuine
        # empty feed (mirrors app/spikes/phmsa_probe.py's PARSE ANOMALY
        # discipline).
        html_interstitial = ("<html><body>Site temporarily unavailable for "
                             "scheduled maintenance. Please try again "
                             "later.</body></html>\n" * 20)
        self.assertGreater(len(html_interstitial),
                           phmsa.MIN_RESPONSE_BYTES_FOR_ANOMALY_CHECK)
        with mock.patch.object(phmsa, "_fetch_feed_text",
                              return_value=html_interstitial):
            with self.assertRaises(ValueError):
                list(phmsa.fetch_events(None, 365, None))

    def test_small_response_yielding_zero_rows_does_not_raise(self):
        # A header-only feed (no data rows at all) legitimately parses to
        # zero records; below the anomaly-check size floor, that is not
        # flagged as a shape mismatch.
        header_only = feed_text([])
        self.assertLess(len(header_only),
                        phmsa.MIN_RESPONSE_BYTES_FOR_ANOMALY_CHECK)
        events = self._events(header_only)
        self.assertEqual(events, [])


class RunSourceIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO source_policies (source_id, name, ttl, enabled) "
            "VALUES ('phmsa_enforcement', 'PHMSA', 86400, 1)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _run(self, text, force=False):
        with mock.patch.object(phmsa, "_fetch_feed_text", return_value=text):
            return runner.run_source(
                self.conn, phmsa.SOURCE_ID, phmsa.fetch_events,
                phmsa.PARSER_VERSION, force=force, window_days=3650)

    def test_run_and_idempotent_reingest(self):
        text = feed_text([row(cpf="1CAO")])
        summary = self._run(text)
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["records_seen"], 1)
        self.assertEqual(summary["records_new"], 1)
        stored = self.conn.execute(
            "SELECT * FROM raw_events WHERE raw_event_id = "
            "'phmsa_enforcement:1CAO'").fetchone()
        self.assertEqual(stored["source_native_id"], "1CAO")

        summary2 = self._run(text, force=True)
        self.assertEqual(summary2["status"], "success")
        self.assertEqual(summary2["records_seen"], 1)
        self.assertEqual(summary2["records_new"], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_events")
            .fetchone()[0], 1)

    def test_distinct_cpf_numbers_do_not_collide(self):
        text = feed_text([
            row(cpf="1CAO", operator="Acme Pipeline LLC",
               case_type="Corrective Action Order"),
            row(cpf="2CAO", operator="Acme Pipeline LLC",
               case_type="Corrective Action Order"),
        ])
        summary = self._run(text)
        self.assertEqual(summary["records_new"], 2)

    def test_fetch_error_records_run_as_error(self):
        with mock.patch.object(
                phmsa, "_fetch_feed_text",
                side_effect=urllib.error.URLError("boom")):
            summary = runner.run_source(
                self.conn, phmsa.SOURCE_ID, phmsa.fetch_events,
                phmsa.PARSER_VERSION, window_days=365)
        self.assertEqual(summary["status"], "error")
        self.assertIn("URLError", summary["error_state"])


if __name__ == "__main__":
    unittest.main()
