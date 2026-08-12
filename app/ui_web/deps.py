"""Request-scoped dependencies for the GridSignals web UI.

Read path: ``get_db`` yields a short-lived connection (WAL, FK on) honoring
GRIDSIGNALS_DB, reusing ``app.db.connection.get_connection`` so the web UI opens
the DB exactly like the CLIs and the Streamlit UI.

Write path (later chunks): ``config_write`` re-exports
``app.ui.data.config_write_conn`` — the single-writer lock context manager
(R3.2). Config writes go through it so an ingestion/scoring run in progress
yields a clean 'busy' signal instead of a torn write. No new queries live here;
the UI is a read-only reader over app/ui/data.py.
"""
from app.db.connection import get_connection
from app.ui.data import config_write_conn


def get_db():
    """FastAPI dependency: yield a connection, always close it."""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


# Routes depend on the UI package's seam, not app.ui.data directly.
config_write = config_write_conn
