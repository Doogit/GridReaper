"""Ingestion runner framework (R3.2, R10.3, R10.4).

A fetcher is a generator ``fetch_events(conn, window_days, limit)`` yielding
event dicts with keys:

  source_native_id   the source's own id (accession number, document_number,
                     guid, cveID); empty/absent when the source has none
  event_date         UTC ISO date of the event at the source
  payload            JSON string of the source's own record (R3.7: enough raw
                     material to reprocess later; do not pre-digest)
  url, canonical_url, etag, last_modified   optional; default ''
  content_hash       optional; runner computes sha256(payload) when absent

``run_source`` wraps a fetcher with source_policies checks, per-run
bookkeeping in source_runs, R10.4 idempotent dedupe into raw_events, and
R10.3 error containment (a fetcher exception records status 'error' and
returns; it never raises past the runner). Live runs are serialized by the
application-level single-writer lockfile (R3.2) — fetcher CLIs go through
``cli()`` which acquires it.
"""
import argparse
import contextlib
import hashlib
import json
import os
import signal
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone

from app.db.connection import get_connection

LOCK_PATH = "data/.ingest.lock"   # default; GRIDSIGNALS_LOCK overrides
LOCK_STALE_S = 2 * 60 * 60        # == STALE_MINUTES in deploy/scheduled_run.sh
COMMIT_EVERY = 200          # short transactions per R3.2


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


# -- single-writer ingestion lock (R3.2) -------------------------------------

def _restore_target(previous_handler):
    """The value to hand `signal.signal()` when un-installing our SIGTERM
    handler. `signal.getsignal()` returns None for a disposition Python
    can't represent as SIG_DFL/SIG_IGN/callable (rare, but signal.signal()
    itself rejects None outright) -- SIG_DFL is the correct substitute,
    since that's what "no Python-level handler" already means in practice.
    Shared by _sigterm_cleanup_handler and ingest_lock's own restore so the
    fallback rule lives in exactly one place."""
    return previous_handler if previous_handler is not None else signal.SIG_DFL


def _sigterm_cleanup_handler(path, previous_handler):
    """SIGTERM handler installed for ingest_lock's critical section (U35).

    Python's default SIGTERM disposition kills the interpreter immediately
    without unwinding `finally` blocks (unlike SIGINT, which Python converts
    into a catchable KeyboardInterrupt) -- so the lock's normal
    `finally: os.remove(path)` never gets a turn when a cron tick is killed
    mid-step, even now that the container's shutdown trap relays SIGTERM to
    the Python process. Installing ANY Python-level handler suppresses that
    default fatal action, so this only cleans up here for the SIG_DFL/None
    case (restore SIG_DFL, then self-deliver so the process still actually
    terminates the way it would have without us in the way) -- that's the
    one branch we know results in synchronous termination, i.e. the exact
    case that needs the lock rescued before it goes down. SIG_IGN and a
    callable previous handler are chained WITHOUT touching the lock first:
    a pre-existing handler is invoked (not silently discarded) but the
    critical section may still be running afterward (SIG_IGN never dies;
    a "soft" callable handler may just flag-and-return), and pulling the
    lock out from under still-executing code would defeat the single-writer
    guarantee (R3.2) for no reason -- ingest_lock's own `finally` already
    cleans up once/if the critical section actually unwinds, exactly as it
    did before U35. (Every real caller today either runs as a fresh
    subprocess with previous_handler == SIG_DFL, or is the one
    off-main-thread caller this function is never installed for -- so
    SIG_IGN/callable chaining is dormant in practice, not just theoretical
    protection.)"""
    def _handler(signum, frame):
        restore = _restore_target(previous_handler)
        signal.signal(signal.SIGTERM, restore)
        if restore is signal.SIG_DFL:
            with contextlib.suppress(OSError):
                os.remove(path)
            os.kill(os.getpid(), signum)
        elif restore is signal.SIG_IGN:
            pass
        else:
            restore(signum, frame)
    return _handler


@contextlib.contextmanager
def ingest_lock(path=None):
    """Exclusive-create lockfile holding {pid, ts}. A lock older than
    LOCK_STALE_S is presumed abandoned (crashed run) and is broken; a live
    one raises a clear error naming the holder.

    The path is resolved at call time: an explicit argument, else the
    GRIDSIGNALS_LOCK override deploy/scheduled_run.sh documents, else
    LOCK_PATH.

    A SIGTERM handler is installed around the critical section (U35) so a
    cron tick killed mid-step still cleans up the lock -- but only when this
    call is running on the main thread. `signal.signal()` raises ValueError
    off the main thread, and this context manager has a real off-main-thread
    caller: the UI's Admin write path runs sync route handlers in a
    FastAPI/Starlette worker thread (see app/ui_web/deps.py), which reach
    here via app.ui.data.config_write_conn. That path already relies on the
    existing `finally: os.remove(path)` for cleanup (it has no OS signal to
    receive anyway), so skipping the handler there is a no-op, not a gap.

    A SIGTERM landing before the handler is installed (between the O_EXCL
    create/re-create above and the `signal.signal()` call below) still hits
    whatever disposition preceded this call -- unchanged from before U35,
    and bounded by the existing zero-byte/mtime staleness fallback rather
    than wedging ingestion forever."""
    path = path or os.environ.get("GRIDSIGNALS_LOCK") or LOCK_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = json.dumps({"pid": os.getpid(), "ts": _utcnow()})
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        holder = _read_lock(path)
        age = _lock_age_seconds(path, holder)
        if age is None or age < LOCK_STALE_S:
            raise RuntimeError(
                f"Ingestion lock held: {path} ({holder}). Another ingestion "
                f"run is in progress; wait for it or delete the lockfile if "
                f"you are sure it is dead."
            )
        os.remove(path)   # stale (>2h): break and take it
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)

    on_main_thread = threading.current_thread() is threading.main_thread()
    previous_handler = None
    if on_main_thread:
        previous_handler = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM,
                      _sigterm_cleanup_handler(path, previous_handler))
    try:
        os.write(fd, payload.encode("utf-8"))
        os.close(fd)
        yield
    finally:
        # Remove the lock BEFORE restoring the handler (mirrors the order
        # inside _sigterm_cleanup_handler itself, U35 code review): a SIGTERM
        # landing in this window still uses OUR handler until the file is
        # gone, so it cleans up too, instead of a default-disposition kill
        # racing ahead of an as-yet-unremoved lock.
        with contextlib.suppress(OSError):
            os.remove(path)
        if on_main_thread:
            signal.signal(signal.SIGTERM, _restore_target(previous_handler))


def _read_lock(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _lock_age_seconds(path, holder):
    """Seconds since the lock was taken. The JSON `ts` is authoritative; the
    lockfile's mtime is the fallback when it is missing or unparseable, so a
    zero-byte lock — the residue of a crash between the O_EXCL create and the
    payload write — ages out instead of wedging ingestion forever."""
    ts = None
    if holder and "ts" in holder:
        with contextlib.suppress(ValueError):
            ts = datetime.fromisoformat(holder["ts"])
    if ts is None:
        try:
            ts = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
        except OSError:
            return None
    return (datetime.now(timezone.utc) - ts).total_seconds()


# -- raw_events dedupe (R10.4) -----------------------------------------------

def store_raw_event(conn, source_id, run_id, event):
    """Insert-or-touch one event. Returns 'new' or 'seen'.

    raw_event_id = "{source_id}:{source_native_id}", or
    "{source_id}:h:{content_hash[:24]}" when the source has no native id.
    On 'seen': last_seen_at (+ etag/last_modified) refresh; first_seen_at,
    payload, and the original run_id are preserved.
    """
    payload = event["payload"]
    content_hash = event.get("content_hash") or hashlib.sha256(
        payload.encode("utf-8")).hexdigest()
    native_id = (event.get("source_native_id") or "").strip()
    if native_id:
        raw_event_id = f"{source_id}:{native_id}"
    else:
        raw_event_id = f"{source_id}:h:{content_hash[:24]}"

    now = _utcnow()
    existing = conn.execute(
        "SELECT raw_event_id FROM raw_events WHERE raw_event_id = ?",
        (raw_event_id,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE raw_events SET last_seen_at = ?, etag = ?, "
            "last_modified = ? WHERE raw_event_id = ?",
            (now, event.get("etag", "") or "",
             event.get("last_modified", "") or "", raw_event_id))
        return "seen"

    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, run_id, "
        " source_native_id, fetched_at, event_date, payload, url, "
        " canonical_url, etag, last_modified, content_hash, "
        " first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (raw_event_id, source_id, run_id, native_id, now,
         event.get("event_date", "") or "", payload,
         event.get("url", "") or "", event.get("canonical_url", "") or "",
         event.get("etag", "") or "", event.get("last_modified", "") or "",
         content_hash, now, now))
    return "new"


# -- run bookkeeping ---------------------------------------------------------

def _last_success_at(conn, source_id):
    row = conn.execute(
        "SELECT MAX(finished_at) AS t FROM source_runs "
        "WHERE source_id = ? AND status = 'success'", (source_id,)).fetchone()
    return row["t"] if row and row["t"] else None


def _finish_run(conn, run_id, source_id, status,
                records_seen, records_new, error_state):
    conn.execute(
        "UPDATE source_runs SET finished_at = ?, status = ?, "
        " records_seen = ?, records_new = ?, error_state = ? "
        "WHERE run_id = ?",
        (_utcnow(), status, records_seen, records_new, error_state, run_id))
    conn.commit()
    return {"run_id": run_id, "source_id": source_id, "status": status,
            "records_seen": records_seen, "records_new": records_new,
            "error_state": error_state}


def run_source(conn, source_id, fetcher, parser_version, force=False,
               window_days=365, limit=None):
    """Run one fetcher with policy checks and bookkeeping; returns the
    source_runs summary dict. Never raises for fetcher failures (R10.3);
    an unknown source_id is a configuration error and does raise."""
    policy = conn.execute(
        "SELECT enabled, ttl FROM source_policies WHERE source_id = ?",
        (source_id,)).fetchone()
    if policy is None:
        raise ValueError(
            f"Unknown source_id {source_id!r}: not in source_policies. "
            f"Seed it via seeds/source_policies.csv.")

    run_id = f"{source_id}:{uuid.uuid4().hex[:12]}"
    # the run row is inserted up front (raw_events.run_id FK) and finalized
    # by _finish_run; skip paths finalize immediately
    conn.execute(
        "INSERT INTO source_runs (run_id, source_id, started_at, finished_at, "
        " status, records_seen, records_new, error_state, parser_version) "
        "VALUES (?, ?, ?, '', 'running', 0, 0, '', ?)",
        (run_id, source_id, _utcnow(), parser_version))
    conn.commit()

    if not policy["enabled"]:
        return _finish_run(conn, run_id, source_id, "skipped_disabled",
                           0, 0, "")
    last = _last_success_at(conn, source_id)
    if last and not force:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
        if age < timedelta(seconds=policy["ttl"] or 0):
            return _finish_run(conn, run_id, source_id, "skipped_ttl",
                               0, 0, "")

    seen = new = 0
    error_state = ""
    status = "success"
    try:
        for event in fetcher(conn, window_days, limit):
            seen += 1
            if store_raw_event(conn, source_id, run_id, event) == "new":
                new += 1
            if seen % COMMIT_EVERY == 0:
                conn.commit()
    except Exception as exc:   # R10.3: one source down never blocks the run
        status = "error"
        error_state = f"{type(exc).__name__}: {exc}"
    conn.commit()
    return _finish_run(conn, run_id, source_id, status, seen, new,
                       error_state)


# -- shared fetcher CLI ------------------------------------------------------

def cli(source_id, fetcher, parser_version, description):
    """Shared __main__ for fetcher modules: parse args, take the ingestion
    lock, open the connection, run_source, print the summary. Returns an
    exit code (1 when the run errored)."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--window-days", type=int, default=365,
                        help="event window in days (default 365, R5.5)")
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after N events (debugging)")
    parser.add_argument("--force", action="store_true",
                        help="ignore the source TTL")
    args = parser.parse_args()

    with ingest_lock():
        conn = get_connection()
        try:
            summary = run_source(conn, source_id, fetcher, parser_version,
                                 force=args.force,
                                 window_days=args.window_days,
                                 limit=args.limit)
        finally:
            conn.close()
    line = (f"{source_id}: {summary['status']} "
            f"seen={summary['records_seen']} new={summary['records_new']}")
    if summary["error_state"]:
        line += f" error_state={summary['error_state']}"
    print(line)
    return 1 if summary["status"] == "error" else 0
