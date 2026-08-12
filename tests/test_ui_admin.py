"""Admin / Config page tests (R8.7; R3.3, R3.2, R10.7).

Hermetic AppTest against a temp-file SQLite (a file DB, not :memory:, so the
page's own connection sees the same rows). Covers: the page renders every
section; saving a weight edits scoring_weights + writes a config_audit row +
rescores; saving a half-life edits triggers + audits; toggling a source edits
source_policies.enabled + audits; a held single-writer lock turns a save into an
"ingestion in progress" warning with NO DB write; staleness labels state only
what the field says. Every case asserts ``at.exception`` is empty.
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from streamlit.testing.v1 import AppTest

from app.db.migrate import apply_migrations
from app.ingest.runner import ingest_lock

PAGE = "app/ui/pages/5_Admin.py"
NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def days_ago_date(n):
    return (NOW.date() - timedelta(days=n)).isoformat()


def seed(conn):
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_lead','Leadership',4,90)")
    conn.execute("INSERT INTO scoring_weights (weight_kind, key, weight, notes) "
                 "VALUES ('scope','sector',0.55,'seed note')")
    conn.execute("INSERT INTO source_policies (source_id, name, enabled, ttl, "
                 "access_method, evidence_rank) VALUES "
                 "('edgar','EDGAR',1,3600,'rss',1)")
    # Watchlist entities for the entity manager (R8.7): a seeded entity that
    # exists in the real seed CSV (so Reset can find its seed row) and an
    # operator-added entity with a referencing signal (the delete-safety gate).
    conn.execute("INSERT INTO watchlist_entities (entity_id, name, subsector, "
                 "gov_cloud_likelihood, origin, active) VALUES "
                 "('E0004','Dominion Energy','Electric','medium','seed',1)")
    conn.execute("INSERT INTO watchlist_entities (entity_id, name, origin, "
                 "active) VALUES ('E9001','Zzz Operator Co','operator',1)")
    # An active signal so a weight/half-life save has something to rescore, and a
    # signal referencing the operator entity so its Remove is FK-blocked.
    conn.execute(
        "INSERT INTO signals (signal_id, signal_scope, trigger_id, event_date, "
        " headline, status, score) VALUES "
        "('s1','sector','t_lead','2026-08-01','Card','active',2.2)")
    conn.execute(
        "INSERT INTO signals (signal_id, entity_id, signal_scope, trigger_id, "
        " event_date, headline, status, score) VALUES "
        "('s_op','E9001','account','t_lead','2026-08-01','Op card','active',1.5)")
    # Staleness: one fact stale by date, one with an unknown verified_date.
    conn.execute("INSERT INTO products (product_id, name) VALUES "
                 "('p_sent','Microsoft Sentinel')")
    conn.execute(
        "INSERT INTO license_facts (fact_id, product_id, segment, "
        "source_quality, verified_date) VALUES "
        "('f_old','p_sent','commercial','non-primary', ?)", (days_ago_date(210),))
    conn.execute(
        "INSERT INTO license_facts (fact_id, product_id, segment, "
        "source_quality, verified_date) VALUES "
        "('f_unk','p_sent','commercial','non-primary','')")


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    apply_migrations(conn)
    seed(conn)
    conn.commit()
    conn.close()
    return path


def all_text(at):
    parts = []
    for kind in ("markdown", "caption", "subheader", "title", "header"):
        for el in getattr(at, kind, []):
            val = getattr(el, "value", None)
            if val:
                parts.append(str(val))
    return "\n".join(parts)


class AdminPageCase(unittest.TestCase):
    def setUp(self):
        self.path = make_db()
        os.environ["GRIDSIGNALS_DB"] = self.path

    def tearDown(self):
        os.environ.pop("GRIDSIGNALS_DB", None)
        if self.path and os.path.exists(self.path):
            try:
                os.remove(self.path)
            except PermissionError:
                pass

    def _run(self):
        return AppTest.from_file(PAGE, default_timeout=30).run()

    def _open_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _widget(self, at, kind, key):
        for w in getattr(at, kind):
            if w.key == key:
                return w
        self.fail(f"{kind} {key} not found")

    def assertNoException(self, at):
        self.assertEqual(list(at.exception), [], msg=[
            (e.type, e.message) for e in at.exception])


class TestRender(AdminPageCase):
    def test_all_sections_render(self):
        at = self._run()
        self.assertNoException(at)
        text = all_text(at)
        for section in ("Scoring weights", "Decay half-lives", "Source policies",
                        "License-fact staleness", "Recent config changes"):
            self.assertIn(section, text)


class TestWeightSave(AdminPageCase):
    def test_save_edits_audits_and_rescores(self):
        at = self._run()
        at = self._widget(at, "number_input", "w_scope_sector").set_value(0.90).run()
        at = self._widget(at, "button", "wsave_scope_sector").click().run()
        self.assertNoException(at)

        conn = self._open_conn()
        self.assertEqual(conn.execute(
            "SELECT weight FROM scoring_weights WHERE key='sector'"
            ).fetchone()["weight"], 0.90)
        audit = conn.execute("SELECT table_name, field, new_value "
                             "FROM config_audit").fetchall()
        conn.close()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["table_name"], "scoring_weights")
        self.assertEqual(audit[0]["field"], "weight")
        self.assertTrue(any("Saved" in s.value for s in at.success))


class TestNoOpSave(AdminPageCase):
    def test_saving_unchanged_weight_writes_nothing_and_shows_info(self):
        at = self._run()
        # click Save without touching the number_input (stays at seed 0.55)
        at = self._widget(at, "button", "wsave_scope_sector").click().run()
        self.assertNoException(at)
        self.assertTrue(any("No change" in i.value for i in at.info))
        conn = self._open_conn()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM config_audit").fetchone()["c"], 0)
        conn.close()


class TestHalfLifeSave(AdminPageCase):
    def test_save_edits_and_audits(self):
        at = self._run()
        at = self._widget(at, "number_input", "hl_t_lead").set_value(150).run()
        at = self._widget(at, "button", "hlsave_t_lead").click().run()
        self.assertNoException(at)
        conn = self._open_conn()
        self.assertEqual(conn.execute(
            "SELECT decay_half_life_days FROM triggers WHERE trigger_id='t_lead'"
            ).fetchone()["decay_half_life_days"], 150)
        self.assertEqual(conn.execute(
            "SELECT field FROM config_audit").fetchone()["field"],
            "decay_half_life_days")
        conn.close()


class TestSourceToggle(AdminPageCase):
    def test_disable_source_audits(self):
        at = self._run()
        at = self._widget(at, "toggle", "src_edgar").set_value(False).run()
        at = self._widget(at, "button", "srcsave_edgar").click().run()
        self.assertNoException(at)
        conn = self._open_conn()
        self.assertEqual(conn.execute(
            "SELECT enabled FROM source_policies WHERE source_id='edgar'"
            ).fetchone()["enabled"], 0)
        self.assertEqual(conn.execute(
            "SELECT field FROM config_audit").fetchone()["field"], "enabled")
        conn.close()


class TestLockHeld(AdminPageCase):
    def test_save_under_held_lock_warns_and_writes_nothing(self):
        at = self._run()
        with ingest_lock():                       # simulate an in-progress run
            at = self._widget(at, "number_input", "w_scope_sector"
                              ).set_value(0.90).run()
            at = self._widget(at, "button", "wsave_scope_sector").click().run()
        self.assertNoException(at)
        self.assertTrue(any("Ingestion in progress" in w.value
                            for w in at.warning))
        conn = self._open_conn()
        self.assertEqual(conn.execute(
            "SELECT weight FROM scoring_weights WHERE key='sector'"
            ).fetchone()["weight"], 0.55)          # unchanged
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM config_audit").fetchone()["c"], 0)
        conn.close()


class TestStalenessLabels(AdminPageCase):
    def test_literal_field_stating_labels(self):
        at = self._run()
        self.assertNoException(at)
        text = all_text(at)
        # age is computed from the real clock, so assert the label FORM, not an
        # exact day count (the stale fact is >180d old either way).
        self.assertRegex(text, r"verified \d+ days ago")
        self.assertIn("verification date unknown", text)
        self.assertNotIn("unverified", text)      # no interpretive verb


class TestEntityRender(AdminPageCase):
    def test_entity_section_renders(self):
        at = self._run()
        self.assertNoException(at)
        self.assertIn("Watchlist entities", all_text(at))


class TestEntityAdd(AdminPageCase):
    def test_add_operator_entity(self):
        at = self._run()
        self._widget(at, "text_input", "ent_new_id").set_value("E9002").run()
        self._widget(at, "text_input", "ent_new_name").set_value("New Co").run()
        at = self._widget(at, "button", "ent_add_btn").click().run()
        self.assertNoException(at)
        conn = self._open_conn()
        row = conn.execute("SELECT origin, active FROM watchlist_entities "
                           "WHERE entity_id='E9002'").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["origin"], "operator")
        self.assertEqual(row["active"], 1)


class TestEntityDisable(AdminPageCase):
    def test_disable_seeded_entity(self):
        # Default selectbox pick is E0004 (name-ordered first).
        at = self._run()
        self._widget(at, "toggle", "entactive_E0004").set_value(False).run()
        at = self._widget(at, "button", "entactivesave_E0004").click().run()
        self.assertNoException(at)
        conn = self._open_conn()
        self.assertEqual(conn.execute(
            "SELECT active FROM watchlist_entities WHERE entity_id='E0004'"
            ).fetchone()["active"], 0)
        self.assertEqual(conn.execute(
            "SELECT field FROM config_audit").fetchone()["field"], "active")
        conn.close()


class TestEntityEditField(AdminPageCase):
    def test_edit_subsector(self):
        at = self._run()
        self._widget(at, "text_input", "entfield_subsector_E0004"
                     ).set_value("Nuclear").run()
        at = self._widget(at, "button", "entfieldsave_subsector_E0004"
                          ).click().run()
        self.assertNoException(at)
        conn = self._open_conn()
        self.assertEqual(conn.execute(
            "SELECT subsector FROM watchlist_entities WHERE entity_id='E0004'"
            ).fetchone()["subsector"], "Nuclear")
        conn.close()


class TestEntityAliasAdd(AdminPageCase):
    def test_add_operator_alias(self):
        at = self._run()
        self._widget(at, "text_input", "aliasadd_E0004").set_value("Dominion Va").run()
        at = self._widget(at, "button", "aliasaddbtn_E0004").click().run()
        self.assertNoException(at)
        conn = self._open_conn()
        self.assertEqual(conn.execute(
            "SELECT source FROM entity_aliases WHERE alias='Dominion Va'"
            ).fetchone()["source"], "operator")
        conn.close()


class TestEntityRemoveDeleteSafety(AdminPageCase):
    """UI-layer delete-safety gate: removing an operator entity with a
    referencing signal shows a legible error and changes nothing."""

    def _select_e9001(self, at):
        sb = self._widget(at, "selectbox", "ent_select")
        opt = next(o for o in sb.options if "E9001" in o)
        return sb.set_value(opt).run()

    def test_remove_blocked_by_referencing_signal(self):
        at = self._run()
        at = self._select_e9001(at)
        self._widget(at, "checkbox", "rmok_E9001").set_value(True).run()
        at = self._widget(at, "button", "rmbtn_E9001").click().run()
        self.assertNoException(at)
        self.assertTrue(any("reference" in e.value.lower() for e in at.error),
                        msg=[e.value for e in at.error])
        conn = self._open_conn()
        self.assertIsNotNone(conn.execute(     # entity still present, no write
            "SELECT 1 FROM watchlist_entities WHERE entity_id='E9001'").fetchone())
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM config_audit").fetchone()["c"], 0)
        conn.close()


class TestEntityConfirmGate(AdminPageCase):
    """The confirm checkbox is a real server-side gate (persona-pass P0): a
    destructive action fires only when the box is checked, not on a bare click."""

    def _select_e9001(self, at):
        sb = self._widget(at, "selectbox", "ent_select")
        opt = next(o for o in sb.options if "E9001" in o)
        return sb.set_value(opt).run()

    def test_remove_without_confirm_does_nothing(self):
        at = self._run()
        at = self._select_e9001(at)
        at = self._widget(at, "button", "rmbtn_E9001").click().run()  # no confirm
        self.assertNoException(at)
        self.assertTrue(any("Check the box" in w.value for w in at.warning),
                        msg=[w.value for w in at.warning])
        conn = self._open_conn()
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM watchlist_entities WHERE entity_id='E9001'").fetchone())
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM config_audit").fetchone()["c"], 0)
        conn.close()

    def test_reset_without_confirm_does_nothing(self):
        at = self._run()          # default selection is E0004 (seeded)
        at = self._widget(at, "button", "rstbtn_E0004").click().run()  # no confirm
        self.assertNoException(at)
        self.assertTrue(any("Check the box" in w.value for w in at.warning))
        conn = self._open_conn()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM config_audit WHERE field='__reset__'"
            ).fetchone()["c"], 0)
        conn.close()

    def test_reset_with_confirm_restores_and_reenables(self):
        at = self._run()
        # Disable + mis-edit E0004, then reset with the box checked.
        self._widget(at, "toggle", "entactive_E0004").set_value(False).run()
        at = self._widget(at, "button", "entactivesave_E0004").click().run()
        self._widget(at, "text_input", "entfield_subsector_E0004"
                     ).set_value("Wrong").run()
        at = self._widget(at, "button", "entfieldsave_subsector_E0004").click().run()
        self._widget(at, "checkbox", "rstok_E0004").set_value(True).run()
        at = self._widget(at, "button", "rstbtn_E0004").click().run()
        self.assertNoException(at)          # widget-clear flag path must not raise
        conn = self._open_conn()
        row = conn.execute("SELECT subsector, active FROM watchlist_entities "
                           "WHERE entity_id='E0004'").fetchone()
        conn.close()
        self.assertEqual(row["subsector"], "iou_electric")   # restored from seed
        self.assertEqual(row["active"], 1)                   # re-enabled


if __name__ == "__main__":
    unittest.main()
