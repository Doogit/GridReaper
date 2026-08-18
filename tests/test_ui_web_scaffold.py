"""Chunk 0 scaffold tests for the FastAPI web UI (R8.9 UI port).

Hermetic and network-free: FastAPI TestClient drives the app in-process; the
deps.get_db test builds a temp-file SQLite DB via apply_migrations (FK on)
and points the connection at it through the GRIDSIGNALS_DB override — the
same env seam the CLIs and the retired Streamlit UI used. These replace
nothing yet; the real page/AppTest ports arrive in later chunks. This file
only proves the scaffold boots, serves its static design assets, renders
through the base layout, and that the read/write DB seam is wired.
"""
import os
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.db.migrate import apply_migrations
from app.ui import data
from app.ui_web import deps
from app.ui_web.app import app


class ScaffoldAppTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_healthz_ok(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text, "ok")

    # The base layout rendered through "/" is now the DB-backed Home feed; that
    # (and its base.html chrome) is covered by tests/test_ui_web_feed.py, which
    # seeds a hermetic DB. These scaffold tests stay infra-only (no DB).

    def test_app_css_served_with_tokens(self):
        resp = self.client.get("/static/app.css")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/css", resp.headers["content-type"])
        # design tokens ported from theme.py must reach the DOM
        self.assertIn("--gs-bg", resp.text)
        self.assertIn("--gs-accent", resp.text)

    def test_htmx_served(self):
        resp = self.client.get("/static/htmx.min.js")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("htmx", resp.text)
        self.assertIn('version:"2.0.4"', resp.text)


class DepsSeamTest(unittest.TestCase):
    def test_config_write_reexports_data_lock(self):
        # deps re-exports the single-writer lock so routes bind the UI package's
        # seam, not app.ui.data directly (R3.2).
        self.assertIs(deps.config_write, data.config_write_conn)

    def test_get_db_honors_gridsignals_db_override(self):
        prior = os.environ.get("GRIDSIGNALS_DB")
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "scaffold.db")
            seed = sqlite3.connect(db_path)
            try:
                seed.execute("PRAGMA foreign_keys=ON;")
                apply_migrations(seed)
                seed.commit()
            finally:
                seed.close()

            os.environ["GRIDSIGNALS_DB"] = db_path
            try:
                gen = deps.get_db()
                conn = next(gen)
                try:
                    # FK enforcement is on (get_connection pragma)
                    self.assertEqual(
                        conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                    # points at the overridden DB: a migrated table is visible
                    row = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name='schema_migrations'").fetchone()
                    self.assertIsNotNone(row)
                finally:
                    gen.close()  # runs the finally: conn.close()
            finally:
                if prior is None:
                    os.environ.pop("GRIDSIGNALS_DB", None)
                else:
                    os.environ["GRIDSIGNALS_DB"] = prior


if __name__ == "__main__":
    unittest.main()
