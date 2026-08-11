"""Tests for the ingestion runner framework (R3.2, R10.3, R10.4).

Hermetic: in-memory SQLite via apply_migrations, FK enforcement on, no
network. What must not regress: run bookkeeping, idempotent dedupe,
per-source error containment, policy skip paths, and the single-writer lock.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from app.db.migrate import apply_migrations
from app.ingest import runner


def _event(native_id, n=0):
    return {
        "source_native_id": native_id,
        "event_date": "2026-08-01",
        "payload": json.dumps({"n": n, "id": native_id}),
        "url": f"https://example.com/{native_id}",
    }


def fake_fetcher(events):
    def fetch_events(conn, window_days, limit):
        for e in events if limit is None else events[:limit]:
            yield e
    return fetch_events


def error_fetcher(conn, window_days, limit):
    yield _event("ok-1")
    raise RuntimeError("boom mid-fetch")


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO source_policies (source_id, name, ttl, enabled) "
            "VALUES ('test_source', 'Test source', 3600, 1)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()


class TestRunSource(RunnerTestCase):
    def test_success_bookkeeping(self):
        events = [_event(f"a-{i}", i) for i in range(3)]
        summary = runner.run_source(
            self.conn, "test_source", fake_fetcher(events), "test/1.0")
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["records_seen"], 3)
        self.assertEqual(summary["records_new"], 3)
        run = self.conn.execute("SELECT * FROM source_runs").fetchone()
        self.assertEqual(run["run_id"], summary["run_id"])
        self.assertEqual(run["source_id"], "test_source")
        self.assertEqual(run["status"], "success")
        self.assertEqual(run["records_new"], 3)
        self.assertEqual(run["parser_version"], "test/1.0")
        self.assertTrue(run["started_at"] and run["finished_at"])
        row = self.conn.execute(
            "SELECT * FROM raw_events WHERE raw_event_id='test_source:a-0'"
        ).fetchone()
        self.assertEqual(row["source_native_id"], "a-0")
        self.assertEqual(row["content_hash"],
                         __import__("hashlib").sha256(
                             row["payload"].encode()).hexdigest())
        self.assertEqual(row["first_seen_at"], row["last_seen_at"])

    def test_second_run_dedupes_to_zero_new(self):
        events = [_event(f"a-{i}") for i in range(3)]
        runner.run_source(self.conn, "test_source", fake_fetcher(events),
                          "test/1.0")
        first = self.conn.execute(
            "SELECT first_seen_at, run_id FROM raw_events "
            "WHERE raw_event_id='test_source:a-0'").fetchone()
        summary = runner.run_source(
            self.conn, "test_source", fake_fetcher(events), "test/1.0",
            force=True)
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["records_seen"], 3)
        self.assertEqual(summary["records_new"], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0], 3)
        after = self.conn.execute(
            "SELECT first_seen_at, last_seen_at, run_id FROM raw_events "
            "WHERE raw_event_id='test_source:a-0'").fetchone()
        # first_seen_at and originating run preserved; last_seen_at touched
        self.assertEqual(after["first_seen_at"], first["first_seen_at"])
        self.assertEqual(after["run_id"], first["run_id"])
        self.assertGreaterEqual(after["last_seen_at"], after["first_seen_at"])

    def test_error_recorded_not_raised(self):
        summary = runner.run_source(
            self.conn, "test_source", error_fetcher, "test/1.0")
        self.assertEqual(summary["status"], "error")
        self.assertIn("boom mid-fetch", summary["error_state"])
        self.assertEqual(summary["records_seen"], 1)
        run = self.conn.execute("SELECT * FROM source_runs").fetchone()
        self.assertEqual(run["status"], "error")
        self.assertIn("RuntimeError", run["error_state"])
        # the event yielded before the failure is kept (R10.3)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0], 1)

    def test_disabled_source_skipped(self):
        self.conn.execute(
            "UPDATE source_policies SET enabled=0 WHERE source_id='test_source'")
        summary = runner.run_source(
            self.conn, "test_source", fake_fetcher([_event("x")]), "test/1.0")
        self.assertEqual(summary["status"], "skipped_disabled")
        self.assertEqual(summary["records_seen"], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0], 0)
        run = self.conn.execute("SELECT status FROM source_runs").fetchone()
        self.assertEqual(run["status"], "skipped_disabled")

    def test_ttl_skip_and_force(self):
        events = [_event("a")]
        runner.run_source(self.conn, "test_source", fake_fetcher(events),
                          "test/1.0")
        summary = runner.run_source(
            self.conn, "test_source", fake_fetcher(events), "test/1.0")
        self.assertEqual(summary["status"], "skipped_ttl")
        summary = runner.run_source(
            self.conn, "test_source", fake_fetcher(events), "test/1.0",
            force=True)
        self.assertEqual(summary["status"], "success")

    def test_ttl_expired_runs_again(self):
        runner.run_source(self.conn, "test_source",
                          fake_fetcher([_event("a")]), "test/1.0")
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        self.conn.execute("UPDATE source_runs SET finished_at=?", (old,))
        summary = runner.run_source(
            self.conn, "test_source", fake_fetcher([_event("a")]), "test/1.0")
        self.assertEqual(summary["status"], "success")

    def test_unknown_source_raises(self):
        with self.assertRaises(ValueError):
            runner.run_source(self.conn, "nope", fake_fetcher([]), "test/1.0")

    def test_content_hash_id_when_no_native_id(self):
        event = {"event_date": "2026-08-01", "payload": '{"page": "text"}'}
        summary = runner.run_source(
            self.conn, "test_source", fake_fetcher([event]), "test/1.0")
        self.assertEqual(summary["records_new"], 1)
        row = self.conn.execute("SELECT * FROM raw_events").fetchone()
        self.assertTrue(row["raw_event_id"].startswith("test_source:h:"))
        self.assertEqual(len(row["raw_event_id"]), len("test_source:h:") + 24)
        # same payload again -> seen; changed payload -> new (R10.4)
        summary = runner.run_source(
            self.conn, "test_source",
            fake_fetcher([event, {"event_date": "2026-08-02",
                                  "payload": '{"page": "changed"}'}]),
            "test/1.0", force=True)
        self.assertEqual(summary["records_seen"], 2)
        self.assertEqual(summary["records_new"], 1)

    def test_limit_passed_through(self):
        events = [_event(f"a-{i}") for i in range(5)]
        summary = runner.run_source(
            self.conn, "test_source", fake_fetcher(events), "test/1.0",
            limit=2)
        self.assertEqual(summary["records_seen"], 2)

    def test_limit_zero_stores_no_events(self):
        events = [_event(f"a-{i}") for i in range(5)]
        summary = runner.run_source(
            self.conn, "test_source", fake_fetcher(events), "test/1.0",
            limit=0)
        self.assertEqual(summary["records_seen"], 0)
        self.assertEqual(summary["records_new"], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0],
            0)


class TestIngestLock(unittest.TestCase):
    def test_contention_raises(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, ".ingest.lock")
            with runner.ingest_lock(path):
                with self.assertRaises(RuntimeError):
                    with runner.ingest_lock(path):
                        pass
            self.assertFalse(os.path.exists(path))

    def test_stale_lock_broken(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, ".ingest.lock")
            old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"pid": 99999, "ts": old}, fh)
            with runner.ingest_lock(path):
                with open(path, encoding="utf-8") as fh:
                    holder = json.load(fh)
                self.assertEqual(holder["pid"], os.getpid())
            self.assertFalse(os.path.exists(path))

    def test_unreadable_lock_not_broken(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, ".ingest.lock")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("not json")
            with self.assertRaises(RuntimeError):
                with runner.ingest_lock(path):
                    pass
            self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
