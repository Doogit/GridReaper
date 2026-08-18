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
import signal
import tempfile
import threading
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


class TestSigtermHandling(unittest.TestCase):
    """U35: a cron tick killed mid-Python-step still doesn't clean up its lock,
    even after the container's shutdown trap relays SIGTERM straight to the
    process -- Python's default SIGTERM disposition kills the interpreter
    without unwinding `finally` blocks. ingest_lock now installs a narrowly
    scoped handler around its critical section that does the same cleanup and
    then chains to whatever handler SIGTERM had before."""

    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="gs-ingest-sigterm-")
        self.addCleanup(td.cleanup)
        self.path = os.path.join(td.name, ".ingest.lock")

    def test_sigterm_during_critical_section_removes_the_lock(self):
        with runner.ingest_lock(self.path):
            self.assertTrue(os.path.exists(self.path))
            handler = signal.getsignal(signal.SIGTERM)
            with mock.patch.object(signal, "signal"), \
                 mock.patch.object(os, "kill"):
                handler(signal.SIGTERM, None)
            self.assertFalse(os.path.exists(self.path))

    def test_previous_handler_restored_after_normal_exit(self):
        original = signal.getsignal(signal.SIGTERM)
        with runner.ingest_lock(self.path):
            self.assertIsNot(signal.getsignal(signal.SIGTERM), original)
        self.assertEqual(signal.getsignal(signal.SIGTERM), original)

    def test_preexisting_handler_is_chained_not_discarded(self):
        # A pre-existing custom SIGTERM handler (another caller's) must still
        # run rather than being silently dropped -- but since it may not
        # actually terminate the process (a "soft" handler that flags and
        # returns), the lock must NOT be pulled out from under the critical
        # section, which is still executing. The existing `finally` cleans
        # up once/if the `with` block actually unwinds, same as pre-U35.
        previous = mock.Mock()
        original = signal.signal(signal.SIGTERM, previous)
        self.addCleanup(signal.signal, signal.SIGTERM, original)
        with runner.ingest_lock(self.path):
            installed = signal.getsignal(signal.SIGTERM)
            self.assertIsNot(installed, previous)
            installed(signal.SIGTERM, None)
            previous.assert_called_once_with(signal.SIGTERM, None)
            # chained, but NOT preemptively removed -- single-writer safety
            # holds while this critical section keeps running.
            self.assertTrue(os.path.exists(self.path))
            # our handler already restored the chained-to handler
            self.assertIs(signal.getsignal(signal.SIGTERM), previous)
        # normal exit of the `with` block cleans up via the pre-existing
        # finally, exactly as it did before U35.
        self.assertFalse(os.path.exists(self.path))

    def test_default_previous_handler_redelivers_after_cleanup(self):
        # previous_handler=None (fresh-process default) must not be handed to
        # signal.signal() as-is (it raises TypeError) and must still result in
        # the process actually terminating -- restore SIG_DFL, then re-raise
        # the signal so the OS's default action (terminate) really happens.
        open(self.path, "w").close()
        handler = runner._sigterm_cleanup_handler(self.path, None)
        with mock.patch.object(signal, "signal") as mock_signal, \
             mock.patch.object(os, "kill") as mock_kill:
            handler(signal.SIGTERM, None)
        self.assertFalse(os.path.exists(self.path))
        mock_signal.assert_called_once_with(signal.SIGTERM, signal.SIG_DFL)
        mock_kill.assert_called_once_with(os.getpid(), signal.SIGTERM)

    def test_literal_sig_dfl_previous_handler_behaves_like_none(self):
        # signal.getsignal() on a real fresh process returns the SIG_DFL
        # sentinel itself, not None -- None only occurs for a disposition
        # installed outside Python. Same branch, same outcome as the None
        # case above; pinned separately so the two aren't conflated.
        open(self.path, "w").close()
        handler = runner._sigterm_cleanup_handler(self.path, signal.SIG_DFL)
        with mock.patch.object(signal, "signal") as mock_signal, \
             mock.patch.object(os, "kill") as mock_kill:
            handler(signal.SIGTERM, None)
        self.assertFalse(os.path.exists(self.path))
        mock_signal.assert_called_once_with(signal.SIGTERM, signal.SIG_DFL)
        mock_kill.assert_called_once_with(os.getpid(), signal.SIGTERM)

    def test_ignored_previous_handler_stays_ignored(self):
        # SIG_IGN never terminates the process, so nothing here should
        # remove the lock either -- that's still the running critical
        # section's job to finish and unwind normally.
        open(self.path, "w").close()
        handler = runner._sigterm_cleanup_handler(self.path, signal.SIG_IGN)
        with mock.patch.object(signal, "signal") as mock_signal, \
             mock.patch.object(os, "kill") as mock_kill:
            handler(signal.SIGTERM, None)
        self.assertTrue(os.path.exists(self.path))
        mock_signal.assert_called_once_with(signal.SIGTERM, signal.SIG_IGN)
        mock_kill.assert_not_called()

    def test_off_main_thread_skips_the_handler_but_still_locks(self):
        # app/ui_web/deps.py: FastAPI runs sync route handlers (the Admin
        # write path) in a worker thread. signal.signal() raises ValueError
        # off the main thread, so ingest_lock must not attempt it there --
        # the existing finally-based cleanup already covers that path (no OS
        # signal is ever delivered to a worker thread anyway).
        errors = []

        def worker():
            try:
                with runner.ingest_lock(self.path):
                    pass
            except Exception as exc:   # pragma: no cover - failure path
                errors.append(exc)

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertEqual(errors, [])
        self.assertFalse(os.path.exists(self.path))


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
