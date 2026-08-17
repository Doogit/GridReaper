"""Tests for the CISA ICS advisories fetcher (R5.3, R9.6, R10.4).

Hermetic: in-memory SQLite via apply_migrations, FK on, HTTP mocked with a
canned ICS advisories RSS fixture. What must not regress: event shape,
pubDate window filtering, limit, and delta behavior on re-runs.
"""
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.db.migrate import apply_migrations
from app.ingest import cisa_ics, runner


def _rfc822(days_ago):
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    # CISA's feed uses a 2-digit year (e.g. "Thu, 13 Aug 26 12:00:00 +0000").
    return dt.strftime("%a, %d %b %y %H:%M:%S +0000")


def _item(advisory_id, days_ago, sectors="Energy"):
    return f"""<item>
  <title>ACME Widget Controller</title>
  <link>https://www.cisa.gov/news-events/ics-advisories/{advisory_id}</link>
  <description>&lt;p&gt;&lt;strong&gt;Vendor:&lt;/strong&gt; ACME&lt;/p&gt;
&lt;li&gt;&lt;strong&gt;Critical Infrastructure Sectors: &lt;/strong&gt;{sectors}&lt;/li&gt;
&lt;p&gt;CVE-2026-1234&lt;/p&gt;</description>
  <pubDate>{_rfc822(days_ago)}</pubDate>
  <dc:creator>CISA</dc:creator>
  <guid isPermaLink="false">/node/{advisory_id}</guid>
</item>"""


def _feed(items):
    body = "\n".join(items)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<channel><title>ICS Advisories</title>{body}</channel></rss>")


FIXTURE = _feed([
    _item("icsa-21-old-01", 1500, sectors="Energy"),
    _item("icsa-25-mid-02", 300, sectors="Information Technology"),
    _item("icsa-26-new-03", 10, sectors="Energy, Critical Manufacturing"),
])


class IcsTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO source_policies (source_id, name, ttl, enabled) "
            "VALUES ('cisa_ics', 'CISA ICS Advisories RSS', 86400, 1)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _events(self, fixture_text, window_days, limit=None):
        with mock.patch.object(cisa_ics, "_get_text",
                               return_value=fixture_text):
            return list(cisa_ics.fetch_events(self.conn, window_days, limit))


class TestFetchEvents(IcsTestCase):
    def test_event_shape(self):
        events = self._events(FIXTURE, 3650)
        self.assertEqual(len(events), 3)
        e = events[2]
        self.assertEqual(e["source_native_id"], "icsa-26-new-03")
        self.assertEqual(
            e["url"],
            "https://www.cisa.gov/news-events/ics-advisories/icsa-26-new-03")
        payload = json.loads(e["payload"])
        self.assertEqual(payload["title"], "ACME Widget Controller")
        self.assertIn("Critical Infrastructure Sectors", payload["description"])
        self.assertIn("Energy, Critical Manufacturing", payload["description"])
        self.assertTrue(e["event_date"])   # a real pubDate was parsed

    def test_window_filter(self):
        events = self._events(FIXTURE, 30)
        self.assertEqual([e["source_native_id"] for e in events],
                         ["icsa-26-new-03"])
        events = self._events(FIXTURE, 400)
        self.assertEqual([e["source_native_id"] for e in events],
                         ["icsa-25-mid-02", "icsa-26-new-03"])
        self.assertEqual(len(self._events(FIXTURE, 3650)), 3)

    def test_missing_pubdate_kept(self):
        feed = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<rss version="2.0"><channel>'
            '<item><title>No Date Advisory</title>'
            '<link>https://www.cisa.gov/news-events/ics-advisories/icsa-99-999-99</link>'
            '<description>no sectors line</description>'
            '<guid isPermaLink="false">/node/1</guid></item>'
            '</channel></rss>')
        events = self._events(feed, 30)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_date"], "")
        self.assertEqual(events[0]["source_native_id"], "icsa-99-999-99")

    def test_advisory_id_falls_back_to_guid_when_link_missing(self):
        feed = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<rss version="2.0"><channel>'
            '<item><title>No Link Advisory</title>'
            '<description>no sectors line</description>'
            '<guid isPermaLink="false">fallback-guid-1</guid></item>'
            '</channel></rss>')
        events = self._events(feed, 30)
        self.assertEqual(events[0]["source_native_id"], "fallback-guid-1")

    def test_advisory_id_strips_query_string_and_fragment(self):
        # R10.4: a tracking param or anchor on the link must not mint a
        # second raw_event for the same advisory.
        feed = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<rss version="2.0"><channel>'
            '<item><title>Tracked Link Advisory</title>'
            '<link>https://www.cisa.gov/news-events/ics-advisories/'
            'icsa-26-225-05?utm_source=rss#top</link>'
            '<description>no sectors line</description>'
            '<guid isPermaLink="false">/node/1</guid></item>'
            '</channel></rss>')
        events = self._events(feed, 30)
        self.assertEqual(events[0]["source_native_id"], "icsa-26-225-05")

    def test_advisory_id_falls_back_to_guid_for_bare_domain_link(self):
        # A link with no path segment (e.g. just the host) must not collapse
        # onto a hostname-shaped id; fall back to guid instead.
        feed = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<rss version="2.0"><channel>'
            '<item><title>Bare Domain Advisory</title>'
            '<link>https://www.cisa.gov/</link>'
            '<description>no sectors line</description>'
            '<guid isPermaLink="false">fallback-guid-2</guid></item>'
            '</channel></rss>')
        events = self._events(feed, 30)
        self.assertEqual(events[0]["source_native_id"], "fallback-guid-2")

    def test_limit(self):
        events = self._events(FIXTURE, 3650, limit=1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source_native_id"], "icsa-21-old-01")

    def test_limit_zero_skips_request(self):
        with mock.patch.object(cisa_ics, "_get_text") as get_text:
            events = list(cisa_ics.fetch_events(self.conn, 3650, 0))
        self.assertEqual(events, [])
        get_text.assert_not_called()


class TestRunSource(IcsTestCase):
    def test_run_and_delta_rerun(self):
        with mock.patch.object(cisa_ics, "_get_text", return_value=FIXTURE):
            summary = runner.run_source(
                self.conn, "cisa_ics", cisa_ics.fetch_events,
                cisa_ics.PARSER_VERSION, window_days=3650)
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["records_seen"], 3)
        self.assertEqual(summary["records_new"], 3)
        row = self.conn.execute(
            "SELECT * FROM raw_events "
            "WHERE raw_event_id='cisa_ics:icsa-26-new-03'").fetchone()
        self.assertEqual(row["source_native_id"], "icsa-26-new-03")

        # a later feed adds one advisory: only it is new (delta per R10.4)
        extended = _feed([
            _item("icsa-21-old-01", 1500, sectors="Energy"),
            _item("icsa-25-mid-02", 300, sectors="Information Technology"),
            _item("icsa-26-new-03", 10, sectors="Energy, Critical Manufacturing"),
            _item("icsa-26-newest-04", 1, sectors="Energy"),
        ])
        with mock.patch.object(cisa_ics, "_get_text", return_value=extended):
            summary = runner.run_source(
                self.conn, "cisa_ics", cisa_ics.fetch_events,
                cisa_ics.PARSER_VERSION, force=True, window_days=3650)
        self.assertEqual(summary["records_seen"], 4)
        self.assertEqual(summary["records_new"], 1)

    def test_http_failure_recorded_as_error(self):
        with mock.patch.object(
                cisa_ics, "_get_text",
                side_effect=OSError("connection refused")):
            summary = runner.run_source(
                self.conn, "cisa_ics", cisa_ics.fetch_events,
                cisa_ics.PARSER_VERSION, window_days=3650)
        self.assertEqual(summary["status"], "error")
        self.assertIn("connection refused", summary["error_state"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0],
            0)


if __name__ == "__main__":
    unittest.main()
