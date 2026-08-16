"""Shared test fixture: point the single-writer ingestion lock at a temp file.

Why this exists (R3.2). `app.ui.data.config_write_conn` takes the ingestion lock
before every Admin/incident write, and raises when the lock is held. It accepts a
`lock_path`, but the routes call it bare (`app/ui_web/routes/admin.py` ->
`app/ui_web/deps.py:config_write`), so a `TestClient` request has no seam to
inject a path through. A leftover `data/.ingest.lock` — the residue of a killed
ingestion run, or of a test that took the lock at the repo path and died — then
turns ~47 ui_web tests red with assertions that blame the feature under test.

The seam that does work is the module attribute: `config_write_conn` imports
`LOCK_PATH` lazily *inside the function body*, so rebinding
`app.ingest.runner.LOCK_PATH` is read on every call. (There is no
`app.ui.data.LOCK_PATH` to patch — the import is function-local.) Note that
`ingest_lock(path=LOCK_PATH)` binds its default at def time, so a test that wants
to hold the lock must pass the redirected path explicitly.
"""
import os
import shutil
import tempfile
from unittest import mock

from app.ingest import runner


def redirect_ingest_lock(test):
    """Redirect the ingestion lock to a private temp path for one test.

    Registers cleanup on `test`; returns the redirected lock path so a test that
    deliberately holds the lock can pass it to `ingest_lock(...)`.
    """
    tmpdir = tempfile.mkdtemp(prefix="gs-lock-")
    test.addCleanup(shutil.rmtree, tmpdir, True)
    lock_path = os.path.join(tmpdir, ".ingest.lock")
    patcher = mock.patch.object(runner, "LOCK_PATH", lock_path)
    patcher.start()
    test.addCleanup(patcher.stop)
    return lock_path
