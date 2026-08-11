"""Schema migrations (PRD R3.6).

Numbered .sql files under app/db/migrations/ are applied in order inside a
transaction; each applied version is recorded in schema_migrations with a
checksum. Re-running is a no-op. A checksum mismatch on an already-applied
migration is a hard error (edited history), as is a legacy pre-migration
database.
"""
import hashlib
import os
import sys
from datetime import datetime, timezone

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")


def _checksum(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sql_literal(value):
    return "'" + value.replace("'", "''") + "'"


def _is_legacy_db(conn):
    """A DB created before the migration framework: has our tables but no
    schema_migrations. Such DBs hold only regenerable seed data."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    return "schema_migrations" not in tables and "products" in tables


def apply_migrations(conn):
    """Apply unapplied migrations in filename order. Returns applied versions."""
    if _is_legacy_db(conn):
        raise RuntimeError(
            "Legacy pre-migration database detected. It contains only seeded "
            "config data - delete the .db file (and -wal/-shm) and re-run "
            "python -m app.db.load_seeds to regenerate."
        )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL,"
        " checksum TEXT NOT NULL)"
    )
    applied = {r["version"]: r["checksum"] for r in conn.execute(
        "SELECT version, checksum FROM schema_migrations")}

    ran = []
    for fname in sorted(os.listdir(MIGRATIONS_DIR)):
        if not fname.endswith(".sql"):
            continue
        version = fname[:-4]
        with open(os.path.join(MIGRATIONS_DIR, fname), encoding="utf-8") as fh:
            sql = fh.read()
        digest = _checksum(sql)
        if version in applied:
            if applied[version] != digest:
                raise RuntimeError(
                    f"Migration {version} changed after being applied "
                    f"(checksum mismatch). Never edit applied migrations - "
                    f"add a new one."
                )
            continue
        applied_at = datetime.now(timezone.utc).isoformat()
        script = (
            "BEGIN;\n"
            f"{sql.rstrip()}\n"
            "INSERT INTO schema_migrations (version, applied_at, checksum) "
            f"VALUES ({_sql_literal(version)}, {_sql_literal(applied_at)}, "
            f"{_sql_literal(digest)});\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        ran.append(version)
    return ran


if __name__ == "__main__":
    from app.db.connection import get_connection
    conn = get_connection()
    ran = apply_migrations(conn)
    print(f"applied: {ran}" if ran else "up to date")
    conn.close()
    sys.exit(0)
