"""Tests for the EPA ECHO fetcher (R7, R10.4).

Hermetic: the HTTP layer is mocked with canned case_rest_services JSON
responses (shaped after live-verified 2026-08-18 API output); in-memory
SQLite via apply_migrations, FK enforcement on, no network.
"""
import json
import sqlite3
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

from app.db.migrate import apply_migrations
from app.ingest import epa_echo, runner


def _case(n, category="JDC", civil="CI",
          date_filed="01/01/2020", settlement="03/15/2020"):
    return {
        "CaseNumber": f"03-2020-{n:04d}",
        "CaseName": f"OIL COMPANY {n} (LEAD) (NATIONAL CASE)",
        "CaseCategoryCode": category,
        "CivilCriminalIndicator": civil,
        "PrimaryLaw": "CAA",
        "PrimaryNAICSCode": "211130",
        "DateFiled": date_filed,
        "SettlementDate": settlement,
        "EnfOutcome": "Final Order With Penalty",
    }


def _cases_response(qid, rows, message="Success"):
    return {"Results": {"Message": message, "QueryID": str(qid),
                        "QueryRows": str(rows)}}


def _qid_response(cases, message="Working"):
    return {"Results": {"Message": message, "Cases": cases}}


def _error_response(msg):
    return {"Results": {"Error": {"ErrorMessage": msg}}}


class FetchEventsTest(unittest.TestCase):
    def _events(self, responses, window_days=365, limit=None):
        """responses: canned _get_json return values, consumed in call
        order (NAICS get_cases, [NAICS get_qid page(s)], SIC get_cases,
        [SIC get_qid page(s)])."""
        urls = []

        def fake_get(url, retries=2):
            urls.append(url)
            return responses[len(urls) - 1]

        with mock.patch.object(epa_echo, "_get_json", fake_get), \
                mock.patch.object(epa_echo.time, "sleep"):
            events = list(epa_echo.fetch_events(None, window_days, limit))
        return events, urls

    def test_query_construction_naics_and_sic(self):
        responses = [_cases_response(1, 0), _cases_response(2, 0)]
        _, urls = self._events(responses)
        self.assertEqual(len(urls), 2)
        naics_params = parse_qs(urlparse(urls[0]).query)
        self.assertEqual(naics_params["p_naics"],
                         [",".join(epa_echo.NAICS_CODES)])
        self.assertEqual(naics_params["p_case_category"], ["JDC"])
        sic_params = parse_qs(urlparse(urls[1]).query)
        self.assertEqual(sic_params["p_sic"], [",".join(epa_echo.SIC_CODES)])

    def test_pagination_walks_get_qid(self):
        cases = [_case(i) for i in range(1, 4)]
        responses = [
            _cases_response(101, 3),
            _qid_response(cases),
            _cases_response(102, 0),
        ]
        events, urls = self._events(responses, window_days=3650)
        self.assertEqual(len(events), 3)
        self.assertIn("qid=101", urls[1])
        self.assertIn("pageno=1", urls[1])

    def test_naics_and_sic_queries_can_yield_the_same_case(self):
        # Dedupe across the two queries is the runner's job (source_native_id
        # keying); the fetcher itself just yields what each query returns.
        responses = [
            _cases_response(1, 1), _qid_response([_case(1)]),
            _cases_response(2, 1), _qid_response([_case(1)]),
        ]
        events, _ = self._events(responses, window_days=3650)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["source_native_id"],
                         events[1]["source_native_id"])

    def test_malformed_record_missing_case_number_skipped(self):
        bad = _case(1)
        del bad["CaseNumber"]
        responses = [
            _cases_response(1, 1), _qid_response([bad]),
            _cases_response(2, 0),
        ]
        events, _ = self._events(responses, window_days=3650)
        self.assertEqual(events, [])

    def test_window_days_filters_old_cases(self):
        old = _case(1, date_filed="01/01/2010", settlement="01/01/2010")
        responses = [
            _cases_response(1, 1), _qid_response([old]),
            _cases_response(2, 0),
        ]
        events, _ = self._events(responses, window_days=30)
        self.assertEqual(events, [])

    def test_unparseable_dates_excluded_regardless_of_window(self):
        # A record whose age can't be determined is treated as out-of-window
        # (matching app.ingest.edgar's convention), not as "always keep" --
        # an unbounded window_days must not let it through either.
        bad_dates = _case(1, date_filed="not-a-date", settlement="")
        responses = [
            _cases_response(1, 1), _qid_response([bad_dates]),
            _cases_response(2, 0),
        ]
        events, _ = self._events(responses, window_days=36500)
        self.assertEqual(events, [])

    def test_non_dict_case_record_skipped(self):
        responses = [
            _cases_response(1, 1), _qid_response(["not-a-dict"]),
            _cases_response(2, 0),
        ]
        events, _ = self._events(responses, window_days=3650)
        self.assertEqual(events, [])

    def test_unexpected_message_raises(self):
        responses = [{"Results": {"Message": "Failed", "QueryID": "1",
                                  "QueryRows": "1"}}]
        with self.assertRaises(ValueError):
            self._events(responses)

    def test_pagination_truncation_raises(self):
        # QueryRows implies 6 pages; MAX_PAGES caps the fetch at 5. A FULL
        # final page at the cap means more rows exist than were fetched --
        # this must raise (R10.3 error), never silently report "success"
        # with an incomplete result.
        full_page = [{"CaseNumber": f"X{i}"} for i in range(epa_echo.RESPONSESET)]
        stub_page = [{"CaseNumber": "X0"}]
        responses = [
            _cases_response(1, epa_echo.RESPONSESET * 6),
            _qid_response(stub_page), _qid_response(stub_page),
            _qid_response(stub_page), _qid_response(stub_page),
            _qid_response(full_page),
        ]
        with self.assertRaises(ValueError):
            self._events(responses, window_days=3650)

    def test_event_date_prefers_settlement_over_filed(self):
        c = _case(1, date_filed="01/01/2020", settlement="03/15/2020")
        responses = [
            _cases_response(1, 1), _qid_response([c]),
            _cases_response(2, 0),
        ]
        events, _ = self._events(responses, window_days=3650)
        self.assertEqual(events[0]["event_date"], "2020-03-15")

    def test_event_dict_shape(self):
        c = _case(7)
        responses = [
            _cases_response(1, 1), _qid_response([c]),
            _cases_response(2, 0),
        ]
        events, _ = self._events(responses, window_days=3650)
        e = events[0]
        self.assertEqual(e["source_native_id"], "03-2020-0007")
        self.assertEqual(e["event_date"], "2020-03-15")
        payload = json.loads(e["payload"])
        self.assertEqual(payload["CaseName"], c["CaseName"])

    def test_limit_stops_before_second_query(self):
        cases = [_case(i) for i in range(1, 4)]
        responses = [_cases_response(1, 3), _qid_response(cases)]
        events, urls = self._events(responses, window_days=3650, limit=2)
        self.assertEqual(len(events), 2)
        self.assertEqual(len(urls), 2)   # never reaches the SIC query

    def test_limit_zero_skips_requests(self):
        events, urls = self._events([], limit=0)
        self.assertEqual(events, [])
        self.assertEqual(urls, [])

    def test_api_error_propagates(self):
        responses = [_error_response("Rows Returned would be too many")]
        with self.assertRaises(ValueError):
            self._events(responses)


class RunSourceIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO source_policies (source_id, name, ttl, enabled) "
            "VALUES ('epa_echo', 'EPA ECHO', 86400, 1)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _run(self, force=False):
        c = _case(1)
        responses = [
            _cases_response(1, 1), _qid_response([c]),
            _cases_response(2, 1), _qid_response([dict(c)]),
        ]
        calls = {"n": 0}

        def fake_get(url, retries=2):
            resp = responses[calls["n"] % len(responses)]
            calls["n"] += 1
            return resp

        with mock.patch.object(epa_echo, "_get_json", fake_get), \
                mock.patch.object(epa_echo.time, "sleep"):
            return runner.run_source(
                self.conn, epa_echo.SOURCE_ID, epa_echo.fetch_events,
                epa_echo.PARSER_VERSION, force=force, window_days=3650)

    def test_run_and_dedupe(self):
        summary = self._run()
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["records_seen"], 2)
        self.assertEqual(summary["records_new"], 1)
        row = self.conn.execute(
            "SELECT * FROM raw_events "
            "WHERE raw_event_id='epa_echo:03-2020-0001'").fetchone()
        self.assertEqual(row["source_native_id"], "03-2020-0001")

        summary = self._run(force=True)
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["records_seen"], 2)
        self.assertEqual(summary["records_new"], 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM raw_events").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
