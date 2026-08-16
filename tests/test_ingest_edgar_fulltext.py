"""Tests for the SEC EDGAR full-text search fetcher (R5.2, R3.4, R10.4).

Hermetic: in-memory SQLite via apply_migrations, FK on; the HTTP layer is
mocked with canned Elasticsearch-shaped responses. What must not regress:
the request URL (quoted-phrase query, form + window bounds, `from` paging),
the event-dict shape (per-entity dedupe key, archive URL, verbatim _source
payload), the limit contract, inactive-entity exclusion, `from`-advances-by
-hits-returned pagination, and idempotent re-runs through the runner.
"""
import json
import sqlite3
import unittest
import urllib.parse
from datetime import datetime, timezone
from unittest import mock

from app.db.migrate import apply_migrations
from app.ingest import edgar, edgar_fulltext, runner

TODAY = datetime.now(timezone.utc).date().isoformat()


def hit(accession="0001326160-26-000012", filename="d8k.htm",
        cik="0001326160", file_date=TODAY, **extra):
    source = {"adsh": accession, "ciks": [cik], "file_date": file_date,
              "form": "8-K", "root_forms": ["8-K"],
              "display_names": ["Duke Energy CORP  (DUK)  (CIK 0001326160)"]}
    source.update(extra)
    return {"_id": f"{accession}:{filename}", "_source": source}


def response(hits, total=None):
    return {"hits": {"total": {"value": total if total is not None
                               else len(hits), "relation": "eq"},
                     "hits": list(hits)}}


class FulltextTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO source_policies (source_id, name, ttl, enabled) "
            "VALUES ('sec_edgar_fulltext', 'EDGAR full-text', 604800, 1)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def add_entity(self, entity_id, name, active=1):
        self.conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, active) "
            "VALUES (?, ?, ?)", (entity_id, name, active))
        self.conn.commit()

    def fetch_all(self, pages, window_days=365, limit=None):
        """Run fetch_events with _get_page serving `pages`: one response for
        every URL, or a list consumed in request order. Returns (events,
        requested URLs)."""
        calls = []
        queue = list(pages) if isinstance(pages, list) else None

        def fake_get_page(url):
            calls.append(url)
            if queue is not None:
                return queue.pop(0)
            return pages

        with mock.patch.object(edgar_fulltext, "_get_page",
                               side_effect=fake_get_page):
            events = list(edgar_fulltext.fetch_events(
                self.conn, window_days, limit))
        return events, calls


class TestRequestUrl(FulltextTestCase):
    def test_query_is_quoted_phrase_with_forms_and_window(self):
        self.add_entity("E0002", "Duke Energy")
        _, calls = self.fetch_all(response([]))
        self.assertEqual(len(calls), 1)
        parsed = urllib.parse.urlparse(calls[0])
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                         "https://efts.sec.gov/LATEST/search-index")
        q = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(q["q"], ['"Duke Energy"'])
        self.assertEqual(q["forms"], ["8-K,10-K"])
        self.assertEqual(q["dateRange"], ["custom"])
        self.assertEqual(q["enddt"], [TODAY])
        self.assertNotIn("from", q)          # first page omits the offset
        # 365-day window start is 365 days before today
        self.assertLess(q["startdt"][0], q["enddt"][0])

    def test_shares_the_edgar_user_agent_and_throttle(self):
        # R5.2: one 10 req/s budget across every EDGAR host, so the module
        # must not declare a second UA or sleep constant of its own.
        self.assertIs(edgar_fulltext.USER_AGENT, edgar.USER_AGENT)
        self.assertEqual(edgar_fulltext.SLEEP_S, edgar.SLEEP_S)

    def test_get_page_sleeps_before_each_edgar_request(self):
        with mock.patch.object(edgar_fulltext.time, "sleep") as slept, \
                mock.patch.object(edgar, "_get_json",
                                  return_value={"ok": 1}) as get:
            self.assertEqual(edgar_fulltext._get_page("http://x"), {"ok": 1})
        slept.assert_called_once_with(edgar_fulltext.SLEEP_S)
        get.assert_called_once_with("http://x")

    def test_inactive_and_unnamed_entities_are_not_searched(self):
        self.add_entity("E0002", "Duke Energy")
        self.add_entity("E0003", "Retired Co", active=0)
        self.add_entity("E0004", "  ")
        _, calls = self.fetch_all(response([]))
        self.assertEqual(len(calls), 1)
        self.assertIn("Duke+Energy", calls[0])


class TestEventShape(FulltextTestCase):
    def test_event_fields_and_archive_url(self):
        self.add_entity("E0002", "Duke Energy")
        events, _ = self.fetch_all(response([hit()]))
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(
            e["source_native_id"],
            "E0002:0001326160-26-000012:d8k.htm")
        self.assertEqual(e["event_date"], TODAY)
        self.assertEqual(
            e["url"],
            "https://www.sec.gov/Archives/edgar/data/1326160/"
            "000132616026000012/d8k.htm")

    def test_payload_keeps_every_source_field_verbatim(self):
        self.add_entity("E0002", "Duke Energy")
        events, _ = self.fetch_all(response([hit(
            items=["1.05", "9.01"], sics=["4911"], period_ending="2026-06-30",
            unexpected_new_sec_field="keep me")]))
        payload = json.loads(events[0]["payload"])
        self.assertEqual(payload["items"], ["1.05", "9.01"])
        self.assertEqual(payload["sics"], ["4911"])
        self.assertEqual(payload["period_ending"], "2026-06-30")
        self.assertEqual(payload["unexpected_new_sec_field"], "keep me")
        self.assertEqual(payload["entity_id"], "E0002")
        self.assertEqual(payload["matched_name"], "Duke Energy")
        self.assertEqual(payload["_id"],
                         "0001326160-26-000012:d8k.htm")

    def test_missing_source_fields_degrade_to_empty_not_crash(self):
        # R3.7 guard: only _id and file_date are load-bearing; a renamed or
        # absent SEC field must not drop the event.
        self.add_entity("E0002", "Duke Energy")
        events, _ = self.fetch_all(response(
            [{"_id": "0001326160-26-000012:d8k.htm", "_source": {}}]))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_date"], "")
        self.assertEqual(events[0]["url"], "")   # no CIK -> no archive URL

    def test_non_numeric_cik_yields_empty_url_without_raising(self):
        self.add_entity("E0002", "Duke Energy")
        events, _ = self.fetch_all(response([hit(cik="not-a-cik")]))
        self.assertEqual(events[0]["url"], "")

    def test_same_document_matching_two_entities_keeps_both(self):
        # R10.4: keying on _id alone would silently drop one attribution.
        self.add_entity("E0002", "Duke Energy")
        self.add_entity("E0009", "Edison International")
        events, _ = self.fetch_all(response([hit()]))
        self.assertEqual(
            [e["source_native_id"] for e in events],
            ["E0002:0001326160-26-000012:d8k.htm",
             "E0009:0001326160-26-000012:d8k.htm"])


class TestPagination(FulltextTestCase):
    def test_from_advances_by_hits_returned_until_total_reached(self):
        self.add_entity("E0002", "Duke Energy")
        page1 = response([hit(accession=f"acc-{i}") for i in range(3)],
                         total=5)
        page2 = response([hit(accession=f"acc-{i}") for i in range(3, 5)],
                         total=5)
        events, calls = self.fetch_all([page1, page2])
        self.assertEqual(len(events), 5)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("from=", calls[0])
        self.assertIn("from=3", calls[1])

    def test_empty_page_stops_paging(self):
        self.add_entity("E0002", "Duke Energy")
        # total overstates what the endpoint actually returns; an empty page
        # must end the loop rather than spin to MAX_PAGES.
        pages = [response([hit()], total=999), response([], total=999)]
        events, calls = self.fetch_all(pages)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(calls), 2)

    def test_max_pages_caps_a_runaway_entity(self):
        self.add_entity("E0002", "Duke Energy")
        page = response([hit(accession="acc-x")], total=10 ** 6)
        events, calls = self.fetch_all(page)
        self.assertEqual(len(calls), edgar_fulltext.MAX_PAGES)
        self.assertEqual(len(events), edgar_fulltext.MAX_PAGES)


class TestLimit(FulltextTestCase):
    def test_limit_stops_mid_page_and_skips_later_entities(self):
        self.add_entity("E0002", "Duke Energy")
        self.add_entity("E0009", "Edison International")
        page = response([hit(accession="acc-1"), hit(accession="acc-2")])
        events, calls = self.fetch_all(page, limit=1)
        self.assertEqual([e["source_native_id"] for e in events],
                         ["E0002:acc-1:d8k.htm"])
        self.assertEqual(len(calls), 1)

    def test_limit_zero_makes_no_requests(self):
        self.add_entity("E0002", "Duke Energy")
        events, calls = self.fetch_all(response([hit()]), limit=0)
        self.assertEqual(events, [])
        self.assertEqual(calls, [])


class TestRunSourceIntegration(FulltextTestCase):
    def test_run_and_rerun_dedupe(self):
        self.add_entity("E0002", "Duke Energy")
        page = response([hit(accession="acc-1"), hit(accession="acc-2")])
        with mock.patch.object(edgar_fulltext, "_get_page",
                               return_value=page):
            summary = runner.run_source(
                self.conn, edgar_fulltext.SOURCE_ID,
                edgar_fulltext.fetch_events, edgar_fulltext.PARSER_VERSION)
            self.assertEqual(summary["status"], "success")
            self.assertEqual(summary["records_seen"], 2)
            self.assertEqual(summary["records_new"], 2)
            again = runner.run_source(
                self.conn, edgar_fulltext.SOURCE_ID,
                edgar_fulltext.fetch_events, edgar_fulltext.PARSER_VERSION,
                force=True)
        self.assertEqual(again["status"], "success")
        self.assertEqual(again["records_seen"], 2)
        self.assertEqual(again["records_new"], 0)
        row = self.conn.execute(
            "SELECT * FROM raw_events WHERE raw_event_id = "
            "'sec_edgar_fulltext:E0002:acc-1:d8k.htm'").fetchone()
        self.assertEqual(row["source_id"], "sec_edgar_fulltext")
        self.assertEqual(row["source_native_id"], "E0002:acc-1:d8k.htm")

    def test_http_failure_recorded_as_error_run(self):
        # R10.3: the failure is contained in this source's run row; it never
        # raises past the runner.
        self.add_entity("E0002", "Duke Energy")
        with mock.patch.object(edgar_fulltext, "_get_page",
                               side_effect=OSError("efts down")):
            summary = runner.run_source(
                self.conn, edgar_fulltext.SOURCE_ID,
                edgar_fulltext.fetch_events, edgar_fulltext.PARSER_VERSION)
        self.assertEqual(summary["status"], "error")
        self.assertIn("efts down", summary["error_state"])


if __name__ == "__main__":
    unittest.main()
