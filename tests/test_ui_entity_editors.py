"""Watchlist entity-editor helper tests (R8.7 entity manager; R3.3, R4.4, R6.3).

Hermetic: real migrations against in-memory SQLite, FK on, fixture rows. Covers
add / edit / soft-disable / alias+collision editing / reset-to-seed, the
frozen-score contract (entity edits never rescore), and the LOAD-BEARING
delete-safety gate: remove_operator_entity must block legibly when a referencing
row is present (trap 2), never crash or orphan. The helpers take a passed
connection so DB assertions stay hermetic; the page's lock path is covered in
test_ui_admin.py.
"""
import sqlite3
import unittest
from datetime import datetime, timezone

import app.db.load_seeds as load_seeds
from app.db.migrate import apply_migrations
from app.ui import data

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    # A seeded entity (origin defaults to 'seed', active to 1) with a seed alias
    # and a seed collision term (the loader's source/reason markers).
    conn.execute("INSERT INTO watchlist_entities (entity_id, name, subsector, "
                 "gov_cloud_likelihood) VALUES ('E_SEED','Duke Energy','Electric','medium')")
    conn.execute("INSERT INTO entity_aliases (entity_id, alias, source) "
                 "VALUES ('E_SEED','Duke','watchlist_entities.csv')")
    conn.execute("INSERT INTO entity_collision_terms (entity_id, term, reason) "
                 "VALUES ('E_SEED','Duke University','seeded from watchlist_entities.csv')")
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_lead','Leadership',4,90)")
    conn.commit()
    return conn


def _seed_signal(conn, signal_id, entity_id, score=2.5):
    conn.execute(
        "INSERT INTO signals (signal_id, entity_id, signal_scope, trigger_id, "
        " event_date, headline, status, score, score_base, score_decay, "
        " score_account_fit, score_scope_fit) VALUES "
        "(?, ?, 'account', 't_lead', '2026-08-01', 'Card', 'active', ?, 4, 1.0, "
        " 1.0, 0.625)", (signal_id, entity_id, score))
    conn.commit()


class TestAddEntity(unittest.TestCase):
    def test_happy_adds_operator_row_and_audits(self):
        conn = fixture_conn()
        res = data.add_watchlist_entity(conn, "E_OP", "Op Utility",
                                        fields={"subsector": "Gas"},
                                        reason="missed by seed", now=NOW)
        self.assertTrue(res["created"])
        row = conn.execute("SELECT name, origin, active, subsector FROM "
                           "watchlist_entities WHERE entity_id='E_OP'").fetchone()
        self.assertEqual(row["name"], "Op Utility")
        self.assertEqual(row["origin"], "operator")
        self.assertEqual(row["active"], 1)
        self.assertEqual(row["subsector"], "Gas")
        audit = conn.execute("SELECT * FROM config_audit").fetchall()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["table_name"], "watchlist_entities")
        self.assertEqual(audit[0]["field"], "__add__")
        self.assertTrue(audit[0]["ts"].startswith("2026-08-01"))

    def test_duplicate_id_rejected(self):
        conn = fixture_conn()
        with self.assertRaises(ValueError):
            data.add_watchlist_entity(conn, "E_SEED", "Dup")
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM config_audit")
                         .fetchone()["n"], 0)

    def test_empty_name_and_bad_field_rejected(self):
        conn = fixture_conn()
        with self.assertRaises(ValueError):
            data.add_watchlist_entity(conn, "E_X", "")
        with self.assertRaises(ValueError):
            data.add_watchlist_entity(conn, "E_X", "X", fields={"cik": "123"})


class TestUpdateEntity(unittest.TestCase):
    def test_happy_edit_and_audit(self):
        conn = fixture_conn()
        res = data.update_watchlist_entity(conn, "E_SEED", "subsector",
                                           "Nuclear", now=NOW)
        self.assertTrue(res["changed"])
        self.assertEqual(conn.execute(
            "SELECT subsector FROM watchlist_entities WHERE entity_id='E_SEED'")
            .fetchone()["subsector"], "Nuclear")
        self.assertEqual(conn.execute("SELECT field FROM config_audit").fetchone()
                         ["field"], "subsector")

    def test_non_editable_field_rejected(self):
        conn = fixture_conn()
        with self.assertRaises(ValueError):
            data.update_watchlist_entity(conn, "E_SEED", "cik", "0001234")

    def test_noop_writes_nothing(self):
        conn = fixture_conn()
        res = data.update_watchlist_entity(conn, "E_SEED", "subsector", "Electric")
        self.assertFalse(res["changed"])
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM config_audit")
                         .fetchone()["n"], 0)


class TestSetActiveFrozenContract(unittest.TestCase):
    def test_disable_audits_without_rescore_and_preserves_scores(self):
        conn = fixture_conn()
        _seed_signal(conn, "s1", "E_SEED", score=2.5)
        # Spy: entity edits must never call rescore (frozen explainability).
        orig = data.rescore
        data.rescore = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("entity edit must not rescore"))
        self.addCleanup(lambda: setattr(data, "rescore", orig))
        res = data.set_entity_active(conn, "E_SEED", False, now=NOW)
        self.assertEqual(res["new"], 0)
        self.assertEqual(conn.execute(
            "SELECT active FROM watchlist_entities WHERE entity_id='E_SEED'")
            .fetchone()["active"], 0)
        # Existing signal score untouched.
        self.assertEqual(conn.execute(
            "SELECT score FROM signals WHERE signal_id='s1'").fetchone()["score"],
            2.5)

    def test_disable_noop_when_already_disabled(self):
        conn = fixture_conn()
        data.set_entity_active(conn, "E_SEED", False)
        res = data.set_entity_active(conn, "E_SEED", False)
        self.assertFalse(res["changed"])


class TestAliasCollisionEditor(unittest.TestCase):
    def test_add_and_remove_operator_alias(self):
        conn = fixture_conn()
        data.add_alias(conn, "E_SEED", "Duke Power", now=NOW)
        rows = data.entity_alias_rows(conn, "E_SEED")
        op = [a for a in rows["aliases"] if a["alias"] == "Duke Power"][0]
        self.assertEqual(op["source"], "operator")
        self.assertTrue(op["operator_editable"])
        # The seed alias is marked not operator-editable.
        seed = [a for a in rows["aliases"] if a["alias"] == "Duke"][0]
        self.assertFalse(seed["operator_editable"])
        data.remove_alias(conn, "E_SEED", "Duke Power")
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM entity_aliases WHERE alias='Duke Power'").fetchone())

    def test_duplicate_alias_and_seed_removal_rejected(self):
        conn = fixture_conn()
        data.add_alias(conn, "E_SEED", "Duke Power")
        with self.assertRaises(ValueError):
            data.add_alias(conn, "E_SEED", "Duke Power")
        with self.assertRaises(ValueError):      # refuse to remove a seed alias
            data.remove_alias(conn, "E_SEED", "Duke")

    def test_collision_term_add_remove_and_seed_guard(self):
        conn = fixture_conn()
        data.add_collision_term(conn, "E_SEED", "Duke Blue Devils")
        self.assertEqual(conn.execute(
            "SELECT reason FROM entity_collision_terms WHERE term='Duke Blue Devils'")
            .fetchone()["reason"], "operator")
        with self.assertRaises(ValueError):      # seed term not operator-removable
            data.remove_collision_term(conn, "E_SEED", "Duke University")
        data.remove_collision_term(conn, "E_SEED", "Duke Blue Devils")
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM entity_collision_terms WHERE term='Duke Blue Devils'")
            .fetchone())


class TestResetToSeed(unittest.TestCase):
    def _patch_seed_csv(self):
        header = ["entity_id", "name", "subsector", "parent_id", "richness",
                  "coverage_flag", "gov_cloud_likelihood", "notes", "owning_seller"]
        seed_row = {"entity_id": "E_SEED", "name": "Duke Energy",
                    "subsector": "Electric", "parent_id": "", "richness": "",
                    "coverage_flag": "", "gov_cloud_likelihood": "medium",
                    "notes": "", "owning_seller": ""}
        orig = load_seeds.read_rows
        load_seeds.read_rows = lambda path: (header, [seed_row])
        self.addCleanup(lambda: setattr(load_seeds, "read_rows", orig))

    def test_reset_restores_columns_reenables_and_drops_operator_rows(self):
        conn = fixture_conn()
        self._patch_seed_csv()
        data.update_watchlist_entity(conn, "E_SEED", "subsector", "Wrong")
        data.set_entity_active(conn, "E_SEED", False)
        data.add_alias(conn, "E_SEED", "Op Alias")
        data.reset_entity_to_seed(conn, "E_SEED", now=NOW)
        row = conn.execute("SELECT subsector, active FROM watchlist_entities "
                           "WHERE entity_id='E_SEED'").fetchone()
        self.assertEqual(row["subsector"], "Electric")   # restored from seed
        self.assertEqual(row["active"], 1)               # re-enabled
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM entity_aliases WHERE alias='Op Alias'").fetchone())
        # The seed alias survives the reset.
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM entity_aliases WHERE alias='Duke'").fetchone())

    def test_reset_on_operator_entity_rejected(self):
        conn = fixture_conn()
        data.add_watchlist_entity(conn, "E_OP", "Op")
        with self.assertRaises(ValueError):
            data.reset_entity_to_seed(conn, "E_OP")

    def test_reset_delete_safety_referencing_signal_survives(self):
        conn = fixture_conn()
        self._patch_seed_csv()
        _seed_signal(conn, "s1", "E_SEED")
        data.update_watchlist_entity(conn, "E_SEED", "subsector", "Wrong")
        data.reset_entity_to_seed(conn, "E_SEED")   # must not crash/orphan
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM signals WHERE signal_id='s1'").fetchone())


class TestRemoveOperatorEntityDeleteSafety(unittest.TestCase):
    """The load-bearing delete-safety gate (trap 2): the one hard-delete path."""

    def test_remove_blocked_by_referencing_signal(self):
        conn = fixture_conn()
        data.add_watchlist_entity(conn, "E_OP", "Op Utility")
        data.add_alias(conn, "E_OP", "OpCo")
        _seed_signal(conn, "s_op", "E_OP")           # a referencing row present
        with self.assertRaises(ValueError) as ctx:
            data.remove_operator_entity(conn, "E_OP")
        self.assertIn("reference", str(ctx.exception).lower())
        # No partial delete: entity AND its alias still present.
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM watchlist_entities WHERE entity_id='E_OP'").fetchone())
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM entity_aliases WHERE entity_id='E_OP'").fetchone())

    def test_remove_succeeds_when_unreferenced(self):
        conn = fixture_conn()
        data.add_watchlist_entity(conn, "E_OP", "Op Utility")
        data.add_alias(conn, "E_OP", "OpCo")
        data.remove_operator_entity(conn, "E_OP", now=NOW)
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM watchlist_entities WHERE entity_id='E_OP'").fetchone())
        self.assertIsNone(conn.execute(     # leaf alias went with it
            "SELECT 1 FROM entity_aliases WHERE entity_id='E_OP'").fetchone())
        self.assertEqual(conn.execute(
            "SELECT field FROM config_audit ORDER BY audit_id DESC LIMIT 1")
            .fetchone()["field"], "__remove__")

    def test_remove_seeded_entity_refused(self):
        conn = fixture_conn()
        with self.assertRaises(ValueError):
            data.remove_operator_entity(conn, "E_SEED")
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM watchlist_entities WHERE entity_id='E_SEED'").fetchone())


class TestEntityEditorRows(unittest.TestCase):
    def test_rows_carry_active_origin_and_signal_count(self):
        conn = fixture_conn()
        _seed_signal(conn, "s1", "E_SEED")
        rows = {r["entity_id"]: r for r in data.entity_editor_rows(conn)}
        self.assertEqual(rows["E_SEED"]["origin"], "seed")
        self.assertEqual(rows["E_SEED"]["active"], 1)
        self.assertEqual(rows["E_SEED"]["signal_count"], 1)


if __name__ == "__main__":
    unittest.main()
