"""First-load detection for the runtime ingest bootstrap (deploy path, R3.1).

The container ships without a baked dataset; ``deploy/entrypoint.sh`` runs the
ingest pipeline on first load only. This module answers the single question that
gates that decision: does the store already hold signals?

Exit code is the entrypoint's signal — 0 = feed empty (run ingest), 1 = feed
populated (skip). Read-only; honors ``GRIDSIGNALS_DB`` like every other CLI. The
schema is assumed present (the entrypoint runs ``app.db.load_seeds`` first).
"""
import sys

from app.db.connection import get_connection


def feed_is_empty(conn):
    """True when the signals table has no rows — the store has never been
    populated (fresh container) or a durable volume was mounted empty."""
    return conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0


def main():
    conn = get_connection()
    try:
        empty = feed_is_empty(conn)
    finally:
        conn.close()
    print("empty" if empty else "populated")
    sys.exit(0 if empty else 1)


if __name__ == "__main__":
    main()
