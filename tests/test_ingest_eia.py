"""Tests for the EIA operating-generator-capacity fetcher (R5.4, R10.4).

Hermetic: in-memory SQLite via apply_migrations, FK on, HTTP mocked with a
canned v2 envelope fixture, EIA_API_KEY set within the test (never a real
key). What must not regress: event shape (plantid -> native id, period ->
event_date, full-record payload round-trip), offset/length pagination across
pages via response.total, limit, limit<=0 short-circuit (no request, no key
required), missing-key error, and delta behavior on re-runs.
"""
import json
import os
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.db.migrate import apply_migrations
from app.ingest import eia, runner


def _month(days_ago):
    d = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return d.strftime("%Y-%m")


def _record(plantid, days_ago, generatorid="1"):
    return {
        "period": _month(days_ago),
        "plantid": plantid,
        "plantName": f"Plant {plantid}",
        "generatorid": generatorid,
        "stateid": "TX",
        "latitude": "31.0",
        "longitude": "-99.0",
        "nameplate-capacity-mw": "250.0",
        "net-summer-capacity-mw": "240.0",
        "net-winter-capacity-mw": "245.0",
    }


def _envelope(records, total):
    return {"response": {"total": str(total), "data": list(records)},
            "apiVersion": "2.1.8"}


class EiaTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO source_policies (source_id, name, ttl, enabled) "
            "VALUES ('eia', 'EIA API v2', 604800, 1)")
        self.conn.commit()
        # tests never depend on a real key (R10.8)
        self._env = mock.patch.dict(os.environ, {"EIA_API_KEY": "TEST_KEY"})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.conn.close()

    def _events(self, envelope, window_days, limit=None):
        with mock.patch.object(eia, "_get_json", return_value=envelope):
            return list(eia.fetch_events(self.conn, window_days, limit))


class TestFetchEvents(EiaTestCase):
    def test_event_shape(self):
        rec = _record("3470", 10, generatorid="7")
        env = _envelope([rec], total=1)
        events = self._events(env, 3650)
        self.assertEqual(len(events), 1)
        e = events[0]
        # native id is unique per record: plantid:generatorid:period
        self.assertEqual(e["source_native_id"], f"3470:7:{rec['period']}")
        self.assertEqual(e["event_date"], _event_date_of(rec["period"]))
        self.assertEqual(e["url"], eia.BROWSER_URL)
        # payload round-trips the full source record (R3.7)
        self.assertEqual(json.loads(e["payload"]), rec)

    def test_period_annual_and_monthly_to_event_date(self):
        self.assertEqual(eia._event_date("2026"), "2026-01-01")
        self.assertEqual(eia._event_date("2026-03"), "2026-03-01")
        self.assertEqual(eia._event_date(""), "")
        self.assertEqual(eia._event_date(None), "")
        self.assertEqual(eia._event_date("garbage"), "")

    def test_window_filter(self):
        env = _envelope(
            [_record("A", 3000), _record("B", 300), _record("C", 10)],
            total=3)
        plants = [_plant(e) for e in self._events(env, 30)]
        self.assertEqual(plants, ["C"])
        plants = [_plant(e) for e in self._events(env, 400)]
        self.assertEqual(plants, ["B", "C"])
        self.assertEqual(len(self._events(env, 3650)), 3)

    def test_missing_period_kept(self):
        # no generatorid/period -> can't form a unique id; omit it so the
        # runner falls back to its content-hash key (never collide records)
        rec = {"plantid": "999", "plantName": "No Period"}
        env = _envelope([rec], total=1)
        events = self._events(env, 30)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_date"], "")
        self.assertEqual(events[0]["source_native_id"], "")

    def test_native_id_composed_from_record(self):
        self.assertEqual(
            eia._native_id(_record("100", 5, generatorid="2A")),
            f"100:2A:{_month(5)}")
        # any missing component -> '' (content-hash fallback in the runner)
        self.assertEqual(eia._native_id({"plantid": "100", "period": "2026-03"}), "")
        self.assertEqual(
            eia._native_id({"plantid": "100", "generatorid": "1"}), "")
        self.assertEqual(
            eia._native_id({"generatorid": "1", "period": "2026-03"}), "")

    def test_pagination_across_pages(self):
        page1 = _envelope([_record("A", 5), _record("B", 5)], total=3)
        page2 = _envelope([_record("C", 5)], total=3)
        with mock.patch.object(
                eia, "_get_json", side_effect=[page1, page2]) as get_json:
            events = list(eia.fetch_events(self.conn, 3650, None))
        self.assertEqual([_plant(e) for e in events], ["A", "B", "C"])
        # two page requests: offset 0 then offset 2, stopping at total=3
        self.assertEqual(get_json.call_count, 2)
        first_url, second_url = (c.args[0] for c in get_json.call_args_list)
        self.assertIn("offset=0", first_url)
        self.assertIn("offset=2", second_url)

    def test_limit_stops_across_pages(self):
        page1 = _envelope([_record("A", 5), _record("B", 5)], total=4)
        with mock.patch.object(
                eia, "_get_json", side_effect=[page1]) as get_json:
            events = list(eia.fetch_events(self.conn, 3650, 1))
        self.assertEqual(len(events), 1)
        self.assertEqual(_plant(events[0]), "A")
        # limit hit inside page 1 -> only one request
        self.assertEqual(get_json.call_count, 1)

    def test_limit_zero_skips_request_and_key(self):
        # limit<=0 must not require the key and must make no HTTP call
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(eia, "_get_json") as get_json:
            events = list(eia.fetch_events(self.conn, 3650, 0))
        self.assertEqual(events, [])
        get_json.assert_not_called()

    def test_missing_key_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                list(eia.fetch_events(self.conn, 3650, None))

    def test_key_never_in_payload_or_url(self):
        env = _envelope([_record("A", 5)], total=1)
        events = self._events(env, 3650)
        for e in events:
            self.assertNotIn("TEST_KEY", e["payload"])
            self.assertNotIn("TEST_KEY", e["url"])


class TestRunSource(EiaTestCase):
    def test_run_and_delta_rerun(self):
        env = _envelope(
            [_record("A", 5), _record("B", 5), _record("C", 5)], total=3)
        with mock.patch.object(eia, "_get_json", return_value=env):
            summary = runner.run_source(
                self.conn, "eia", eia.fetch_events,
                eia.PARSER_VERSION, window_days=3650)
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["records_seen"], 3)
        self.assertEqual(summary["records_new"], 3)
        native = f"A:1:{_month(5)}"
        row = self.conn.execute(
            "SELECT * FROM raw_events WHERE raw_event_id=?",
            (f"eia:{native}",)).fetchone()
        self.assertEqual(row["source_native_id"], native)

        # unchanged re-fetch (force past TTL): nothing new (delta, R10.4)
        with mock.patch.object(eia, "_get_json", return_value=env):
            summary = runner.run_source(
                self.conn, "eia", eia.fetch_events,
                eia.PARSER_VERSION, force=True, window_days=3650)
        self.assertEqual(summary["records_seen"], 3)
        self.assertEqual(summary["records_new"], 0)

    def test_same_plant_many_records_all_stored(self):
        # regression: one plant has many rows (per generator, per period).
        # plantid-only keys would collapse these into one; the composite key
        # must store all of them (R3.7). Fails against the old plantid key.
        recs = [
            _record("500", 5, generatorid="1"),
            _record("500", 5, generatorid="2"),
            _record("500", 35, generatorid="1"),  # same gen, later period
        ]
        # sanity: distinct native ids
        self.assertEqual(len({eia._native_id(r) for r in recs}), 3)
        env = _envelope(recs, total=3)
        with mock.patch.object(eia, "_get_json", return_value=env):
            summary = runner.run_source(
                self.conn, "eia", eia.fetch_events,
                eia.PARSER_VERSION, window_days=3650)
        self.assertEqual(summary["records_seen"], 3)
        self.assertEqual(summary["records_new"], 3)
        count = self.conn.execute(
            "SELECT COUNT(DISTINCT raw_event_id) FROM raw_events "
            "WHERE source_id='eia'").fetchone()[0]
        self.assertEqual(count, 3)

    def test_pagination_stops_when_total_unparseable(self):
        # documents the intentional branch: total=None -> stop after one full
        # page rather than looping forever (status success).
        page = _envelope([_record("A", 5)], total=3)
        page["response"]["total"] = None
        with mock.patch.object(
                eia, "_get_json", return_value=page) as get_json:
            summary = runner.run_source(
                self.conn, "eia", eia.fetch_events,
                eia.PARSER_VERSION, window_days=3650)
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["records_seen"], 1)
        self.assertEqual(get_json.call_count, 1)

    def test_missing_key_recorded_as_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            summary = runner.run_source(
                self.conn, "eia", eia.fetch_events,
                eia.PARSER_VERSION, window_days=3650)
        self.assertEqual(summary["status"], "error")
        self.assertIn("EIA_API_KEY", summary["error_state"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0],
            0)

    def test_http_failure_recorded_as_error(self):
        with mock.patch.object(
                eia, "_get_json", side_effect=OSError("connection refused")):
            summary = runner.run_source(
                self.conn, "eia", eia.fetch_events,
                eia.PARSER_VERSION, window_days=3650)
        self.assertEqual(summary["status"], "error")
        self.assertIn("connection refused", summary["error_state"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0],
            0)


def _event_date_of(period):
    return eia._event_date(period)


def _plant(event):
    """plantid component of the composite native id (plantid:generatorid:period)."""
    return event["source_native_id"].split(":", 1)[0]


if __name__ == "__main__":
    unittest.main()
