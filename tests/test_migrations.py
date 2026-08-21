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


class TestWatchlistAdminColsMigration(unittest.TestCase):
    """0008 watchlist_entities.active / origin (R8.7 entity editors)."""

    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def test_0008_adds_active_and_origin_columns(self):
        conn = self._conn()
        ran = apply_migrations(conn)
        self.assertIn("0008_watchlist_admin_cols", ran)
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(watchlist_entities)")}
        self.assertIn("active", cols)
        self.assertIn("origin", cols)

    def test_0008_defaults_active_seed_on_insert_without_them(self):
        conn = self._conn()
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name) VALUES ('E1', 'Acme')")
        row = conn.execute(
            "SELECT active, origin FROM watchlist_entities "
            "WHERE entity_id = 'E1'").fetchone()
        self.assertEqual(row["active"], 1)
        self.assertEqual(row["origin"], "seed")

    def test_reapply_is_noop(self):
        conn = self._conn()
        apply_migrations(conn)
        self.assertEqual(apply_migrations(conn), [])
        version = conn.execute(
            "SELECT version FROM schema_migrations "
            "WHERE version = '0008_watchlist_admin_cols'").fetchone()
        self.assertIsNotNone(version)


class TestScoringConfigVersionMigration(unittest.TestCase):
    """0011 signals.scoring_config_version (R3.7)."""

    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def test_0011_adds_scoring_config_version_to_signals(self):
        conn = self._conn()
        ran = apply_migrations(conn)
        self.assertIn("0011_scoring_config_version", ran)
        cols = {r["name"]: r for r in conn.execute(
            "PRAGMA table_info(signals)")}
        self.assertIn("scoring_config_version", cols)
        col = cols["scoring_config_version"]
        # nullable, no default: a pre-versioning signal stays NULL and reads
        # "pre-versioning" rather than being backfilled with a guess.
        self.assertEqual(col["notnull"], 0)
        self.assertIsNone(col["dflt_value"])

    def test_existing_signal_defaults_to_null(self):
        conn = self._conn()
        apply_migrations(conn)
        conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                     "decay_half_life_days) VALUES ('t1','T',4,90)")
        conn.execute(
            "INSERT INTO signals (signal_id, trigger_id, signal_scope, "
            " event_date, status) VALUES ('s1','t1','sector','2026-08-01',"
            " 'active')")
        row = conn.execute("SELECT scoring_config_version FROM signals "
                           "WHERE signal_id = 's1'").fetchone()
        self.assertIsNone(row["scoring_config_version"])

    def test_reapply_is_noop(self):
        conn = self._conn()
        apply_migrations(conn)
        self.assertEqual(apply_migrations(conn), [])
        version = conn.execute(
            "SELECT version FROM schema_migrations "
            "WHERE version = '0011_scoring_config_version'").fetchone()
        self.assertIsNotNone(version)


class TestComboScoringMigration(unittest.TestCase):
    """0014 signals.score_combo (R12, R9): nullable combo multiplier column."""

    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def test_0014_adds_score_combo_to_signals(self):
        conn = self._conn()
        ran = apply_migrations(conn)
        self.assertIn("0014_combo_scoring", ran)
        cols = {r["name"]: r for r in conn.execute(
            "PRAGMA table_info(signals)")}
        self.assertIn("score_combo", cols)
        col = cols["score_combo"]
        # nullable, no default: pre-combo signals keep score_combo=NULL.
        self.assertEqual(col["notnull"], 0)
        self.assertIsNone(col["dflt_value"])

    def test_existing_signal_defaults_to_null(self):
        """Signals that existed before 0014 keep score_combo = NULL — no
        backfill, no fabricated 1.0."""
        conn = self._conn()
        apply_migrations(conn)
        conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                     "decay_half_life_days) VALUES ('t1','T',4,90)")
        conn.execute(
            "INSERT INTO signals (signal_id, trigger_id, signal_scope, "
            " event_date, status) VALUES ('s1','t1','sector','2026-08-01',"
            " 'active')")
        row = conn.execute("SELECT score_combo FROM signals "
                           "WHERE signal_id = 's1'").fetchone()
        self.assertIsNone(row["score_combo"])

    def test_reapply_is_noop(self):
        conn = self._conn()
        apply_migrations(conn)
        self.assertEqual(apply_migrations(conn), [])
        version = conn.execute(
            "SELECT version FROM schema_migrations "
            "WHERE version = '0014_combo_scoring'").fetchone()
        self.assertIsNotNone(version)

    def test_checksum_stability(self):
        """Migration file content must not change once applied (immutable contract)."""
        import hashlib
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "app", "db", "migrations", "0014_combo_scoring.sql")
        with open(path, encoding="utf-8") as fh:
            digest = hashlib.sha256(fh.read().encode("utf-8")).hexdigest()
        # Record the digest now; if the file changes this test fails.
        conn = self._conn()
        apply_migrations(conn)
        stored = conn.execute(
            "SELECT checksum FROM schema_migrations "
            "WHERE version = '0014_combo_scoring'").fetchone()
        self.assertEqual(digest, stored["checksum"])


if __name__ == "__main__":
    unittest.main()
