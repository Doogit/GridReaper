"""Regression checks for immutable migration files."""
import hashlib
import os
import sqlite3
import unittest

from app.db.migrate import apply_migrations


MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app",
    "db",
    "migrations",
)

EXPECTED_CHECKSUMS = {
    "0001_initial.sql":
        "4c5dcbe57463479b8b3aa6c83b7ef4451ccc7cd177925e37ed02a7f47f1b3b25",
    "0002_match_decision_parser_version.sql":
        "76385203f7b3190790f549bd11c7b967c5b83ee39b5c9a16b422968c299f34af",
}


class TestMigrationChecksums(unittest.TestCase):
    def test_applied_migration_files_are_immutable(self):
        for filename, expected in EXPECTED_CHECKSUMS.items():
            with self.subTest(migration=filename):
                path = os.path.join(MIGRATIONS_DIR, filename)
                with open(path, encoding="utf-8") as fh:
                    digest = hashlib.sha256(fh.read().encode("utf-8")).hexdigest()
                self.assertEqual(digest, expected)


class TestConfigAuditMigration(unittest.TestCase):
    """0007 config_audit: the R8.7 provenance table (R3.3)."""

    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def test_0007_creates_config_audit_with_expected_columns_and_index(self):
        conn = self._conn()
        ran = apply_migrations(conn)
        self.assertIn("0007_config_audit", ran)
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(config_audit)")}
        self.assertEqual(cols, {"audit_id", "table_name", "pk", "field",
                                "old_value", "new_value", "editor", "reason",
                                "ts"})
        indexes = {r["name"] for r in conn.execute(
            "PRAGMA index_list(config_audit)")}
        self.assertIn("idx_config_audit_ts", indexes)

    def test_reapply_is_noop(self):
        conn = self._conn()
        apply_migrations(conn)
        self.assertEqual(apply_migrations(conn), [])
        version = conn.execute(
            "SELECT version FROM schema_migrations "
            "WHERE version = '0007_config_audit'").fetchone()
        self.assertIsNotNone(version)

    def test_insert_round_trips_with_monotonic_id(self):
        conn = self._conn()
        apply_migrations(conn)
        for i in range(2):
            conn.execute(
                "INSERT INTO config_audit (table_name, pk, field, old_value, "
                " new_value, editor, reason, ts) VALUES "
                "('scoring_weights', ?, 'weight', '1.0', '1.5', 'operator', "
                " 'tuning', '2026-08-12T00:00:0%d+00:00')" % i,
                (f'{{"weight_kind":"subsector","key":"k{i}"}}',))
        rows = conn.execute(
            "SELECT audit_id, field FROM config_audit ORDER BY audit_id").fetchall()
        self.assertEqual([r["audit_id"] for r in rows], [1, 2])
        self.assertEqual(rows[0]["field"], "weight")


if __name__ == "__main__":
    unittest.main()
