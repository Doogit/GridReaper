"""Tests for the GDELT DOC API fetcher (R5.x, R6.3, R10.4).

Hermetic: in-memory SQLite via apply_migrations, FK on, HTTP mocked with a
canned GDELT articles fixture. What must not regress: event shape (native
id=url, event_date parsed from seendate, full record round-trips), seendate
window filtering, limit, empty-response handling, delta behavior on re-runs,
and the R6.3 query construction (per-entity terms, collision terms dropped
from the positive set, chunking, cross-chunk article dedupe).
"""
import io
import json
import sqlite3
import unittest
import urllib.error
import urllib.parse
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


# Watchlist slice mirroring the real seed's awkward cases (R6.3):
# E0038's *name* is itself a collision term; E0028's name carries a
# parenthetical; E0009 is inactive.
ENTITIES = [
    ("E0004", "Dominion Energy", 1),
    ("E0007", "Constellation Energy", 1),
    ("E0009", "Retired Utility", 0),
    ("E0028", "TXNM Energy (PNM)", 1),
    ("E0038", "Otter Tail", 1),
]
ALIASES = [
    ("E0028", "TXNM Energy"),
    ("E0038", "Otter Tail Power"),
    ("E0038", "Otter Tail Corporation"),
]
COLLISIONS = [
    ("E0004", "Dominion"),
    ("E0007", "Constellation"),
    ("E0028", "PNM"),
    ("E0038", "Otter Tail"),
]


def _query_of(url):
    """The decoded `query` parameter of a built request URL."""
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.parse_qs(parsed.query)["query"][0]


class GdeltTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO source_policies (source_id, name, ttl, enabled) "
            "VALUES ('gdelt', 'GDELT 2.0 DOC API', 86400, 1)")
        for eid, name, active in ENTITIES:
            self.conn.execute(
                "INSERT INTO watchlist_entities (entity_id, name, active) "
                "VALUES (?, ?, ?)", (eid, name, active))
        for eid, alias in ALIASES:
            self.conn.execute(
                "INSERT INTO entity_aliases (entity_id, alias) VALUES (?, ?)",
                (eid, alias))
        for eid, term in COLLISIONS:
            self.conn.execute(
                "INSERT INTO entity_collision_terms (entity_id, term) "
                "VALUES (?, ?)", (eid, term))
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


class TestEntityTerms(GdeltTestCase):
    """R6.3: the query carries per-entity disambiguation terms."""

    def test_terms_are_name_plus_aliases_of_active_entities(self):
        self.assertEqual(
            gdelt.entity_terms(self.conn),
            ["Dominion Energy", "Constellation Energy", "TXNM Energy",
             "Otter Tail Corporation", "Otter Tail Power"])

    def test_collision_term_never_queried_but_aliases_survive(self):
        terms = gdelt.entity_terms(self.conn)
        # E0038's own name is a registered collision term -> dropped bare...
        self.assertNotIn("Otter Tail", terms)
        # ...while its disambiguated aliases still query.
        self.assertIn("Otter Tail Power", terms)
        # collision terms are negative terms (app/resolve.py); they are never
        # emitted as positive phrases for any entity either
        for collision in ("Dominion", "Constellation", "PNM"):
            self.assertNotIn(collision, terms)

    def test_inactive_entity_contributes_nothing(self):
        self.assertNotIn("Retired Utility", gdelt.entity_terms(self.conn))

    def test_entity_with_only_colliding_terms_is_silent(self):
        self.conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, active) "
            "VALUES ('E9001', 'Range', 1)")
        self.conn.execute(
            "INSERT INTO entity_collision_terms (entity_id, term) "
            "VALUES ('E9001', 'Range')")
        self.assertNotIn("Range", gdelt.entity_terms(self.conn))

    def test_phrase_sanitizes_query_metacharacters(self):
        # '(' ')' '"' delimit GDELT query syntax
        self.assertEqual(gdelt._phrase('TXNM Energy (PNM)'), "TXNM Energy")
        self.assertEqual(gdelt._phrase('Acme "Power" Co'), "Acme Power Co")
        self.assertEqual(gdelt._phrase("(unnamed)"), "")


class TestBuildUrls(GdeltTestCase):
    def test_query_contains_quoted_entity_phrases_and_context(self):
        urls = gdelt.build_urls(self.conn, 30)
        self.assertEqual(len(urls), 1)          # 5 terms < MAX_TERMS_PER_QUERY
        query = _query_of(urls[0])
        self.assertEqual(
            query,
            '("Dominion Energy" OR "Constellation Energy" OR "TXNM Energy" '
            'OR "Otter Tail Corporation" OR "Otter Tail Power") '
            'sourcecountry:US')
        self.assertTrue(urls[0].startswith(gdelt.API_BASE + "?"))
        self.assertIn("maxrecords=250", urls[0])
        self.assertIn("timespan=30d", urls[0])

    def test_timespan_capped_to_rolling_window(self):
        self.assertIn("timespan=90d", gdelt.build_urls(self.conn, 365)[0])
        self.assertIn("timespan=1d", gdelt.build_urls(self.conn, 0)[0])

    def test_chunked_at_max_terms_per_query(self):
        with mock.patch.object(gdelt, "MAX_TERMS_PER_QUERY", 2):
            urls = gdelt.build_urls(self.conn, 30)
        self.assertEqual(len(urls), 3)          # 5 terms -> 2 + 2 + 1
        queries = [_query_of(u) for u in urls]
        self.assertEqual(
            queries[0],
            '("Dominion Energy" OR "Constellation Energy") sourcecountry:US')
        # every term appears exactly once across the chunks
        self.assertEqual(
            sum(q.count(" OR ") + 1 for q in queries), 5)
        # a one-term chunk is not wrapped in an OR block (syntax error)
        self.assertEqual(queries[2], '"Otter Tail Power" sourcecountry:US')

    def test_urls_stay_within_a_safe_http_length(self):
        with mock.patch.object(gdelt, "MAX_TERMS_PER_QUERY", 25):
            long_names = [(f"E9{i:03d}", "Wisconsin Public Service " * 2, 1)
                          for i in range(50)]
            for eid, name, active in long_names:
                self.conn.execute(
                    "INSERT INTO watchlist_entities (entity_id, name, active) "
                    "VALUES (?, ?, ?)", (eid, name.strip(), active))
            urls = gdelt.build_urls(self.conn, 30)
        self.assertTrue(urls)
        self.assertLess(max(len(u) for u in urls), 2000)

    def test_empty_watchlist_issues_no_request(self):
        self.conn.execute("DELETE FROM entity_collision_terms")
        self.conn.execute("DELETE FROM entity_aliases")
        self.conn.execute("DELETE FROM watchlist_entities")
        self.assertEqual(gdelt.build_urls(self.conn, 30), [])
        with mock.patch.object(gdelt, "_get_json") as get_json:
            self.assertEqual(list(gdelt.fetch_events(self.conn, 30, None)), [])
        get_json.assert_not_called()


class TestChunkedFetch(GdeltTestCase):
    def test_one_request_per_chunk_spaced_by_sleep(self):
        with mock.patch.object(gdelt, "MAX_TERMS_PER_QUERY", 2), \
                mock.patch.object(gdelt, "_get_json",
                                  return_value={"articles": []}) as get_json, \
                mock.patch.object(gdelt.time, "sleep") as sleep:
            list(gdelt.fetch_events(self.conn, 30, None))
        self.assertEqual(get_json.call_count, 3)
        # polite spacing between chunks, not before the first (R3.4)
        self.assertEqual(sleep.call_args_list,
                         [mock.call(gdelt.SLEEP_S)] * 2)

    def test_article_matching_two_chunks_yielded_once(self):
        with mock.patch.object(gdelt, "MAX_TERMS_PER_QUERY", 2), \
                mock.patch.object(gdelt, "_get_json", return_value=FIXTURE), \
                mock.patch.object(gdelt.time, "sleep"):
            events = list(gdelt.fetch_events(self.conn, 3650, None))
        self.assertEqual(len(events), 3)        # 3 chunks x same 3 articles
        self.assertEqual(len({e["source_native_id"] for e in events}), 3)

    def test_limit_stops_before_later_chunks(self):
        with mock.patch.object(gdelt, "MAX_TERMS_PER_QUERY", 2), \
                mock.patch.object(gdelt, "_get_json",
                                  return_value=FIXTURE) as get_json, \
                mock.patch.object(gdelt.time, "sleep"):
            events = list(gdelt.fetch_events(self.conn, 3650, 2))
        self.assertEqual(len(events), 2)
        self.assertEqual(get_json.call_count, 1)


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

    def test_malformed_body_then_success(self):
        # R9.6 live re-trial: an overloaded DOC API backend sometimes answers
        # 200 with an empty body instead of the documented 429. That must be
        # retried like a transient URLError, not fail the chunk outright.
        payload = {"articles": [{"url": "https://news.example.com/a"}]}
        with mock.patch.object(gdelt.urllib.request, "urlopen",
                               side_effect=[io.BytesIO(b""),
                                            self._resp(payload)]) as urlopen, \
                mock.patch.object(gdelt.time, "sleep") as sleep:
            result = gdelt._get_json("https://api.example/x")
        self.assertEqual(result, payload)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called()

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
