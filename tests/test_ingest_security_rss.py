"""Tests for security-press RSS ingestion (R3.7, R10.4, R9.6).

Hermetic: canned RSS XML via a mocked HTTP layer, in-memory SQLite with
FK enforcement, no network.
"""
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest import mock

from app.db.migrate import apply_migrations
from app.ingest import runner, security_rss


def _rss(items):
    body = "".join(items)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<rss version="2.0"><channel><title>Feed</title>{body}'
            "</channel></rss>")


def _item(title="Threat actor breaches vendor", guid="",
          link="https://sec.example/a", pubdate="", description="Body text.",
          categories=()):
    parts = [f"<title>{title}</title>"]
    if link:
        parts.append(f"<link>{link}</link>")
    if guid:
        parts.append(f"<guid isPermaLink=\"true\">{guid}</guid>")
    if pubdate:
        parts.append(f"<pubDate>{pubdate}</pubDate>")
    parts.append(f"<description>{description}</description>")
    parts += [f"<category>{c}</category>" for c in categories]
    return f"<item>{''.join(parts)}</item>"


def _recent_pubdate(days_ago=1):
    return format_datetime(datetime.now(timezone.utc) - timedelta(days=days_ago))


class TestFeedParsing(unittest.TestCase):
    def _events(self, xml, window_days=365, limit=None):
        with mock.patch.object(security_rss, "_get_text", return_value=xml):
            return list(security_rss._fetch_feed_events(
                ["https://feed.example/rss"], window_days, limit))

    def test_guid_used_link_fallback(self):
        xml = _rss([_item(guid="https://sec.example/post-123",
                          link="https://sec.example/a",
                          pubdate=_recent_pubdate()),
                    _item(guid="", link="https://sec.example/b",
                          pubdate=_recent_pubdate())])
        events = self._events(xml)
        self.assertEqual([e["source_native_id"] for e in events],
                         ["https://sec.example/post-123", "https://sec.example/b"])

    def test_pubdate_normalized_to_utc(self):
        xml = _rss([_item(guid="g1",
                          pubdate="Wed, 13 Aug 2026 14:15:19 -0400")])
        events = self._events(xml, window_days=36500)
        self.assertEqual(events[0]["event_date"], "2026-08-13T18:15:19+00:00")

    def test_unparseable_pubdate_kept_with_empty_date(self):
        xml = _rss([_item(guid="g1", pubdate="not a date")])
        events = self._events(xml)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_date"], "")

    def test_window_filter_excludes_old_items(self):
        xml = _rss([_item(guid="old", pubdate="Tue, 07 Jan 2020 10:00:00 GMT"),
                    _item(guid="new", pubdate=_recent_pubdate())])
        events = self._events(xml, window_days=30)
        self.assertEqual([e["source_native_id"] for e in events], ["new"])

    def test_payload_fields_complete(self):
        xml = _rss([_item(title="T", guid="g1", link="https://sec.example/a",
                          pubdate=_recent_pubdate(), description="D",
                          categories=("Cybercrime", "Ransomware"))])
        events = self._events(xml)
        payload = json.loads(events[0]["payload"])
        self.assertEqual(payload["title"], "T")
        self.assertEqual(payload["description"], "D")
        self.assertEqual(payload["link"], "https://sec.example/a")
        self.assertEqual(payload["guid"], "g1")
        self.assertIn("pubDate", payload)
        self.assertEqual(payload["categories"], ["Cybercrime", "Ransomware"])
        self.assertEqual(events[0]["url"], "https://sec.example/a")

    def test_non_ascii_payload_preserved(self):
        # RSS security copy is smart-quote / em-dash heavy; store UTF-8 as-is.
        xml = _rss([_item(title="Acme’s OT network — breached",
                          guid="g1", pubdate=_recent_pubdate())])
        events = self._events(xml)
        payload = json.loads(events[0]["payload"])
        self.assertEqual(payload["title"], "Acme’s OT network — breached")

    def test_limit_stops_across_feeds(self):
        xml = _rss([_item(guid=f"g{i}", pubdate=_recent_pubdate())
                    for i in range(5)])
        with mock.patch.object(security_rss, "_get_text", return_value=xml):
            events = list(security_rss._fetch_feed_events(
                ["https://a.example/rss", "https://b.example/rss"], 365, 3))
        self.assertEqual(len(events), 3)

    def test_limit_zero_skips_requests(self):
        with mock.patch.object(security_rss, "_get_text") as get_text:
            events = list(security_rss._fetch_feed_events(
                ["https://a.example/rss"], 365, 0))
        self.assertEqual(events, [])
        get_text.assert_not_called()

    def test_wrappers_hit_their_own_feed_lists(self):
        requested = []

        def record(url, retries=2):
            requested.append(url)
            return _rss([])

        with mock.patch.object(security_rss, "_get_text", side_effect=record):
            list(security_rss.fetch_events_therecord(None, 365, None))
            record_feed = list(requested)
            requested.clear()
            list(security_rss.fetch_events_bleepingcomputer(None, 365, None))
            bc_feed = list(requested)
        self.assertEqual(record_feed, security_rss.THE_RECORD_FEEDS)
        self.assertEqual(bc_feed, security_rss.BLEEPINGCOMPUTER_FEEDS)


class TestRunSourceIntegration(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        for source_id in ("the_record", "bleepingcomputer"):
            self.conn.execute(
                "INSERT INTO source_policies (source_id, name, ttl, enabled) "
                "VALUES (?, ?, 3600, 1)", (source_id, source_id))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_run_and_dedupe_to_zero_new(self):
        xml = _rss([_item(guid="g1", pubdate=_recent_pubdate()),
                    _item(guid="g2", pubdate=_recent_pubdate())])
        with mock.patch.object(security_rss, "_get_text", return_value=xml):
            first = runner.run_source(
                self.conn, "the_record",
                security_rss.fetch_events_therecord, security_rss.PARSER_VERSION)
            second = runner.run_source(
                self.conn, "the_record",
                security_rss.fetch_events_therecord, security_rss.PARSER_VERSION,
                force=True)
        self.assertEqual(first["status"], "success")
        self.assertEqual(first["records_new"], 2)
        self.assertEqual(second["status"], "success")
        self.assertEqual(second["records_new"], 0)   # guid dedupe holds
        row = self.conn.execute(
            "SELECT raw_event_id FROM raw_events ORDER BY raw_event_id"
        ).fetchall()
        self.assertEqual([r["raw_event_id"] for r in row],
                         ["the_record:g1", "the_record:g2"])

    def test_feed_http_failure_records_error(self):
        with mock.patch.object(security_rss, "_get_text",
                               side_effect=OSError("connection refused")):
            summary = runner.run_source(
                self.conn, "bleepingcomputer",
                security_rss.fetch_events_bleepingcomputer,
                security_rss.PARSER_VERSION)
        self.assertEqual(summary["status"], "error")
        self.assertIn("connection refused", summary["error_state"])


if __name__ == "__main__":
    unittest.main()
