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
from app.db.connection import get_connection
from app.ui import data


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


class TestWatchlistReloadGuard(unittest.TestCase):
    """R8.7 entity editors: operator watchlist edits survive a seed reload.

    Curation columns (subsector, ...) are guarded like the #9 columns; the
    runtime active flag (not a CSV column) is untouched by the upsert; operator
    aliases written with a distinct source escape the loader's seed-scoped
    DELETE. Identifier/name columns still refresh so seed corrections flow.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.db = os.path.join(self.tmpdir, "wl_guard.db")
        load(db_path=self.db)

    def _conn(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def test_operator_watchlist_edits_survive_reload(self):
        conn = self._conn()
        e = conn.execute("SELECT entity_id, name, subsector FROM watchlist_entities "
                         "WHERE TRIM(COALESCE(subsector,'')) != '' LIMIT 1").fetchone()
        eid, seed_name, seed_subsector = e["entity_id"], e["name"], e["subsector"]
        op_subsector = seed_subsector + " -- OPERATOR EDIT"

        # Operator edits: a guarded curation column + a soft-disable + operator
        # alias/collision rows; and a JUNK edit to an unguarded column (name).
        conn.execute("UPDATE watchlist_entities SET subsector = ?, active = 0 "
                     "WHERE entity_id = ?", (op_subsector, eid))
        conn.execute("UPDATE watchlist_entities SET name = 'JUNK_TYPO' "
                     "WHERE entity_id = ?", (eid,))
        conn.execute("INSERT INTO entity_aliases (entity_id, alias, source) "
                     "VALUES (?, 'Operator Added Alias', 'operator')", (eid,))
        conn.execute("INSERT INTO entity_collision_terms (entity_id, term, reason) "
                     "VALUES (?, 'op collision', 'operator')", (eid,))
        conn.commit()
        conn.close()

        load(db_path=self.db)          # reload must preserve operator state

        conn = self._conn()
        row = conn.execute(
            "SELECT subsector, active, name FROM watchlist_entities "
            "WHERE entity_id = ?", (eid,)).fetchone()
        alias = conn.execute(
            "SELECT 1 FROM entity_aliases WHERE entity_id = ? AND source = 'operator'",
            (eid,)).fetchone()
        term = conn.execute(
            "SELECT 1 FROM entity_collision_terms "
            "WHERE entity_id = ? AND reason = 'operator'", (eid,)).fetchone()
        conn.close()

        # Guarded curation column + runtime flag survived.
        self.assertEqual(row["subsector"], op_subsector)
        self.assertEqual(row["active"], 0)
        # Unguarded identifier/name column reverted to the seed CSV value.
        self.assertEqual(row["name"], seed_name)
        # Operator alias/collision rows escaped the loader's seed-scoped DELETE.
        self.assertIsNotNone(alias)
        self.assertIsNotNone(term)


class TestSourceRegistryReloadGuard(unittest.TestCase):
    """R9.5 source registry: an operator-added source survives a seed reload,
    and a junked seeded-source column (name, which IS in update_cols) reverts to
    the CSV. There is no seed-scoped DELETE on source_policies, so the operator
    row is never wiped; origin (absent from the CSV) stays 'operator'."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.db = os.path.join(self.tmpdir, "src_guard.db")
        load(db_path=self.db)

    def _conn(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def test_operator_source_survives_and_junked_seed_name_reverts(self):
        wconn = get_connection(self.db)
        data.add_source(wconn, "op_feed", "Operator Feed", access_method="rss")
        wconn.close()

        conn = self._conn()
        seeded = conn.execute("SELECT source_id, name FROM source_policies "
                              "WHERE origin = 'seed' LIMIT 1").fetchone()
        sid, seed_name = seeded["source_id"], seeded["name"]
        # Junk a seeded source's name (a column that IS in update_cols).
        conn.execute("UPDATE source_policies SET name = 'JUNK_TYPO' "
                     "WHERE source_id = ?", (sid,))
        conn.commit()
        conn.close()

        load(db_path=self.db)          # reload

        conn = self._conn()
        op = conn.execute(
            "SELECT name, origin FROM source_policies WHERE source_id = 'op_feed'"
            ).fetchone()
        reverted = conn.execute(
            "SELECT name FROM source_policies WHERE source_id = ?",
            (sid,)).fetchone()["name"]
        conn.close()

        # Operator source survived reload with origin intact.
        self.assertIsNotNone(op)
        self.assertEqual(op["name"], "Operator Feed")
        self.assertEqual(op["origin"], "operator")
        # Junked seed-source name reverted to the CSV value.
        self.assertEqual(reverted, seed_name)
        self.assertNotEqual(reverted, "JUNK_TYPO")


if __name__ == "__main__":
    unittest.main()
