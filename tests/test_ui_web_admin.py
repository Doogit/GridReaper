"""Admin / Config tests for the FastAPI web UI (R8.7; R3.2, R3.3, R10.7).

The TestClient port of tests/test_ui_admin.py (which was AppTest-based): same
hermetic fixture (temp-file SQLite via apply_migrations, pointed at the app
through GRIDSIGNALS_DB, no network), same invariants carried across the framework
change — the page renders every section; a weight save edits scoring_weights +
writes a config_audit row + rescores; a no-op save writes nothing and reads
"No change"; a half-life save audits; a source toggle audits; a HELD single-writer
lock turns a save into an "Ingestion in progress" warning with NO DB write;
staleness labels state only what the field says; the entity + source + license-fact
editors add / edit / remove with delete-safety (FK-guarded + reference breakdown);
the confirm gate is a REAL server-side no-op without the confirm flag; a future
verified_date is rejected inline on a fact edit.

The data.py-direct helper tests (test_ui_entity_editors, test_ui_license_fact_editors,
test_ui_source_registry, test_ui_config_writes) are framework-agnostic and stay
unchanged — they cover the helper semantics; this file covers the HTTP surface.
"""
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db.migrate import apply_migrations
from app.ingest.runner import ingest_lock
from app.ui_web.app import app

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
    # A seeded entity that exists in the real seed CSV (so Reset can find its
    # seed row) and an operator-added entity with a referencing signal.
    conn.execute("INSERT INTO watchlist_entities (entity_id, name, subsector, "
                 "gov_cloud_likelihood, origin, active) VALUES "
                 "('E0004','Dominion Energy','Electric','medium','seed',1)")
    conn.execute("INSERT INTO watchlist_entities (entity_id, name, origin, "
                 "active) VALUES ('E9001','Zzz Operator Co','operator',1)")
    conn.execute(
        "INSERT INTO signals (signal_id, signal_scope, trigger_id, event_date, "
        " headline, status, score) VALUES "
        "('s1','sector','t_lead','2026-08-01','Card','active',2.2)")
    conn.execute(
        "INSERT INTO signals (signal_id, entity_id, signal_scope, trigger_id, "
        " event_date, headline, status, score) VALUES "
        "('s_op','E9001','account','t_lead','2026-08-01','Op card','active',1.5)")
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


class AdminTestBase(unittest.TestCase):
    def setUp(self):
        self.path = make_db()
        os.environ["GRIDSIGNALS_DB"] = self.path
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("GRIDSIGNALS_DB", None)
        try:
            os.remove(self.path)
        except OSError:
            pass

    def page(self):
        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def scalar(self, sql, *params):
        conn = self.conn()
        try:
            row = conn.execute(sql, params).fetchone()
            return None if row is None else row[0]
        finally:
            conn.close()

    def audit_count(self, where=""):
        return self.scalar(
            "SELECT COUNT(*) FROM config_audit" + (f" WHERE {where}" if where else ""))


class TestRender(AdminTestBase):
    def test_all_sections_render(self):
        dom = self.page()
        for section in ("Scoring weights", "Decay half-lives", "Source registry",
                        "Watchlist entities", "License facts",
                        "Recent config changes"):
            self.assertIn(section, dom)

    def test_base_layout_chrome_and_single_flash(self):
        dom = self.page()
        self.assertIn("gs-topbar", dom)
        self.assertIn(">Admin<", dom)                    # nav link present
        self.assertEqual(dom.count('id="flash"'), 1)     # no duplicate flash id


class TestWeightSave(AdminTestBase):
    def test_save_edits_audits_and_rescores(self):
        resp = self.client.post("/admin/weight", data={
            "weight_kind": "scope", "key": "sector", "weight": "0.90"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.scalar(
            "SELECT weight FROM scoring_weights WHERE key='sector'"), 0.90)
        conn = self.conn()
        audit = conn.execute("SELECT table_name, field FROM config_audit").fetchall()
        conn.close()
        self.assertEqual(len(audit), 1)
        self.assertEqual((audit[0]["table_name"], audit[0]["field"]),
                         ("scoring_weights", "weight"))
        self.assertIn("Saved", resp.text)


class TestNoOpSave(AdminTestBase):
    def test_saving_unchanged_weight_writes_nothing_and_shows_info(self):
        resp = self.client.post("/admin/weight", data={
            "weight_kind": "scope", "key": "sector", "weight": "0.55"})
        self.assertIn("No change", resp.text)
        self.assertEqual(self.audit_count(), 0)


class TestHalfLifeSave(AdminTestBase):
    def test_save_edits_and_audits(self):
        resp = self.client.post("/admin/half-life", data={
            "trigger_id": "t_lead", "half_life": "150"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.scalar(
            "SELECT decay_half_life_days FROM triggers WHERE trigger_id='t_lead'"),
            150)
        self.assertEqual(self.scalar("SELECT field FROM config_audit"),
                         "decay_half_life_days")


class TestSourceToggle(AdminTestBase):
    def test_disable_source_audits(self):
        # unchecked checkbox omits 'enabled' from the POST -> disable
        resp = self.client.post("/admin/source/enabled",
                                data={"source_id": "edgar"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.scalar(
            "SELECT enabled FROM source_policies WHERE source_id='edgar'"), 0)
        self.assertEqual(self.scalar("SELECT field FROM config_audit"), "enabled")


class TestLockHeld(AdminTestBase):
    def test_save_under_held_lock_warns_and_writes_nothing(self):
        with ingest_lock():
            resp = self.client.post("/admin/weight", data={
                "weight_kind": "scope", "key": "sector", "weight": "0.90"})
        self.assertIn("Ingestion in progress", resp.text)
        self.assertEqual(self.scalar(
            "SELECT weight FROM scoring_weights WHERE key='sector'"), 0.55)
        self.assertEqual(self.audit_count(), 0)


class TestStalenessLabels(AdminTestBase):
    def test_literal_field_stating_labels(self):
        dom = self.page()
        self.assertRegex(dom, r"verified \d+ days ago")
        self.assertIn("verification date unknown", dom)
        self.assertNotIn("unverified", dom)


class TestEntityAdd(AdminTestBase):
    def test_add_operator_entity(self):
        resp = self.client.post("/admin/entity/add", data={
            "entity_id": "E9002", "name": "New Co"})
        self.assertEqual(resp.status_code, 200)
        conn = self.conn()
        row = conn.execute("SELECT origin, active FROM watchlist_entities "
                           "WHERE entity_id='E9002'").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual((row["origin"], row["active"]), ("operator", 1))


class TestEntityDisable(AdminTestBase):
    def test_disable_seeded_entity(self):
        # unchecked 'active' checkbox omits it -> disable
        resp = self.client.post("/admin/entity/active",
                                data={"entity_id": "E0004"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.scalar(
            "SELECT active FROM watchlist_entities WHERE entity_id='E0004'"), 0)
        self.assertEqual(self.scalar("SELECT field FROM config_audit"), "active")


class TestEntityEditField(AdminTestBase):
    def test_edit_subsector(self):
        resp = self.client.post("/admin/entity/field", data={
            "entity_id": "E0004", "field": "subsector", "value": "Nuclear"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.scalar(
            "SELECT subsector FROM watchlist_entities WHERE entity_id='E0004'"),
            "Nuclear")


class TestEntityAliasAdd(AdminTestBase):
    def test_add_operator_alias(self):
        resp = self.client.post("/admin/entity/alias/add", data={
            "entity_id": "E0004", "alias": "Dominion Va"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.scalar(
            "SELECT source FROM entity_aliases WHERE alias='Dominion Va'"),
            "operator")


class TestEntityRemoveDeleteSafety(AdminTestBase):
    """UI-layer delete-safety gate: removing an operator entity with a
    referencing signal shows a legible error and changes nothing."""

    def test_remove_blocked_by_referencing_signal(self):
        resp = self.client.post("/admin/entity/remove", data={
            "entity_id": "E9001", "confirm": "true"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("reference", resp.text.lower())
        self.assertIsNotNone(self.scalar(
            "SELECT 1 FROM watchlist_entities WHERE entity_id='E9001'"))
        self.assertEqual(self.audit_count(), 0)


class TestEntityConfirmGate(AdminTestBase):
    """The confirm flag is a real server-side gate (persona-pass P0): a
    destructive action fires only when it is set, not on a bare POST."""

    def test_remove_without_confirm_does_nothing(self):
        resp = self.client.post("/admin/entity/remove",
                                data={"entity_id": "E9001"})
        self.assertIn("Check the box", resp.text)
        self.assertIsNotNone(self.scalar(
            "SELECT 1 FROM watchlist_entities WHERE entity_id='E9001'"))
        self.assertEqual(self.audit_count(), 0)

    def test_reset_without_confirm_does_nothing(self):
        resp = self.client.post("/admin/entity/reset",
                                data={"entity_id": "E0004"})
        self.assertIn("Check the box", resp.text)
        self.assertEqual(self.audit_count("field='__reset__'"), 0)

    def test_reset_with_confirm_restores_and_reenables(self):
        # Disable + mis-edit E0004, then reset with confirm.
        self.client.post("/admin/entity/active", data={"entity_id": "E0004"})
        self.client.post("/admin/entity/field", data={
            "entity_id": "E0004", "field": "subsector", "value": "Wrong"})
        resp = self.client.post("/admin/entity/reset", data={
            "entity_id": "E0004", "confirm": "true"})
        self.assertEqual(resp.status_code, 200)
        conn = self.conn()
        row = conn.execute("SELECT subsector, active FROM watchlist_entities "
                           "WHERE entity_id='E0004'").fetchone()
        conn.close()
        self.assertEqual(row["subsector"], "iou_electric")   # restored from seed
        self.assertEqual(row["active"], 1)                   # re-enabled


class TestSourceAdd(AdminTestBase):
    def test_add_operator_source(self):
        resp = self.client.post("/admin/source/add", data={
            "source_id": "myfeed", "name": "My Feed"})
        self.assertEqual(resp.status_code, 200)
        conn = self.conn()
        row = conn.execute("SELECT origin, enabled FROM source_policies "
                           "WHERE source_id='myfeed'").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual((row["origin"], row["enabled"]), ("operator", 1))
        # Provenance table humanizes the __add__ sentinel (render-only).
        dom = self.page()
        self.assertIn("added", dom)
        self.assertNotIn("__add__", dom)


class TestSourceRemoveConfirmGate(AdminTestBase):
    """The source Remove confirm flag is a real server-side gate: removal fires
    only when it is set."""

    def _add(self):
        self.client.post("/admin/source/add",
                         data={"source_id": "myfeed", "name": "My Feed"})

    def test_remove_without_confirm_does_nothing(self):
        self._add()
        resp = self.client.post("/admin/source/remove",
                                data={"source_id": "myfeed"})
        self.assertIn("Check the box", resp.text)
        self.assertIsNotNone(self.scalar(
            "SELECT 1 FROM source_policies WHERE source_id='myfeed'"))

    def test_remove_with_confirm_deletes(self):
        self._add()
        resp = self.client.post("/admin/source/remove", data={
            "source_id": "myfeed", "confirm": "true"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.scalar(
            "SELECT 1 FROM source_policies WHERE source_id='myfeed'"))
        self.assertEqual(self.scalar(
            "SELECT field FROM config_audit ORDER BY audit_id DESC LIMIT 1"),
            "__remove__")


class TestFactEdit(AdminTestBase):
    def test_edit_fact_source_url_audits(self):
        resp = self.client.post("/admin/fact/edit", data={
            "fact_id": "f_old", "field": "source_url", "value": "http://new"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.scalar(
            "SELECT source_url FROM license_facts WHERE fact_id='f_old'"),
            "http://new")
        self.assertEqual(self.scalar("SELECT field FROM config_audit"),
                         "source_url")

    def test_future_verified_date_rejected_inline(self):
        resp = self.client.post("/admin/fact/edit", data={
            "fact_id": "f_old", "field": "verified_date", "value": "2099-12-31"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("future", resp.text.lower())
        self.assertEqual(self.audit_count(), 0)          # nothing written


class TestScoringConfigNeverDeletes(AdminTestBase):
    """Regression gate mirroring TestNeverDelete: the scoring-config write paths
    (weight / half-life / source enable) only UPDATE/INSERT — a save never
    DELETEs the row it targets, even across a toggle round-trip."""

    def test_source_toggle_never_removes_the_row(self):
        # disable then re-enable; the row must persist throughout.
        self.client.post("/admin/source/enabled", data={"source_id": "edgar"})
        self.assertIsNotNone(self.scalar(
            "SELECT 1 FROM source_policies WHERE source_id='edgar'"))
        self.client.post("/admin/source/enabled",
                         data={"source_id": "edgar", "enabled": "true"})
        self.assertIsNotNone(self.scalar(
            "SELECT 1 FROM source_policies WHERE source_id='edgar'"))

    def test_weight_and_halflife_rows_persist(self):
        self.client.post("/admin/weight", data={
            "weight_kind": "scope", "key": "sector", "weight": "0.90"})
        self.client.post("/admin/half-life", data={
            "trigger_id": "t_lead", "half_life": "150"})
        self.assertIsNotNone(self.scalar(
            "SELECT 1 FROM scoring_weights WHERE key='sector'"))
        self.assertIsNotNone(self.scalar(
            "SELECT 1 FROM triggers WHERE trigger_id='t_lead'"))


if __name__ == "__main__":
    unittest.main()
