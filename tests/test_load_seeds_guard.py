"""Seed-loader guards: reload preservation (Pattern B) and R4.5 fail-loud.

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
from unittest import mock

from app.db import load_seeds
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


class TestDeregisteredSourceGuard(unittest.TestCase):
    """`ransomlook` was deregistered: it advertised a capability with no
    fetcher module behind it. A fresh load must not create it, and a reload
    must not resurrect it. The flip side of Pattern B (no seed-scoped DELETE)
    is that dropping the CSV row does NOT remove an already-loaded row from an
    existing DB - that is an operator action via the Admin source registry."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.db = os.path.join(self.tmpdir, "dereg.db")

    def _source_ids(self):
        conn = sqlite3.connect(self.db)
        try:
            return [r[0] for r in conn.execute(
                "SELECT source_id FROM source_policies")]
        finally:
            conn.close()

    def test_ransomlook_absent_and_fulltext_registered_across_reloads(self):
        load(db_path=self.db)
        first = self._source_ids()
        load(db_path=self.db)          # loader stays idempotent after the edit
        second = self._source_ids()

        self.assertNotIn("ransomlook", first)
        self.assertNotIn("ransomlook", second)
        self.assertEqual(sorted(first), sorted(second))
        # sec_edgar_fulltext keeps its registration - it now has a fetcher.
        self.assertEqual(second.count("sec_edgar_fulltext"), 1)

    def test_reload_does_not_resurrect_a_manually_removed_row(self):
        load(db_path=self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO source_policies (source_id, name, enabled) "
                     "VALUES ('ransomlook', 'RansomLook API', 1)")
        conn.commit()
        conn.execute("DELETE FROM source_policies WHERE source_id='ransomlook'")
        conn.commit()
        conn.close()

        load(db_path=self.db)
        self.assertNotIn("ransomlook", self._source_ids())


class TestMissingSeedFileFailsLoudly(unittest.TestCase):
    """R4.5: a missing seed/reference file MUST fail the seed job with a clear
    operator-facing error.

    The fail-loud path exists (``read_rows`` raises FileNotFoundError) but had
    no test, so nothing stopped a future refactor from turning it into a silent
    skip - which would load a partial store that looks successful. These tests
    pin both halves of the requirement: the error NAMES the missing file, and
    the job aborts rather than committing the tables it already read.

    The seeds directory is copied to a temp dir and SEEDS_DIR is pointed at the
    copy; the real ``seeds/`` CSVs are never touched.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.seeds = os.path.join(self.tmpdir, "seeds")
        shutil.copytree(load_seeds.SEEDS_DIR, self.seeds)
        self.db = os.path.join(self.tmpdir, "missing_seed.db")

    def test_read_rows_names_the_missing_file(self):
        absent = os.path.join(self.seeds, "no_such_seed.csv")
        with self.assertRaises(FileNotFoundError) as ctx:
            load_seeds.read_rows(absent)
        self.assertIn("no_such_seed.csv", str(ctx.exception))

    def test_load_aborts_and_commits_nothing_when_a_seed_csv_is_absent(self):
        # license_matrix.csv is one of the six hand-verified seeds and loads
        # after products/triggers, so its absence must also roll those back.
        os.remove(os.path.join(self.seeds, "license_matrix.csv"))
        with mock.patch.object(load_seeds, "SEEDS_DIR", self.seeds):
            with self.assertRaises(FileNotFoundError) as ctx:
                load(db_path=self.db)
        self.assertIn("license_matrix.csv", str(ctx.exception))

        # The job aborted: the tables loaded before the missing file are empty.
        conn = sqlite3.connect(self.db)
        for table in ("products", "triggers", "watchlist_entities"):
            self.assertEqual(
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0,
                f"{table} was committed despite a missing seed file (R4.5)")
        conn.close()


if __name__ == "__main__":
    unittest.main()
