"""get_connection env-override behavior (writer CLIs must be isolatable).

The audit judge / goldens CLIs open the DB via get_connection() with no path;
they must honor GRIDSIGNALS_DB so an operator can point a real-key test run at a
throwaway copy without writing to the primary DB — the same override the UI
already relies on.
"""
import os
import unittest

from app.db import connection


class TestGetConnectionOverride(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("GRIDSIGNALS_DB")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("GRIDSIGNALS_DB", None)
        else:
            os.environ["GRIDSIGNALS_DB"] = self._saved

    def test_env_var_honored_when_no_path_given(self):
        target = os.path.join(self.scratch(), "env_override.db")
        os.environ["GRIDSIGNALS_DB"] = target
        conn = connection.get_connection()
        try:
            path = conn.execute("PRAGMA database_list").fetchall()[0][2]
        finally:
            conn.close()
        self.assertTrue(path.endswith("env_override.db"))

    def test_explicit_path_wins_over_env(self):
        os.environ["GRIDSIGNALS_DB"] = os.path.join(self.scratch(), "ignored.db")
        explicit = os.path.join(self.scratch(), "explicit.db")
        conn = connection.get_connection(explicit)
        try:
            path = conn.execute("PRAGMA database_list").fetchall()[0][2]
        finally:
            conn.close()
        self.assertTrue(path.endswith("explicit.db"))

    def test_default_when_no_env_and_no_path(self):
        os.environ.pop("GRIDSIGNALS_DB", None)
        conn = connection.get_connection()
        try:
            path = conn.execute("PRAGMA database_list").fetchall()[0][2]
        finally:
            conn.close()
        self.assertTrue(path.replace("\\", "/").endswith(
            connection.DEFAULT_DB_PATH.replace("\\", "/")))

    def scratch(self):
        import tempfile
        d = os.path.join(tempfile.gettempdir(), "gs_conn_test")
        os.makedirs(d, exist_ok=True)
        return d


if __name__ == "__main__":
    unittest.main()
