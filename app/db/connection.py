"""GridSignals database connection helper."""
import os
import sqlite3

DEFAULT_DB_PATH = "data/gridsignals.db"


def get_connection(db_path=None):
    """Open a SQLite connection with WAL, FK enforcement, and a busy timeout.

    Creates the parent directory if absent. Applies per-connection pragmas
    (per v3 R3.2): WAL journal mode + 5s busy_timeout, plus foreign_keys=ON.

    With no explicit ``db_path`` the connection honors the ``GRIDSIGNALS_DB``
    env var (else DEFAULT_DB_PATH), so a writer CLI (ingestion, classification,
    the audit judge) can be pointed at a throwaway DB copy for testing without
    touching the primary DB — the same override the UI already uses.
    """
    if db_path is None:
        db_path = os.environ.get("GRIDSIGNALS_DB") or DEFAULT_DB_PATH
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn
