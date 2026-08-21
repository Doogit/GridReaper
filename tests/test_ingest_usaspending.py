"""Tests for the USAspending.gov fetcher (R5, R10.4).

Hermetic: the HTTP layer is mocked with canned spending_by_award JSON
responses (shaped after app/spikes/usaspending_probe.py's live-verified
response shape); in-memory SQLite via apply_migrations, FK enforcement on,
no network.
"""
import json
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest import mock

from app.db.migrate import apply_migrations
from app.ingest import runner, usaspending


def _award(n, agency="Department of Energy", subagency="",
          recipient="Duke Energy Indiana LLC", start="2026-01-15",
          description="Grid modernization capital project grant"):
    return {
        "Award ID": f"AWD-{n:04d}",
        "Recipient Name": recipient,
        "Awarding Agency": agency,
        "Awarding Sub Agency": subagency,
        "Start Date": start,
        "Description": description,
    }


def _page(results, has_next=None):
    page = {"results": results}
    if has_next is not None:
        page["page_metadata"] = {"hasNext": has_next}
    return page


class _FixedDateTime(datetime):
    """A fixed 'now' for deterministic time_period assertions -- returns a
    real datetime instance (not a recursive _FixedDateTime), so subsequent
    arithmetic (timedelta subtraction, .date()) behaves normally."""
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 6, 15, tzinfo=timezone.utc)


class WatchlistSearchTermsTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, active) "
            "VALUES ('E1', 'Duke Energy Indiana LLC', 1)")
        self.conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, active) "
            "VALUES ('E2', 'Disabled Co', 0)")
        self.conn.execute(
            "INSERT INTO entity_aliases (entity_id, alias, source) "
            "VALUES ('E1', 'Duke Energy Indiana', 'test')")
        self.conn.execute(
            "INSERT INTO entity_aliases (entity_id, alias, source) "
            "VALUES ('E2', 'Disabled Alias', 'test')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_active_entities_and_aliases_only(self):
        terms = usaspending.watchlist_search_terms(self.conn)
        self.assertIn("Duke Energy Indiana LLC", terms)
        self.assertIn("Duke Energy Indiana", terms)
        self.assertNotIn("Disabled Co", terms)
        self.assertNotIn("Disabled Alias", terms)


class FetchEventsTest(unittest.TestCase):
    def _events(self, responses, window_days=365, limit=None,
               search_terms=("Duke Energy Indiana LLC",)):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(conn)
        for i, name in enumerate(search_terms):
            conn.execute(
                "INSERT INTO watchlist_entities (entity_id, name, active) "
                "VALUES (?, ?, 1)", (f"E{i}", name))
        conn.commit()

        calls = []

        def fake_post(data, retries=2):
            calls.append(json.loads(data.decode("utf-8")))
            return responses[len(calls) - 1]

        with mock.patch.object(usaspending, "_post_json", fake_post), \
                mock.patch.object(usaspending.time, "sleep"):
            events = list(usaspending.fetch_events(conn, window_days, limit))
        conn.close()
        return events, calls

    def test_query_construction_scopes_by_watchlist_and_agency(self):
        responses = [_page([]), _page([])]
        with mock.patch.object(usaspending, "datetime", _FixedDateTime):
            _, calls = self._events(responses, window_days=30)
        self.assertEqual(len(calls), 2)   # one per qualifying agency
        agencies = {c["filters"]["agencies"][0]["name"] for c in calls}
        self.assertEqual(agencies, set(usaspending.QUALIFYING_TOPTIER_AGENCIES))
        for c in calls:
            self.assertEqual(c["filters"]["recipient_search_text"],
                             ["Duke Energy Indiana LLC"])
            self.assertEqual(c["filters"]["award_type_codes"],
                             list(usaspending.ASSISTANCE_AWARD_TYPE_CODES))
            self.assertEqual(c["filters"]["time_period"],
                             [{"start_date": "2026-05-16",
                               "end_date": "2026-06-15"}])

    def test_pagination_walks_multiple_pages(self):
        full_page = [_award(i) for i in range(usaspending.PAGE_LIMIT)]
        short_page = [_award(9999)]
        responses = [_page(full_page), _page(short_page), _page([])]
        events, calls = self._events(responses, window_days=3650)
        self.assertEqual(len(calls), 3)   # 2 pages for DOE, 1 for DHS
        self.assertEqual(len(events), usaspending.PAGE_LIMIT + 1)

    def test_malformed_record_missing_fields_skipped(self):
        missing_recipient = _award(1)
        del missing_recipient["Recipient Name"]
        responses = [_page([missing_recipient]), _page([])]
        events, _ = self._events(responses, window_days=3650)
        self.assertEqual(events, [])

    def test_non_dict_record_skipped(self):
        responses = [_page(["not-a-dict"]), _page([])]
        events, _ = self._events(responses, window_days=3650)
        self.assertEqual(events, [])

    def test_event_dict_shape_preserves_description_verbatim(self):
        long_description = ("Grant to fund substation hardening and control "
                            "room modernization across three sites. " * 5)
        award = _award(1, description=long_description)
        responses = [_page([award]), _page([])]
        events, _ = self._events(responses, window_days=3650)
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["source_native_id"], "AWD-0001")
        self.assertEqual(e["event_date"], "2026-01-15")
        payload = json.loads(e["payload"])
        self.assertEqual(payload["Description"], long_description)

    def test_limit_stops_before_second_agency_query(self):
        responses = [_page([_award(1), _award(2)])]
        events, calls = self._events(responses, window_days=3650, limit=1)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(calls), 1)   # never reaches the second agency

    def test_limit_zero_skips_requests(self):
        events, calls = self._events([], limit=0)
        self.assertEqual(events, [])
        self.assertEqual(calls, [])

    def test_malformed_api_response_raises(self):
        responses = [{"not": "expected shape"}]
        with self.assertRaises(ValueError):
            self._events(responses)

    def test_no_active_watchlist_entities_fetches_nothing(self):
        # An unscoped, DOE/DHS-wide query would contradict this fetcher's
        # own "unscoped search is untrustworthy" rationale -- an empty
        # watchlist must fetch nothing, not fall back to searching broadly.
        events, calls = self._events([], search_terms=())
        self.assertEqual(events, [])
        self.assertEqual(calls, [])

    def test_search_terms_split_into_multiple_batches(self):
        terms = tuple(f"Watchlist Entity {i}" for i in range(15))
        responses = [_page([]) for _ in range(4)]   # 2 batches x 2 agencies
        _, calls = self._events(responses, window_days=3650,
                                search_terms=terms)
        self.assertEqual(len(calls), 4)
        batch_sizes = [len(c["filters"]["recipient_search_text"])
                      for c in calls]
        self.assertEqual(sorted(batch_sizes), [5, 5, 10, 10])
        # Every term is covered by some batch, for each agency.
        for agency in usaspending.QUALIFYING_TOPTIER_AGENCIES:
            agency_terms = set()
            for c in calls:
                if c["filters"]["agencies"][0]["name"] == agency:
                    agency_terms.update(c["filters"]["recipient_search_text"])
            self.assertEqual(agency_terms, set(terms))

    def test_pagination_stops_on_has_next_false_even_on_full_page(self):
        # A full PAGE_LIMIT-sized page with an explicit hasNext=False must
        # stop immediately -- precise detection takes priority over the
        # short-page heuristic.
        full_page = [_award(i) for i in range(usaspending.PAGE_LIMIT)]
        responses = [_page(full_page, has_next=False), _page([])]
        events, calls = self._events(responses, window_days=3650)
        self.assertEqual(len(calls), 2)   # 1 page for DOE, 1 for DHS
        self.assertEqual(len(events), usaspending.PAGE_LIMIT)

    def test_pagination_truncated_when_has_next_true_at_max_pages_raises(self):
        # hasNext still true after MAX_PAGES is a confirmed truncation --
        # an incomplete result read as complete would be fabrication (R4.1).
        full_page = [_award(i) for i in range(usaspending.PAGE_LIMIT)]
        responses = [_page(full_page, has_next=True)] * usaspending.MAX_PAGES
        with self.assertRaises(ValueError):
            self._events(responses, window_days=3650)


class PostJsonRetryTest(unittest.TestCase):
    """A non-JSON 200 body (e.g. a transient WAF/CDN interstitial) must be
    retried like a network error, not treated as a permanent shape error."""

    class _FakeResponse:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def test_malformed_json_body_is_retried_then_succeeds(self):
        good_body = json.dumps({"results": []}).encode("utf-8")
        bodies = [b"<html>service unavailable</html>", good_body]
        calls = {"n": 0}

        def fake_urlopen(req, timeout=60):
            resp = self._FakeResponse(bodies[calls["n"]])
            calls["n"] += 1
            return resp

        with mock.patch.object(usaspending.urllib.request, "urlopen",
                               fake_urlopen), \
                mock.patch.object(usaspending.time, "sleep"):
            result = usaspending._post_json(b"{}")
        self.assertEqual(result, {"results": []})
        self.assertEqual(calls["n"], 2)

    def test_malformed_json_body_raises_after_retries_exhausted(self):
        def fake_urlopen(req, timeout=60):
            return self._FakeResponse(b"still not json")

        with mock.patch.object(usaspending.urllib.request, "urlopen",
                               fake_urlopen), \
                mock.patch.object(usaspending.time, "sleep"):
            with self.assertRaises(ValueError):
                usaspending._post_json(b"{}")


class RunSourceIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, active) "
            "VALUES ('E1', 'Duke Energy Indiana LLC', 1)")
        self.conn.execute(
            "INSERT INTO source_policies (source_id, name, ttl, enabled) "
            "VALUES ('usaspending', 'USAspending', 86400, 1)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _run(self, force=False):
        award = _award(1)
        # Same award surfaces under both qualifying agencies (a recipient can
        # legitimately match more than one query) -- dedupe is the runner's
        # job via source_native_id, not the fetcher's.
        responses = [_page([dict(award)]), _page([dict(award)])]
        calls = {"n": 0}

        def fake_post(data, retries=2):
            resp = responses[calls["n"] % len(responses)]
            calls["n"] += 1
            return resp

        with mock.patch.object(usaspending, "_post_json", fake_post), \
                mock.patch.object(usaspending.time, "sleep"):
            return runner.run_source(
                self.conn, usaspending.SOURCE_ID, usaspending.fetch_events,
                usaspending.PARSER_VERSION, force=force, window_days=3650)

    def test_run_and_dedupe_across_agencies(self):
        summary = self._run()
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["records_seen"], 2)
        self.assertEqual(summary["records_new"], 1)
        row = self.conn.execute(
            "SELECT * FROM raw_events "
            "WHERE raw_event_id='usaspending:AWD-0001'").fetchone()
        self.assertEqual(row["source_native_id"], "AWD-0001")

        summary = self._run(force=True)
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["records_seen"], 2)
        self.assertEqual(summary["records_new"], 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM raw_events").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
