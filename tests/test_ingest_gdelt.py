"""Tests for the GDELT DOC API fetcher (R5.x, R10.4).

Hermetic: in-memory SQLite via apply_migrations, FK on, HTTP mocked with a
canned GDELT articles fixture. What must not regress: event shape (native
id=url, event_date parsed from seendate, full record round-trips), seendate
window filtering, limit, empty-response handling, and delta behavior on
re-runs.
"""
import io
import json
import sqlite3
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.db.migrate import apply_migrations
from app.ingest import gdelt, runner


def _seendate(days_ago):
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _iso_day(days_ago):
    return (datetime.now(timezone.utc)
            - timedelta(days=days_ago)).date().isoformat()


def _article(slug, days_ago):
    return {
        "url": f"https://news.example.com/{slug}",
        "url_mobile": "",
        "title": f"{slug} headline",
        "seendate": _seendate(days_ago),
        "domain": "news.example.com",
        "language": "English",
        "sourcecountry": "United States",
        "socialimage": f"https://img.example.com/{slug}.jpg",
    }


FIXTURE = {
    "articles": [
        _article("grid-modernization", 80),
        _article("nuclear-restart", 10),
        _article("utility-outage", 1),
    ],
}


class GdeltTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO source_policies (source_id, name, ttl, enabled) "
            "VALUES ('gdelt', 'GDELT 2.0 DOC API', 86400, 1)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _events(self, fixture, window_days, limit=None):
        with mock.patch.object(gdelt, "_get_json", return_value=fixture):
            return list(gdelt.fetch_events(self.conn, window_days, limit))


class TestFetchEvents(GdeltTestCase):
    def test_event_shape(self):
        events = self._events(FIXTURE, 3650)
        self.assertEqual(len(events), 3)
        e = events[1]
        art = FIXTURE["articles"][1]
        self.assertEqual(e["source_native_id"], art["url"])
        self.assertEqual(e["url"], art["url"])
        self.assertEqual(e["event_date"], _iso_day(10))
        # payload round-trips to the source's own record (R3.7), sorted keys
        self.assertEqual(json.loads(e["payload"]), art)
        self.assertEqual(e["payload"], json.dumps(art, sort_keys=True))

    def test_window_filter(self):
        events = self._events(FIXTURE, 30)
        self.assertEqual(
            [e["source_native_id"] for e in events],
            ["https://news.example.com/nuclear-restart",
             "https://news.example.com/utility-outage"])
        self.assertEqual(len(self._events(FIXTURE, 3650)), 3)

    def test_missing_seendate_kept(self):
        fixture = {"articles": [{"url": "https://news.example.com/x"}]}
        events = self._events(fixture, 30)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_date"], "")

    def test_malformed_seendate_kept(self):
        fixture = {"articles": [
            {"url": "https://news.example.com/y", "seendate": "not-a-date"}]}
        events = self._events(fixture, 30)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_date"], "")

    def test_empty_object_response(self):
        self.assertEqual(self._events({}, 3650), [])

    def test_empty_articles_response(self):
        self.assertEqual(self._events({"articles": []}, 3650), [])

    def test_non_ascii_payload_preserved(self):
        art = _article("noticia", 1)
        art["title"] = "Compañía eléctrica — 電力網 update"
        events = self._events({"articles": [art]}, 30)
        self.assertEqual(json.loads(events[0]["payload"])["title"], art["title"])

    def test_limit(self):
        events = self._events(FIXTURE, 3650, limit=1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source_native_id"],
                         "https://news.example.com/grid-modernization")

    def test_limit_zero_skips_request(self):
        with mock.patch.object(gdelt, "_get_json") as get_json:
            events = list(gdelt.fetch_events(self.conn, 3650, 0))
        self.assertEqual(events, [])
        get_json.assert_not_called()


class TestSeendateParsing(GdeltTestCase):
    def test_compact_z_form(self):
        self.assertEqual(gdelt._seendate_to_iso("20260812T120000Z"),
                         "2026-08-12")

    def test_bare_form(self):
        self.assertEqual(gdelt._seendate_to_iso("20260812120000"),
                         "2026-08-12")

    def test_unparseable(self):
        self.assertEqual(gdelt._seendate_to_iso("garbage"), "")
        self.assertEqual(gdelt._seendate_to_iso(""), "")
        self.assertEqual(gdelt._seendate_to_iso(None), "")


class TestGetJson(unittest.TestCase):
    """Exercise the real _get_json retry/re-raise loop (every other test
    patches it out, so this is the only guard on urlopen + backoff)."""

    def _resp(self, obj):
        # _get_json uses `with urlopen(...) as resp: json.load(resp)`;
        # a BytesIO supports the context-manager protocol and reads as a file.
        return io.BytesIO(json.dumps(obj).encode("utf-8"))

    def test_transient_then_success(self):
        payload = {"articles": [{"url": "https://news.example.com/a"}]}
        with mock.patch.object(gdelt.urllib.request, "urlopen",
                               side_effect=[urllib.error.URLError("boom"),
                                            self._resp(payload)]) as urlopen, \
                mock.patch.object(gdelt.time, "sleep") as sleep:
            result = gdelt._get_json("https://api.example/x")
        self.assertEqual(result, payload)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called()   # backoff exercised on the transient failure

    def test_exhausted_retries_reraise(self):
        with mock.patch.object(gdelt.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("down")) \
                as urlopen, \
                mock.patch.object(gdelt.time, "sleep"):
            with self.assertRaises(urllib.error.URLError):
                gdelt._get_json("https://api.example/x", retries=2)
        # retries+1 attempts, then re-raise (never a silent None return)
        self.assertEqual(urlopen.call_count, 3)


class TestRunSource(GdeltTestCase):
    def test_run_and_delta_rerun(self):
        with mock.patch.object(gdelt, "_get_json", return_value=FIXTURE):
            summary = runner.run_source(
                self.conn, "gdelt", gdelt.fetch_events,
                gdelt.PARSER_VERSION, window_days=3650)
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["records_seen"], 3)
        self.assertEqual(summary["records_new"], 3)
        row = self.conn.execute(
            "SELECT * FROM raw_events WHERE raw_event_id="
            "'gdelt:https://news.example.com/utility-outage'").fetchone()
        self.assertEqual(row["source_native_id"],
                         "https://news.example.com/utility-outage")

        # a later fetch adds one article: only it is new (delta per R10.4).
        # seendate on the unchanged three drifts by real time but the sorted
        # payload for those is byte-identical run-to-run, so they dedupe.
        extended = {"articles": FIXTURE["articles"]
                    + [_article("new-substation", 1)]}
        with mock.patch.object(gdelt, "_get_json", return_value=extended):
            summary = runner.run_source(
                self.conn, "gdelt", gdelt.fetch_events,
                gdelt.PARSER_VERSION, force=True, window_days=3650)
        self.assertEqual(summary["records_seen"], 4)
        self.assertEqual(summary["records_new"], 1)

    def test_http_failure_recorded_as_error(self):
        with mock.patch.object(
                gdelt, "_get_json",
                side_effect=OSError("connection refused")):
            summary = runner.run_source(
                self.conn, "gdelt", gdelt.fetch_events,
                gdelt.PARSER_VERSION, window_days=3650)
        self.assertEqual(summary["status"], "error")
        self.assertIn("connection refused", summary["error_state"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0],
            0)


if __name__ == "__main__":
    unittest.main()
