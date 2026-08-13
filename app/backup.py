"""SQLite backup / export CLI + checkpoint policy (R3.6).

R3.6 requires "a checkpoint/backup policy, and an export path for the SQLite
database." Migrations + the schema version table (the other half of R3.6) are
already built (app/db/migrate.py); this module supplies the export path and the
documented checkpoint policy.

Export mechanism: ``VACUUM INTO <path>``. Chosen over the sqlite3 online
``.backup`` API because VACUUM INTO writes a single, fully-checkpointed,
defragmented standalone database file with NO ``-wal``/``-shm`` sidecars — the
export opens on its own with no dangling WAL, which is exactly what a portable
copy needs. It reads the live DB under SQLite's normal locking, so it is safe to
run while the DB exists, and it never mutates the source (VACUUM INTO writes only
to the destination; the live file and its WAL are untouched beyond the read).

Checkpoint policy (the "documented policy" R3.6 calls for):
  * The live DB runs in WAL mode (R3.2), so committed data can sit in the
    ``-wal`` sidecar rather than the main ``.db`` file. Before exporting we run
    ``PRAGMA wal_checkpoint(TRUNCATE)`` so every committed page is folded into
    the main DB and the export reflects all committed data. (VACUUM INTO reads a
    consistent snapshot regardless, but the explicit checkpoint keeps the live
    main file compact and makes the intent unambiguous.)
  * The export itself carries no WAL: VACUUM INTO produces a fresh DB in the
    default (rollback) journal mode with all data materialized in one file.
  * WAL files MUST NOT live on a network filesystem (R3.6). The live DB and its
    ``-wal`` stay on local disk (data/); only the finished single-file export is
    safe to copy elsewhere.

Default destination: ``data/backups/gridsignals-YYYY-MM-DD.db`` (UTC date,
R10.2), with a numeric suffix if that file already exists. Override with
``--out``. The parent directory is created if missing. Prints the written path;
exits 0 on success, non-zero on failure.
"""
import argparse
import os
import sys
from datetime import datetime, timezone

from app.db.connection import get_connection

DEFAULT_BACKUP_DIR = os.path.join("data", "backups")


def default_backup_path(now=None, backup_dir=None):
    """data/backups/gridsignals-YYYY-MM-DD.db using the UTC date (R10.2)."""
    now = now or datetime.now(timezone.utc)
    backup_dir = backup_dir or DEFAULT_BACKUP_DIR
    return os.path.join(backup_dir, f"gridsignals-{now:%Y-%m-%d}.db")


def available_backup_path(path):
    """Return path, or a numbered sibling, without overwriting an export."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    counter = 2
    while True:
        candidate = f"{root}-{counter}{ext}"
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def export(out_path, db_path=None):
    """Write a consistent, portable single-file copy of the DB to out_path.

    Checkpoints the source WAL then runs VACUUM INTO. Read-only w.r.t. the
    source (never mutates it beyond the checkpoint). Returns out_path.
    """
    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = get_connection(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.execute("VACUUM INTO ?;", (out_path,))
    finally:
        conn.close()
    return out_path


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m app.backup",
        description="Export a portable single-file copy of the SQLite DB (R3.6).",
    )
    parser.add_argument(
        "--out",
        help=(
            "destination path (default: data/backups/gridsignals-YYYY-MM-DD.db, UTC; "
            "adds a numeric suffix if needed)"
        ),
    )
    parser.add_argument(
        "--db",
        help="source DB path (default: GRIDSIGNALS_DB env or data/gridsignals.db)",
    )
    args = parser.parse_args(argv)

    out_path = args.out or available_backup_path(default_backup_path())
    try:
        written = export(out_path, db_path=args.db)
    except Exception as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
