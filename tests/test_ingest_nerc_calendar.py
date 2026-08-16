"""Tests for the NERC regulatory calendar fetcher (R5.5, R3.7, R10.4).

Hermetic: the HTTP layer is mocked with a canned /api/search/events
response; in-memory SQLite via apply_migrations, FK enforcement on, no
network. What must not regress: the Eastern-first event_date rule, the
forward-looking month window, and idempotent re-fetch.
"""
import json
import sqlite3
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from app.db.migrate import apply_migrations
from app.ingest import nerc_calendar, runner


def _tz(start, selected):
    return {"isSelected": selected, "eventStartDate": start,
            "timeZoneAbbreviation": "EST" if selected else "CST"}


def _item(slug, day, dates=None):
    return {
        "title": f"Meeting {slug}",
        "url": f"/events/{slug}",
        "month": "Sep",
        "day": day,
        "locationType": "Online",
        "committeeCategories": ["Security Working Group (SWG)"],
        "dateInfoByTimeZone": dates if dates is not None else [],
    }


CENTRAL_FIRST = _item("swg-meeting", "10", [
    _tz("2026-09-10T09:00:00Z", False),      # Central, listed first
    _tz("2026-09-10T10:00:00Z", True),       # Eastern, isSelected
])
NO_TZ_BLOCK = _item("ballot-webinar", "14", [])
UNSELECTED_ONLY = _item("sdt-meeting", "21", [
    _tz("2026-09-21T13:00:00Z", False),
])

RESPONSE = {"year": 2026, "month": 9, "monthAndYearDisplay": "September 2026",
            "allItems": [CENTRAL_FIRST, NO_TZ_BLOCK, UNSELECTED_ONLY],
            "itemsByDay": []}


def _fallback_date():
    """NO_TZ_BLOCK carries no timezone block, so its date falls back to the
    requested calendar month - which fetch_events derives from today."""
    today = date(2026, 9, 1)
    return f"{today.year:04d}-{today.month:02d}-14"


class EventDateTest(unittest.TestCase):
    def test_prefers_selected_eastern_entry(self):
        self.assertEqual(
            nerc_calendar.event_date(CENTRAL_FIRST, 2026, 9), "2026-09-10")

    def test_falls_back_to_any_timezone_entry(self):
        self.assertEqual(
            nerc_calendar.event_date(UNSELECTED_ONLY, 2026, 9), "2026-09-21")

    def test_falls_back_to_grid_day(self):
        self.assertEqual(
            nerc_calendar.event_date(NO_TZ_BLOCK, 2026, 9), "2026-09-14")

    def test_undatable_item_yields_empty(self):
        self.assertEqual(
            nerc_calendar.event_date({"day": ""}, 2026, 9), "")


class CalendarEventTest(unittest.TestCase):
    def test_native_id_and_absolute_url(self):
        event = nerc_calendar.calendar_event(2026, 9, CENTRAL_FIRST)
        self.assertEqual(event["source_native_id"], "/events/swg-meeting")
        self.assertEqual(event["url"],
                         "https://www.nerc.com/events/swg-meeting")
        self.assertEqual(event["canonical_url"], event["url"])

    def test_payload_keeps_the_source_record(self):
        payload = json.loads(
            nerc_calendar.calendar_event(2026, 9, CENTRAL_FIRST)["payload"])
        self.assertEqual(payload["calendar_year"], 2026)
        self.assertEqual(payload["calendar_month"], 9)
        # R3.7: the whole timezone array survives, not just the date used.
        self.assertEqual(len(payload["event"]["dateInfoByTimeZone"]), 2)


class MonthsAheadTest(unittest.TestCase):
    def test_window_is_forward_looking(self):
        today = datetime.now(timezone.utc).date()
        self.assertEqual(nerc_calendar.months_ahead(0),
                         [(today.year, today.month)])
        last = today + timedelta(days=40)
        self.assertEqual(nerc_calendar.months_ahead(40)[-1],
                         (last.year, last.month))

    def test_runaway_guard(self):
        self.assertEqual(len(nerc_calendar.months_ahead(20000)),
                         nerc_calendar.MAX_MONTHS)


class FetchEventsTest(unittest.TestCase):
    def _events(self, window_days=0, limit=None):
        urls = []

        def fake_get(url, retries=2):
            urls.append(url)
            return RESPONSE

        with mock.patch.object(nerc_calendar, "_get_json", fake_get), \
                mock.patch.object(nerc_calendar, "_today",
                                  lambda: date(2026, 9, 1)), \
                mock.patch.object(nerc_calendar.time, "sleep"):
            events = list(nerc_calendar.fetch_events(None, window_days, limit))
        return events, urls

    def test_one_request_per_month_with_query(self):
        _, urls = self._events(window_days=0)
        today = date(2026, 9, 1)
        self.assertEqual(len(urls), 1)
        self.assertIn(f"year={today.year}", urls[0])
        self.assertIn(f"month={today.month}", urls[0])

    def test_events_are_dated(self):
        events, _ = self._events()
        self.assertEqual([e["event_date"] for e in events],
                         ["2026-09-10", _fallback_date(), "2026-09-21"])

    def test_current_month_past_events_are_skipped(self):
        response = {"allItems": [
            _item("past", "03", [_tz("2026-08-03T10:00:00Z", True)]),
            _item("today", "16", [_tz("2026-08-16T10:00:00Z", True)]),
            _item("future", "20", [_tz("2026-08-20T10:00:00Z", True)]),
        ]}

        with mock.patch.object(nerc_calendar, "_today",
                               lambda: date(2026, 8, 16)), \
                mock.patch.object(nerc_calendar, "_get_json",
                                  lambda url, retries=2: response), \
                mock.patch.object(nerc_calendar.time, "sleep"):
            events = list(nerc_calendar.fetch_events(None, 0, None))

        self.assertEqual([e["source_native_id"] for e in events],
                         ["/events/today", "/events/future"])

    def test_limit(self):
        events, _ = self._events(limit=2)
        self.assertEqual(len(events), 2)

    def test_limit_zero_skips_requests(self):
        events, urls = self._events(limit=0)
        self.assertEqual(events, [])
        self.assertEqual(urls, [])


class RunSourceIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO source_policies (source_id, name, ttl, enabled) "
            "VALUES ('nerc_calendar', 'NERC calendar', 3600, 1)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _run(self):
        with mock.patch.object(nerc_calendar, "_get_json",
                               lambda url, retries=2: RESPONSE), \
                mock.patch.object(nerc_calendar, "_today",
                                  lambda: date(2026, 9, 1)), \
                mock.patch.object(nerc_calendar.time, "sleep"):
            return runner.run_source(
                self.conn, "nerc_calendar", nerc_calendar.fetch_events,
                nerc_calendar.PARSER_VERSION, force=True, window_days=0)

    def test_dated_rows_then_idempotent_rerun(self):
        first = self._run()
        self.assertEqual(first["status"], "success")
        self.assertEqual(first["records_new"], 3)

        rows = self.conn.execute(
            "SELECT raw_event_id, event_date FROM raw_events "
            "ORDER BY raw_event_id").fetchall()
        self.assertEqual(sorted(r["event_date"] for r in rows),
                         sorted(["2026-09-10", _fallback_date(),
                                 "2026-09-21"]))
        self.assertEqual([r["raw_event_id"] for r in rows],
                         ["nerc_calendar:/events/ballot-webinar",
                          "nerc_calendar:/events/sdt-meeting",
                          "nerc_calendar:/events/swg-meeting"])

        second = self._run()
        self.assertEqual(second["records_new"], 0)      # R10.4
        self.assertEqual(second["records_seen"], 3)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM raw_events").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
