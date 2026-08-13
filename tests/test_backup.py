"""DB backup / export CLI (R3.6).

Hermetic: a temp on-disk SQLite DB built via apply_migrations, FK on, no network.
Verifies the export is a valid standalone DB (no -wal/-shm dependency), carries
the seeded rows, uses the UTC-dated default path (--out overridable), and leaves
the source DB intact.
"""
import os
import sqlite3
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from app import backup
from app.db.migrate import apply_migrations


def _seed_source(db_path):
    """Build a migrated DB on disk and insert two watchlist_entities rows."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name) VALUES (?, ?)",
        ("e-alpha", "Alpha Power Co"),
    )
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name) VALUES (?, ?)",
        ("e-bravo", "Bravo Grid LLC"),
    )
    conn.commit()
    conn.close()


class TestBackupExport(unittest.TestCase):
    def setUp(self):
        self.tmpdir = TemporaryDirectory(prefix="gs_backup_test_")
        self.tmp = self.tmpdir.name
        self.src = os.path.join(self.tmp, "source.db")
        _seed_source(self.src)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _names(self, db_path):
        conn = sqlite3.connect(db_path)
        try:
            return {
                r[0] for r in conn.execute(
                    "SELECT name FROM watchlist_entities")
            }
        finally:
            conn.close()

    def test_export_is_valid_standalone_db_with_rows(self):
        out = os.path.join(self.tmp, "export.db")
        written = backup.export(out, db_path=self.src)
        self.assertEqual(written, out)
        self.assertTrue(os.path.exists(out))
        # Openable independently, carrying the seeded rows.
        self.assertEqual(
            self._names(out), {"Alpha Power Co", "Bravo Grid LLC"})

    def test_export_has_no_wal_shm_sidecars(self):
        out = os.path.join(self.tmp, "nosidecar.db")
        backup.export(out, db_path=self.src)
        # VACUUM INTO writes a rollback-journal DB: no -wal/-shm files, and it
        # opens with integrity intact standing alone.
        self.assertFalse(os.path.exists(out + "-wal"))
        self.assertFalse(os.path.exists(out + "-shm"))
        conn = sqlite3.connect(out)
        try:
            self.assertEqual(
                conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()

    def test_source_db_intact_after_export(self):
        out = os.path.join(self.tmp, "export2.db")
        backup.export(out, db_path=self.src)
        # Source keeps its rows and stays valid.
        self.assertEqual(
            self._names(self.src), {"Alpha Power Co", "Bravo Grid LLC"})

    def test_default_path_uses_utc_date(self):
        fixed = datetime(2026, 8, 13, tzinfo=timezone.utc)
        path = backup.default_backup_path(now=fixed)
        self.assertEqual(
            path,
            os.path.join("data", "backups", "gridsignals-2026-08-13.db"),
        )

    def test_available_default_path_uses_numeric_suffix_on_collision(self):
        fixed = datetime(2026, 8, 13, tzinfo=timezone.utc)
        backup_dir = os.path.join(self.tmp, "backups")
        os.makedirs(backup_dir)
        base = backup.default_backup_path(now=fixed, backup_dir=backup_dir)
        second = os.path.join(backup_dir, "gridsignals-2026-08-13-2.db")
        with open(base, "w", encoding="utf-8"):
            pass
        with open(second, "w", encoding="utf-8"):
            pass
        self.assertEqual(
            backup.available_backup_path(base),
            os.path.join(backup_dir, "gridsignals-2026-08-13-3.db"),
        )

    def test_out_override_respected(self):
        out = os.path.join(self.tmp, "custom", "my-backup.db")
        rc = backup.main(["--out", out, "--db", self.src])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
