"""Tests for the NVD CVE API 2.0 fetcher (R5.3, R10.4).

Hermetic: in-memory SQLite via apply_migrations, FK on, HTTP mocked with
canned API 2.0 responses, time.sleep mocked. What must not regress: event
shape (native id = cve.id, published -> event_date, payload = inner cve
object), startIndex/resultsPerPage pagination, the 120-day window split,
limit handling, and delta behavior on re-runs.
"""
import json
import sqlite3
import unittest
import urllib.parse
from unittest import mock

from app.db.migrate import apply_migrations
from app.ingest import nvd, runner


def _cve(cve_id, published="2026-06-01T12:00:00.000"):
    return {
        "cve": {
            "id": cve_id,
            "published": published,
            "lastModified": "2026-06-02T00:00:00.000",
            "descriptions": [{"lang": "en", "value": f"{cve_id} desc"}],
        }
    }


def _page(cves, total, results_per_page=nvd.PAGE_SIZE, start_index=0):
    return {
        "vulnerabilities": cves,
        "totalResults": total,
        "resultsPerPage": results_per_page,
        "startIndex": start_index,
    }


ONE_PAGE = _page([_cve("CVE-2026-0001"), _cve("CVE-2026-0002")], total=2)


class NvdTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO source_policies (source_id, name, ttl, enabled) "
            "VALUES ('nvd', 'NVD API 2.0', 86400, 1)")
        self.conn.commit()
        # never sleep for real in tests
        self._sleep = mock.patch.object(nvd.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        self.conn.close()

    def _events(self, responses, window_days, limit=None):
        """Run fetch_events with _get_json returning `responses` in order."""
        with mock.patch.object(
                nvd, "_get_json",
                side_effect=list(responses)) as get_json:
            events = list(nvd.fetch_events(self.conn, window_days, limit))
        return events, get_json


def _query(call):
    """Parse the query string of a _get_json(url) positional call."""
    url = call.args[0]
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


class TestFetchEvents(NvdTestCase):
    def test_event_shape(self):
        events, _ = self._events([ONE_PAGE], window_days=90)
        self.assertEqual(len(events), 2)
        e = events[0]
        self.assertEqual(e["source_native_id"], "CVE-2026-0001")
        self.assertEqual(e["event_date"], "2026-06-01")
        self.assertEqual(e["url"],
                         "https://nvd.nist.gov/vuln/detail/CVE-2026-0001")
        # payload round-trips to the INNER cve object (not the wrapper), R3.7
        self.assertEqual(json.loads(e["payload"]),
                         ONE_PAGE["vulnerabilities"][0]["cve"])

    def test_pagination(self):
        # total 3 across two pages of size 2
        page1 = _page([_cve("CVE-2026-0001"), _cve("CVE-2026-0002")],
                      total=3, results_per_page=2, start_index=0)
        page2 = _page([_cve("CVE-2026-0003")],
                      total=3, results_per_page=2, start_index=2)
        events, get_json = self._events([page1, page2], window_days=90)
        self.assertEqual([e["source_native_id"] for e in events],
                         ["CVE-2026-0001", "CVE-2026-0002", "CVE-2026-0003"])
        self.assertEqual(get_json.call_count, 2)
        # second request advances startIndex by the page size
        self.assertEqual(_query(get_json.call_args_list[1])["startIndex"], "2")

    def test_exact_multiple_termination(self):
        # total=4, two full pages of 2: exactly 2 requests, no phantom 3rd
        page1 = _page([_cve("CVE-2026-0001"), _cve("CVE-2026-0002")],
                      total=4, results_per_page=2, start_index=0)
        page2 = _page([_cve("CVE-2026-0003"), _cve("CVE-2026-0004")],
                      total=4, results_per_page=2, start_index=2)
        events, get_json = self._events([page1, page2], window_days=90)
        self.assertEqual(len(events), 4)
        self.assertEqual(get_json.call_count, 2)

    def test_short_page_no_cves_skipped(self):
        # NVD advertises resultsPerPage=2 but returns only 1 record on page 1
        # (partial response under load). Advancing by the advertised size would
        # skip CVE-0002; advancing by len(records) must yield all 3.
        page1 = _page([_cve("CVE-2026-0001")],
                      total=3, results_per_page=2, start_index=0)
        page2 = _page([_cve("CVE-2026-0002")],
                      total=3, results_per_page=2, start_index=1)
        page3 = _page([_cve("CVE-2026-0003")],
                      total=3, results_per_page=2, start_index=2)
        events, get_json = self._events([page1, page2, page3], window_days=90)
        self.assertEqual([e["source_native_id"] for e in events],
                         ["CVE-2026-0001", "CVE-2026-0002", "CVE-2026-0003"])
        self.assertEqual(get_json.call_count, 3)
        # startIndex advances by the real count (1) each time, not by 2
        self.assertEqual(_query(get_json.call_args_list[1])["startIndex"], "1")
        self.assertEqual(_query(get_json.call_args_list[2])["startIndex"], "2")

    def test_empty_page_guard_stops_loop(self):
        # zero records while start_index (0) < total (5): no progress possible,
        # so the sub-range must stop rather than loop forever.
        empty = _page([], total=5, results_per_page=2, start_index=0)
        events, get_json = self._events([empty], window_days=90)
        self.assertEqual(events, [])
        self.assertEqual(get_json.call_count, 1)

    def test_window_split_over_120_days(self):
        # 300 days -> three sub-ranges (120 + 120 + 60), one empty page each
        empty = _page([], total=0)
        events, get_json = self._events([empty, empty, empty], window_days=300)
        self.assertEqual(events, [])
        self.assertEqual(get_json.call_count, 3)
        # sub-ranges are consecutive and each <= 120 days
        starts = [_query(c)["pubStartDate"] for c in get_json.call_args_list]
        ends = [_query(c)["pubEndDate"] for c in get_json.call_args_list]
        self.assertEqual(starts[1], ends[0])   # contiguous
        self.assertEqual(starts[2], ends[1])
        for s, e in zip(starts, ends):
            d0 = nvd.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.000")
            d1 = nvd.datetime.strptime(e, "%Y-%m-%dT%H:%M:%S.000")
            self.assertLessEqual((d1 - d0).days, nvd.MAX_WINDOW_DAYS)

    def test_window_under_120_days_single_range(self):
        events, get_json = self._events([ONE_PAGE], window_days=90)
        self.assertEqual(len(events), 2)
        self.assertEqual(get_json.call_count, 1)

    def test_limit(self):
        events, _ = self._events([ONE_PAGE], window_days=90, limit=1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source_native_id"], "CVE-2026-0001")

    def test_limit_zero_skips_request(self):
        with mock.patch.object(nvd, "_get_json") as get_json:
            events = list(nvd.fetch_events(self.conn, 90, 0))
        self.assertEqual(events, [])
        get_json.assert_not_called()


class TestRunSource(NvdTestCase):
    def test_run_and_delta_rerun(self):
        with mock.patch.object(nvd, "_get_json", return_value=ONE_PAGE):
            summary = runner.run_source(
                self.conn, "nvd", nvd.fetch_events,
                nvd.PARSER_VERSION, window_days=90)
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["records_seen"], 2)
        self.assertEqual(summary["records_new"], 2)
        row = self.conn.execute(
            "SELECT * FROM raw_events "
            "WHERE raw_event_id='nvd:CVE-2026-0001'").fetchone()
        self.assertEqual(row["source_native_id"], "CVE-2026-0001")

        # re-fetch the same page: nothing new (delta per R5.3/R10.4)
        with mock.patch.object(nvd, "_get_json", return_value=ONE_PAGE):
            summary = runner.run_source(
                self.conn, "nvd", nvd.fetch_events,
                nvd.PARSER_VERSION, force=True, window_days=90)
        self.assertEqual(summary["records_seen"], 2)
        self.assertEqual(summary["records_new"], 0)

    def test_http_failure_recorded_as_error(self):
        with mock.patch.object(
                nvd, "_get_json",
                side_effect=OSError("connection refused")):
            summary = runner.run_source(
                self.conn, "nvd", nvd.fetch_events,
                nvd.PARSER_VERSION, window_days=90)
        self.assertEqual(summary["status"], "error")
        self.assertIn("connection refused", summary["error_state"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0],
            0)


if __name__ == "__main__":
    unittest.main()
