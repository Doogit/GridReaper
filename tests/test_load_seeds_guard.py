"""Pattern B reload guard: operator-tunable columns survive a seed reload.

R8.7 makes scoring_weights.weight and triggers.decay_half_life_days operator-
editable. The seed loader upserts every CSV on each run, so without a guard a
reload would silently revert an operator edit back to the CSV value - the exact
"reload clobbers runtime-managed columns" trap (docs/solutions, trap 1) that
289 in-memory tests miss because they never re-run load().

These tests exercise the real load() against a temp-file DB (load() opens its
own connection via get_connection(db_path); :memory: would not persist across
the two load() calls). No network - the loader only reads seed CSVs.
"""
import os
import shutil
import sqlite3
import tempfile
import unittest

from app.db.load_seeds import load


class TestReloadGuard(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.db = os.path.join(self.tmpdir, "guard.db")
        load(db_path=self.db)          # first load: pristine seed values

    def _conn(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def test_operator_weight_and_half_life_survive_reload(self):
        conn = self._conn()
        w = conn.execute("SELECT weight_kind, key, weight FROM scoring_weights "
                         "LIMIT 1").fetchone()
        t = conn.execute("SELECT trigger_id, decay_half_life_days, name "
                         "FROM triggers LIMIT 1").fetchone()
        seed_weight = w["weight"]
        seed_half_life = t["decay_half_life_days"]
        seed_name = t["name"]

        # Simulate operator edits: distinct non-seed values.
        op_weight = seed_weight + 0.333
        op_half_life = seed_half_life + 47
        conn.execute("UPDATE scoring_weights SET weight = ? "
                     "WHERE weight_kind = ? AND key = ?",
                     (op_weight, w["weight_kind"], w["key"]))
        conn.execute("UPDATE triggers SET decay_half_life_days = ? "
                     "WHERE trigger_id = ?", (op_half_life, t["trigger_id"]))
        # A sibling column edit that the guard must NOT protect (proves the guard
        # is column-scoped, not row-scoped): name should revert on reload.
        conn.execute("UPDATE triggers SET name = 'JUNK_OPERATOR_TYPO' "
                     "WHERE trigger_id = ?", (t["trigger_id"],))
        conn.commit()
        conn.close()

        load(db_path=self.db)          # second load: guard must preserve edits

        conn = self._conn()
        after_w = conn.execute(
            "SELECT weight FROM scoring_weights WHERE weight_kind = ? AND key = ?",
            (w["weight_kind"], w["key"])).fetchone()["weight"]
        after_t = conn.execute(
            "SELECT decay_half_life_days, name FROM triggers WHERE trigger_id = ?",
            (t["trigger_id"],)).fetchone()
        conn.close()

        # Guarded columns survived the reload.
        self.assertAlmostEqual(after_w, op_weight, places=6)
        self.assertEqual(after_t["decay_half_life_days"], op_half_life)
        # Unguarded sibling reverted to the seed CSV value.
        self.assertEqual(after_t["name"], seed_name)
        self.assertNotEqual(after_t["name"], "JUNK_OPERATOR_TYPO")


if __name__ == "__main__":
    unittest.main()
