"""Single-writer ingestion lock: staleness and path resolution (R3.2).

The lockfile is created with O_EXCL and written as a SECOND step, so a crash in
between leaves a zero-byte lock. These tests pin that such residue ages out
(rather than wedging ingestion forever) and that a lock which is merely
unreadable-but-fresh is still treated as live. They also pin the
GRIDSIGNALS_LOCK override deploy/scheduled_run.sh documents.

Every test drives an explicit temp path or the env var: the real
data/.ingest.lock is never created here (a stray one turns the ui_web suite
red with assertions that blame the feature under test).
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.ingest import runner


def _backdate(path, hours):
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp()
    os.utime(path, (stamp, stamp))


def _write_lock(path, ts):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"pid": 99999, "ts": ts}, fh)


class TestLockStaleness(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="gs-ingest-lock-")
        self.addCleanup(td.cleanup)
        self.path = os.path.join(td.name, ".ingest.lock")

    def test_zero_byte_stale_lock_is_broken(self):
        # The wedge: a crash between O_EXCL create and write leaves an empty
        # file. Its JSON will never parse, so before the mtime fallback the age
        # was None, the "stale" branch was unreachable, and every later run
        # raised forever while deploy/scheduled_run.sh (mtime-based) happily
        # announced the lock abandoned and ran the pipeline over frozen data.
        open(self.path, "w").close()
        _backdate(self.path, hours=3)
        with runner.ingest_lock(self.path):
            self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_zero_byte_fresh_lock_still_raises(self):
        # The fail-safe direction: an unparseable lock inside the stale window
        # may be a live run caught mid-write, so it is never stomped.
        open(self.path, "w").close()
        with self.assertRaises(RuntimeError):
            with runner.ingest_lock(self.path):
                pass
        self.assertTrue(os.path.exists(self.path))

    def test_live_lock_still_raises(self):
        _write_lock(self.path, datetime.now(timezone.utc).isoformat())
        with self.assertRaises(RuntimeError):
            with runner.ingest_lock(self.path):
                pass
        self.assertTrue(os.path.exists(self.path))

    def test_stale_lock_still_broken(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        _write_lock(self.path, old)
        with runner.ingest_lock(self.path):
            with open(self.path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["pid"], os.getpid())
        self.assertFalse(os.path.exists(self.path))

    def test_fresh_ts_in_an_old_file_still_raises(self):
        # The JSON `ts` stays authoritative when it parses: a lock re-taken in a
        # file whose mtime was never updated must not be broken on mtime alone.
        _write_lock(self.path, datetime.now(timezone.utc).isoformat())
        _backdate(self.path, hours=3)
        with self.assertRaises(RuntimeError):
            with runner.ingest_lock(self.path):
                pass


class TestLockPathResolution(unittest.TestCase):
    def test_env_var_overrides_the_default_lock_path(self):
        # deploy/scheduled_run.sh documents GRIDSIGNALS_LOCK as the per-step
        # ingestion lock path. If the runner ignored it, setting it as a hosting
        # app-setting would point the guard at a file no writer ever creates and
        # the guard would never skip — the exact collision it exists to prevent.
        with tempfile.TemporaryDirectory(prefix="gs-ingest-lock-") as td:
            path = os.path.join(td, "elsewhere.lock")
            with mock.patch.dict(os.environ, {"GRIDSIGNALS_LOCK": path}):
                with runner.ingest_lock():
                    self.assertTrue(
                        os.path.exists(path),
                        "GRIDSIGNALS_LOCK is documented as the lock path but "
                        "the runner did not take its lock there")
            self.assertFalse(os.path.exists(path))

    def test_explicit_path_beats_the_env_var(self):
        with tempfile.TemporaryDirectory(prefix="gs-ingest-lock-") as td:
            explicit = os.path.join(td, "explicit.lock")
            with mock.patch.dict(os.environ,
                                 {"GRIDSIGNALS_LOCK": os.path.join(td, "env.lock")}):
                with runner.ingest_lock(explicit):
                    self.assertTrue(os.path.exists(explicit))
                    self.assertFalse(os.path.exists(os.path.join(td, "env.lock")))


if __name__ == "__main__":
    unittest.main()
