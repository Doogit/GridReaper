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
import sys
import uuid
from datetime import datetime, timedelta, timezone

from app.db.connection import get_connection

LOCK_PATH = "data/.ingest.lock"   # default; GRIDSIGNALS_LOCK overrides
LOCK_STALE_S = 2 * 60 * 60        # == STALE_MINUTES in deploy/scheduled_run.sh
COMMIT_EVERY = 200          # short transactions per R3.2


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


# -- single-writer ingestion lock (R3.2) -------------------------------------

@contextlib.contextmanager
def ingest_lock(path=None):
    """Exclusive-create lockfile holding {pid, ts}. A lock older than
    LOCK_STALE_S is presumed abandoned (crashed run) and is broken; a live
    one raises a clear error naming the holder.

    The path is resolved at call time: an explicit argument, else the
    GRIDSIGNALS_LOCK override deploy/scheduled_run.sh documents, else
    LOCK_PATH."""
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
    try:
        os.write(fd, payload.encode("utf-8"))
        os.close(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)


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
