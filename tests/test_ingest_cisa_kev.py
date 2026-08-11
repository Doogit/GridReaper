"""Tests for the CISA KEV fetcher (R5.1, R10.4).

Hermetic: in-memory SQLite via apply_migrations, FK on, HTTP mocked with a
canned KEV feed fixture. What must not regress: event shape, dateAdded
window filtering, limit, and delta behavior on re-runs.
"""
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.db.migrate import apply_migrations
from app.ingest import cisa_kev, runner


def _day(days_ago):
    return (datetime.now(timezone.utc)
            - timedelta(days=days_ago)).date().isoformat()


def _vuln(cve, days_ago):
    return {
        "cveID": cve,
        "vendorProject": "Acme",
        "product": "Widget",
        "vulnerabilityName": f"{cve} vuln",
        "dateAdded": _day(days_ago),
        "shortDescription": "desc",
        "requiredAction": "patch",
        "dueDate": _day(days_ago - 21),
        "knownRansomwareCampaignUse": "Unknown",
    }


FIXTURE = {
    "title": "CISA Catalog of Known Exploited Vulnerabilities",
    "catalogVersion": "2026.08.11",
    "count": 3,
    "vulnerabilities": [
        _vuln("CVE-2021-0001", 1500),
        _vuln("CVE-2025-1111", 300),
        _vuln("CVE-2026-2222", 10),
    ],
}


class KevTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO source_policies (source_id, name, ttl, enabled) "
            "VALUES ('cisa_kev', 'CISA KEV JSON', 86400, 1)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _events(self, fixture, window_days, limit=None):
        with mock.patch.object(cisa_kev, "_get_json", return_value=fixture):
            return list(cisa_kev.fetch_events(self.conn, window_days, limit))


class TestFetchEvents(KevTestCase):
    def test_event_shape(self):
        events = self._events(FIXTURE, 3650)
        self.assertEqual(len(events), 3)
        e = events[2]
        self.assertEqual(e["source_native_id"], "CVE-2026-2222")
        self.assertEqual(e["event_date"], _day(10))
        self.assertEqual(e["url"], cisa_kev.CATALOG_URL)
        # payload round-trips to the source's own record (R3.7)
        self.assertEqual(json.loads(e["payload"]),
                         FIXTURE["vulnerabilities"][2])

    def test_window_filter(self):
        events = self._events(FIXTURE, 30)
        self.assertEqual([e["source_native_id"] for e in events],
                         ["CVE-2026-2222"])
        events = self._events(FIXTURE, 400)
        self.assertEqual([e["source_native_id"] for e in events],
                         ["CVE-2025-1111", "CVE-2026-2222"])
        self.assertEqual(len(self._events(FIXTURE, 3650)), 3)

    def test_missing_date_added_kept(self):
        fixture = {"vulnerabilities": [{"cveID": "CVE-2026-9999"}]}
        events = self._events(fixture, 30)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_date"], "")

    def test_limit(self):
        events = self._events(FIXTURE, 3650, limit=1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source_native_id"], "CVE-2021-0001")

    def test_limit_zero_skips_request(self):
        with mock.patch.object(cisa_kev, "_get_json") as get_json:
            events = list(cisa_kev.fetch_events(self.conn, 3650, 0))
        self.assertEqual(events, [])
        get_json.assert_not_called()


class TestRunSource(KevTestCase):
    def test_run_and_delta_rerun(self):
        with mock.patch.object(cisa_kev, "_get_json", return_value=FIXTURE):
            summary = runner.run_source(
                self.conn, "cisa_kev", cisa_kev.fetch_events,
                cisa_kev.PARSER_VERSION, window_days=3650)
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["records_seen"], 3)
        self.assertEqual(summary["records_new"], 3)
        row = self.conn.execute(
            "SELECT * FROM raw_events "
            "WHERE raw_event_id='cisa_kev:CVE-2026-2222'").fetchone()
        self.assertEqual(row["source_native_id"], "CVE-2026-2222")

        # a later catalog adds one CVE: only it is new (delta per R5.1/R10.4)
        extended = dict(FIXTURE)
        extended["vulnerabilities"] = (
            FIXTURE["vulnerabilities"] + [_vuln("CVE-2026-3333", 2)])
        with mock.patch.object(cisa_kev, "_get_json", return_value=extended):
            summary = runner.run_source(
                self.conn, "cisa_kev", cisa_kev.fetch_events,
                cisa_kev.PARSER_VERSION, force=True, window_days=3650)
        self.assertEqual(summary["records_seen"], 4)
        self.assertEqual(summary["records_new"], 1)

    def test_http_failure_recorded_as_error(self):
        with mock.patch.object(
                cisa_kev, "_get_json",
                side_effect=OSError("connection refused")):
            summary = runner.run_source(
                self.conn, "cisa_kev", cisa_kev.fetch_events,
                cisa_kev.PARSER_VERSION, window_days=3650)
        self.assertEqual(summary["status"], "error")
        self.assertIn("connection refused", summary["error_state"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0],
            0)


if __name__ == "__main__":
    unittest.main()
