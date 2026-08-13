"""Tests for the ransomware.live recent-victims fetcher (R5.4, R10.4).

Hermetic: in-memory SQLite via apply_migrations, FK on, HTTP mocked with a
canned victims-array fixture. What must not regress: event shape, discovered
window filtering, limit, and content-hash dedupe on re-runs (the source has no
native id, so stable dedupe proves the payload is deterministic).
"""
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.db.migrate import apply_migrations
from app.ingest import ransomware, runner


def _day(days_ago):
    return (datetime.now(timezone.utc)
            - timedelta(days=days_ago)).date().isoformat()


def _victim(name, days_ago, group="AcmeLocker"):
    dt = (datetime.now(timezone.utc)
          - timedelta(days=days_ago)).isoformat()
    return {
        "victim": name,
        "group": group,
        "discovered": dt,
        "attackdate": dt,
        "description": "leaked data",
        "country": "US",
        "activity": "Energy",
        "domain": f"{name.lower()}.example",
        "url": f"https://www.ransomware.live/id/{name}",
        "screenshot": "",
        "claim_url": "",
        "data_size": None,
        "infostealer": "",
        "press": None,
        "ransom": None,
    }


# non-ASCII org name is stored UTF-8 as-is (no encoding "fix")
FIXTURE = [
    _victim("OldCorp", 1500),
    _victim("MidCo", 300),
    _victim("Sociedad Eléctrica", 10),
]


class RansomwareTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO source_policies (source_id, name, ttl, enabled) "
            "VALUES ('ransomware_live', 'ransomware.live', 86400, 1)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _events(self, fixture, window_days, limit=None):
        with mock.patch.object(ransomware, "_get_json", return_value=fixture):
            return list(
                ransomware.fetch_events(self.conn, window_days, limit))


class TestFetchEvents(RansomwareTestCase):
    def test_event_shape(self):
        events = self._events(FIXTURE, 3650)
        self.assertEqual(len(events), 3)
        e = events[2]
        # no stable native id -> source_native_id omitted
        self.assertNotIn("source_native_id", e)
        self.assertEqual(e["event_date"], _day(10))
        self.assertEqual(e["url"],
                         "https://www.ransomware.live/id/Sociedad Eléctrica")
        # payload round-trips to the source's own record (R3.7): the non-ASCII
        # org name is preserved unchanged (json.dumps escapes it, decoding it
        # back yields the original bytes — no encoding "fix")
        self.assertEqual(json.loads(e["payload"]), FIXTURE[2])
        self.assertEqual(json.loads(e["payload"])["victim"],
                         "Sociedad Eléctrica")
        # payload is deterministic (sort_keys) for content-hash dedupe
        self.assertEqual(e["payload"], json.dumps(FIXTURE[2], sort_keys=True))

    def test_window_filter(self):
        events = self._events(FIXTURE, 30)
        self.assertEqual([e["url"] for e in events],
                         ["https://www.ransomware.live/id/Sociedad Eléctrica"])
        events = self._events(FIXTURE, 400)
        self.assertEqual(len(events), 2)
        self.assertEqual(len(self._events(FIXTURE, 3650)), 3)

    def test_missing_date_kept(self):
        record = {"victim": "NoDate", "group": "X"}
        events = self._events([record], 30)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_date"], "")

    def test_attackdate_fallback_when_no_discovered(self):
        record = _victim("FallbackCo", 5)
        del record["discovered"]
        events = self._events([record], 30)
        self.assertEqual(events[0]["event_date"], _day(5))

    def test_limit(self):
        events = self._events(FIXTURE, 3650, limit=1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["url"],
                         "https://www.ransomware.live/id/OldCorp")

    def test_limit_zero_skips_request(self):
        with mock.patch.object(ransomware, "_get_json") as get_json:
            events = list(ransomware.fetch_events(self.conn, 3650, 0))
        self.assertEqual(events, [])
        get_json.assert_not_called()


class TestRunSource(RansomwareTestCase):
    def test_run_and_content_hash_dedupe_rerun(self):
        with mock.patch.object(
                ransomware, "_get_json", return_value=FIXTURE):
            summary = runner.run_source(
                self.conn, "ransomware_live", ransomware.fetch_events,
                ransomware.PARSER_VERSION, window_days=3650)
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["records_seen"], 3)
        self.assertEqual(summary["records_new"], 3)
        # keyed by content hash (no native id): raw_event_id uses the h: prefix
        row = self.conn.execute(
            "SELECT raw_event_id, source_native_id FROM raw_events "
            "LIMIT 1").fetchone()
        self.assertTrue(row["raw_event_id"].startswith("ransomware_live:h:"))
        self.assertEqual(row["source_native_id"], "")

        # second run over the SAME victims delivered with a DIFFERENT key
        # insertion order (wire-order drift): sort_keys=True must canonicalize
        # the payload so each record hashes to the same content-hash key and
        # nothing is new. This fails if sort_keys=True is dropped (R10.4).
        reordered = [dict(reversed(list(v.items()))) for v in FIXTURE]
        # sanity: the drift is real -- naive dumps would differ pre-sort
        self.assertNotEqual(json.dumps(reordered[0]),
                            json.dumps(FIXTURE[0]))
        with mock.patch.object(
                ransomware, "_get_json", return_value=reordered):
            summary = runner.run_source(
                self.conn, "ransomware_live", ransomware.fetch_events,
                ransomware.PARSER_VERSION, force=True, window_days=3650)
        self.assertEqual(summary["records_seen"], 3)
        self.assertEqual(summary["records_new"], 0)

    def test_http_failure_recorded_as_error(self):
        with mock.patch.object(
                ransomware, "_get_json",
                side_effect=OSError("connection refused")):
            summary = runner.run_source(
                self.conn, "ransomware_live", ransomware.fetch_events,
                ransomware.PARSER_VERSION, window_days=3650)
        self.assertEqual(summary["status"], "error")
        self.assertIn("connection refused", summary["error_state"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM raw_events").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
