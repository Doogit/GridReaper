"""GridReaper database connection helper."""
import os
import sqlite3

DEFAULT_DB_PATH = "data/gridreaper.db"


def get_connection(db_path=DEFAULT_DB_PATH):
    """Open a SQLite connection with WAL, FK enforcement, and a busy timeout.

    Creates the parent directory if absent. Applies per-connection pragmas
    (per v3 R3.2): WAL journal mode + 5s busy_timeout, plus foreign_keys=ON.
    """
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn
