"""GridSignals database connection helper."""
import os
import sqlite3

DEFAULT_DB_PATH = "data/gridsignals.db"


def resolve_db_path(db_path=None):
    """The DB path this run resolves to: explicit argument, else the
    ``GRIDSIGNALS_DB`` env var, else ``DEFAULT_DB_PATH``. The one resolution
    order shared by every reader/writer that needs to know the DB's path (not
    just open a connection to it) — the digest generator's ``digests/``
    directory and the precision report's ``precision-reports/`` directory
    both live beside the DB, so both derive it through this helper instead of
    each independently re-reading ``GRIDSIGNALS_DB``."""
    return db_path or os.environ.get("GRIDSIGNALS_DB") or DEFAULT_DB_PATH


def get_connection(db_path=None):
    """Open a SQLite connection with WAL, FK enforcement, and a busy timeout.

    Creates the parent directory if absent. Applies per-connection pragmas
    (per v3 R3.2): WAL journal mode + 5s busy_timeout, plus foreign_keys=ON.

    With no explicit ``db_path`` the connection honors the ``GRIDSIGNALS_DB``
    env var (else DEFAULT_DB_PATH), so a writer CLI (ingestion, classification,
    the audit judge) can be pointed at a throwaway DB copy for testing without
    touching the primary DB — the same override the UI already uses.
    """
    db_path = resolve_db_path(db_path)
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn
