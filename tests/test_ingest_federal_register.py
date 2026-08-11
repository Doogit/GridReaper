"""Tests for the Federal Register fetcher (R5.5, R3.7, R10.4).

Hermetic: the HTTP layer is mocked with canned documents.json responses;
in-memory SQLite via apply_migrations, FK enforcement on, no network.
"""
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

from app.db.migrate import apply_migrations
from app.ingest import federal_register, runner


def _doc(n):
    return {
        "document_number": f"2026-{n:05d}",
        "title": f"Order No. {n}",
        "type": "Notice",
        "abstract": "Grid reliability filing.",
        "publication_date": "2026-08-03",
        "agency_names": ["Federal Energy Regulatory Commission"],
        "docket_ids": [f"RM26-{n}-000"],
        "html_url": f"https://www.federalregister.gov/d/2026-{n:05d}",
    }


def _page(docs, next_page_url=None):
    page = {"count": len(docs), "results": docs}
    if next_page_url:
        page["next_page_url"] = next_page_url
    return page


class FetchEventsTest(unittest.TestCase):
    def _events(self, pages, window_days=365, limit=None):
        """Run fetch_events against canned pages; returns (events, urls)."""
        urls = []

        def fake_get(url, retries=2):
            urls.append(url)
            return pages[len(urls) - 1]

        with mock.patch.object(federal_register, "_get_json", fake_get), \
                mock.patch.object(federal_register.time, "sleep"):
            events = list(federal_register.fetch_events(
                None, window_days, limit))
        return events, urls

    def test_query_construction(self):
        _, urls = self._events([_page([])], window_days=30)
        params = parse_qs(urlparse(urls[0]).query)
        self.assertEqual(sorted(params["conditions[agencies][]"]),
                         ["federal-energy-regulatory-commission",
                          "transportation-security-administration"])
        since = (datetime.now(timezone.utc) - timedelta(days=30)).date()
        self.assertEqual(params["conditions[publication_date][gte]"],
                         [since.isoformat()])
        self.assertEqual(params["per_page"], ["100"])
        for field in ("document_number", "abstract", "docket_ids",
                      "cfr_references", "html_url"):
            self.assertIn(field, params["fields[]"])

    def test_pagination_follows_next_page_url(self):
        pages = [_page([_doc(1), _doc(2)], "https://api.example/page2"),
                 _page([_doc(3)])]
        events, urls = self._events(pages)
        self.assertEqual(len(events), 3)
        self.assertEqual(len(urls), 2)
        self.assertEqual(urls[1], "https://api.example/page2")

    def test_limit_stops_mid_page(self):
        pages = [_page([_doc(1), _doc(2), _doc(3)], "https://api.example/p2")]
        events, urls = self._events(pages, limit=2)
        self.assertEqual(len(events), 2)
        self.assertEqual(len(urls), 1)   # never fetches page 2

    def test_limit_zero_skips_requests(self):
        events, urls = self._events([_page([_doc(1)])], limit=0)
        self.assertEqual(events, [])
        self.assertEqual(urls, [])

    def test_event_dict_shape(self):
        events, _ = self._events([_page([_doc(7)])])
        e = events[0]
        self.assertEqual(e["source_native_id"], "2026-00007")
        self.assertEqual(e["event_date"], "2026-08-03")
        self.assertEqual(e["url"], "https://www.federalregister.gov/d/2026-00007")
        self.assertEqual(e["canonical_url"], e["url"])
        payload = json.loads(e["payload"])
        self.assertEqual(payload["docket_ids"], ["RM26-7-000"])
        self.assertEqual(payload["title"], "Order No. 7")


class RunSourceIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO source_policies (source_id, name, ttl, enabled) "
            "VALUES ('federal_register', 'Federal Register API', 86400, 1)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _run(self, force=False):
        pages = [_page([_doc(1), _doc(2)], "https://api.example/p2"),
                 _page([_doc(3)])]
        calls = {"n": 0}

        def fake_get(url, retries=2):
            page = pages[calls["n"] % len(pages)]
            calls["n"] += 1
            return page

        with mock.patch.object(federal_register, "_get_json", fake_get), \
                mock.patch.object(federal_register.time, "sleep"):
            return runner.run_source(
                self.conn, federal_register.SOURCE_ID,
                federal_register.fetch_events,
                federal_register.PARSER_VERSION, force=force)

    def test_run_and_dedupe(self):
        summary = self._run()
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["records_seen"], 3)
        self.assertEqual(summary["records_new"], 3)
        row = self.conn.execute(
            "SELECT * FROM raw_events "
            "WHERE raw_event_id='federal_register:2026-00001'").fetchone()
        self.assertEqual(row["source_native_id"], "2026-00001")
        self.assertEqual(row["event_date"], "2026-08-03")

        summary = self._run(force=True)
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["records_seen"], 3)
        self.assertEqual(summary["records_new"], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
