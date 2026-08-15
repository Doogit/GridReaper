"""Incident evidence-tier editor tests for the FastAPI web UI (R8.7; R10.5,
R7.12, R3.2, R3.3).

Hermetic: temp-file SQLite via apply_migrations, pointed at the app through
GRIDSIGNALS_DB, no network. Covers the HTTP surface: revealing the panel, a
re-tier that flips the tier + recomputes customer_facing_allowed both
directions, the append-only incident_tier_edits trail, the no-op save, the
non-incident / unknown-tier rejections, and a held single-writer lock.
"""
import os
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.db.migrate import apply_migrations
from app.ingest.runner import ingest_lock
from app.ui_web.app import app
from app.ui_web.render import tier_dom_id
from tests.lock_fixture import redirect_ingest_lock


def _seed(conn):
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('own_incident','Own incident',6,120)")
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_lead','Leadership',4,90)")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, active) "
        "VALUES ('E_ACME','Acme Energy',1)")
    conn.execute(
        "INSERT INTO source_policies (source_id, name, enabled, ttl, "
        "access_method, evidence_rank) VALUES ('sp_ok','OK',1,3600,'rss',2)")
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, payload, url) "
        "VALUES ('re1','sp_ok','2026-08-01','{}','http://x/1')")
    _add_signal(conn, "S_UNCONF", "own_incident", "account", "E_ACME",
                "Acme named on leak site", cfa=0,
                incident_level="unconfirmed_early_warning")
    _add_signal(conn, "S_CONF", "own_incident", "account", "E_ACME",
                "Acme discloses 8-K Item 1.05", cfa=1,
                incident_level="confirmed")
    _add_signal(conn, "S_PLAIN", "t_lead", "account", "E_ACME",
                "Acme names new CISO", cfa=1, incident_level=None)


def _add_signal(conn, sid, trigger_id, scope, entity_id, headline,
                cfa=0, incident_level=None):
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, signal_scope, "
        "trigger_id, event_date, headline, evidence_snippet, source_url, "
        "confidence, evidence_quality, incident_evidence_level, "
        "customer_facing_allowed, score, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, "re1", entity_id, scope, trigger_id, "2026-08-01", headline,
         headline, "http://x/1", 0.9, "IR", incident_level, cfa, 3.0, "active"))


def _make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    apply_migrations(conn)
    _seed(conn)
    conn.commit()
    conn.close()
    return path


class IncidentTierBase(unittest.TestCase):
    def setUp(self):
        self.path = _make_db()
        os.environ["GRIDSIGNALS_DB"] = self.path
        self.lock_path = redirect_ingest_lock(self)
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("GRIDSIGNALS_DB", None)
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def scalar(self, sql, params=()):
        conn = self._conn()
        try:
            row = conn.execute(sql, params).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def edit_count(self, signal_id=None):
        if signal_id:
            return self.scalar(
                "SELECT COUNT(*) FROM incident_tier_edits WHERE signal_id=?",
                (signal_id,))
        return self.scalar("SELECT COUNT(*) FROM incident_tier_edits")


class TestReveal(IncidentTierBase):
    def test_panel_shows_current_tier_and_options(self):
        resp = self.client.get("/incident/tier", params={"signal_id": "S_UNCONF"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('name="new_level"', resp.text)
        # current tier is pre-selected
        self.assertIn('value="unconfirmed_early_warning" selected', resp.text)
        # all three tiers are offered
        self.assertIn('value="confirmed"', resp.text)
        self.assertIn('value="corroborated"', resp.text)
        # the field must not imply "optional": up-tiering from an unconfirmed
        # card requires a reason because it clears customer-facing outreach.
        self.assertIn('placeholder="reason / source"', resp.text)
        self.assertNotIn("reason (optional)", resp.text)

    def test_reveal_button_uses_css_safe_target(self):
        dom = self.client.get("/").text
        # the incident card exposes a hashed tier target, never the raw id
        self.assertIn(tier_dom_id("S_UNCONF"), dom)

    def test_non_incident_card_has_no_editor(self):
        dom = self.client.get("/").text
        self.assertNotIn(tier_dom_id("S_PLAIN"), dom)

    def test_confirmed_card_shows_cleared_marker(self):
        # a confirmed incident carries a durable "cleared for outreach" marker
        # even without a play snapshot, so the tier state is legible at rest.
        dom = self.client.get("/", params={"status": "all"}).text
        self.assertIn("Confirmed incident — cleared for customer-facing outreach",
                      dom)

    def test_lateral_move_needs_no_reason(self):
        # a move between two customer-facing tiers (no cfa change) is not the
        # outreach-unblocking transition, so no reason is required.
        self.client.post("/incident/tier", data={
            "signal_id": "S_CONF", "new_level": "corroborated"})
        self.assertEqual(self.scalar(
            "SELECT incident_evidence_level FROM signals WHERE signal_id='S_CONF'"),
            "corroborated")
        self.assertEqual(self.edit_count("S_CONF"), 1)

    def test_reveal_rejects_non_incident_signal(self):
        resp = self.client.get("/incident/tier", params={"signal_id": "S_PLAIN"})
        self.assertEqual(resp.text.strip(), "")


class TestRetier(IncidentTierBase):
    def test_unconfirmed_to_confirmed_flips_tier_and_cfa(self):
        resp = self.client.post("/incident/tier", data={
            "signal_id": "S_UNCONF", "new_level": "confirmed",
            "reason": "confirmed via SEC 8-K Item 1.05"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.scalar(
            "SELECT incident_evidence_level FROM signals WHERE signal_id='S_UNCONF'"),
            "confirmed")
        self.assertEqual(self.scalar(
            "SELECT customer_facing_allowed FROM signals WHERE signal_id='S_UNCONF'"),
            1)
        self.assertEqual(self.edit_count("S_UNCONF"), 1)
        # the card no longer reads as an unverified early warning
        self.assertNotIn("No company, regulator, or SEC confirmation yet", resp.text)
        self.assertNotIn("tier-unconfirmed", resp.text)
        self.assertIn("outreach now allowed", resp.text)
        # the up-tiered state leaves a durable positive marker on the card
        self.assertIn("cleared for customer-facing outreach", resp.text)

    def test_uptier_requires_reason(self):
        # promoting to a customer-facing tier is the one transition that clears
        # a card for outreach — it must carry a justification (R4.1 provenance).
        resp = self.client.post("/incident/tier", data={
            "signal_id": "S_UNCONF", "new_level": "confirmed"})
        self.assertIn("a reason is required", resp.text)
        self.assertEqual(self.scalar(
            "SELECT incident_evidence_level FROM signals WHERE signal_id='S_UNCONF'"),
            "unconfirmed_early_warning")
        self.assertEqual(self.edit_count(), 0)

    def test_confirmed_to_unconfirmed_suppresses_outreach(self):
        resp = self.client.post("/incident/tier", data={
            "signal_id": "S_CONF", "new_level": "unconfirmed_early_warning"})
        self.assertEqual(self.scalar(
            "SELECT customer_facing_allowed FROM signals WHERE signal_id='S_CONF'"),
            0)
        self.assertIn("No company, regulator, or SEC confirmation yet", resp.text)
        self.assertIn("Outreach withheld", resp.text)
        self.assertIn("outreach now suppressed", resp.text)

    def test_reason_is_recorded(self):
        self.client.post("/incident/tier", data={
            "signal_id": "S_UNCONF", "new_level": "corroborated",
            "reason": "matches vendor advisory"})
        self.assertEqual(self.scalar(
            "SELECT reason FROM incident_tier_edits WHERE signal_id='S_UNCONF'"),
            "matches vendor advisory")


class TestAppendOnlyTrail(IncidentTierBase):
    def test_successive_retiers_append_never_mutate(self):
        self.client.post("/incident/tier", data={
            "signal_id": "S_UNCONF", "new_level": "corroborated",
            "reason": "vendor advisory corroborates"})
        self.client.post("/incident/tier", data={
            "signal_id": "S_UNCONF", "new_level": "confirmed"})
        self.assertEqual(self.edit_count("S_UNCONF"), 2)
        # the trail chains old->new and reads newest-first
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT old_level, new_level FROM incident_tier_edits "
                "WHERE signal_id='S_UNCONF' ORDER BY edit_id").fetchall()
        finally:
            conn.close()
        self.assertEqual(
            [(r["old_level"], r["new_level"]) for r in rows],
            [("unconfirmed_early_warning", "corroborated"),
             ("corroborated", "confirmed")])

    def test_history_reads_newest_first(self):
        self.client.post("/incident/tier", data={
            "signal_id": "S_UNCONF", "new_level": "corroborated",
            "reason": "vendor advisory corroborates"})
        resp = self.client.post("/incident/tier", data={
            "signal_id": "S_UNCONF", "new_level": "confirmed"})
        # the re-rendered card carries the trail, most recent transition first
        self.assertLess(
            resp.text.index("corroborated → confirmed"),
            resp.text.index("unconfirmed_early_warning → corroborated"))


class TestGuards(IncidentTierBase):
    def test_noop_writes_nothing(self):
        resp = self.client.post("/incident/tier", data={
            "signal_id": "S_CONF", "new_level": "confirmed"})
        self.assertIn("No change — tier already confirmed", resp.text)
        self.assertEqual(self.edit_count("S_CONF"), 0)
        self.assertEqual(self.scalar(
            "SELECT customer_facing_allowed FROM signals WHERE signal_id='S_CONF'"),
            1)

    def test_non_incident_signal_rejected(self):
        resp = self.client.post("/incident/tier", data={
            "signal_id": "S_PLAIN", "new_level": "confirmed"})
        self.assertIn("not an incident", resp.text)
        self.assertIsNone(self.scalar(
            "SELECT incident_evidence_level FROM signals WHERE signal_id='S_PLAIN'"))
        self.assertEqual(self.edit_count(), 0)

    def test_unknown_tier_rejected(self):
        resp = self.client.post("/incident/tier", data={
            "signal_id": "S_UNCONF", "new_level": "bogus"})
        self.assertIn("unknown incident tier", resp.text)
        self.assertEqual(self.scalar(
            "SELECT incident_evidence_level FROM signals WHERE signal_id='S_UNCONF'"),
            "unconfirmed_early_warning")
        self.assertEqual(self.edit_count(), 0)

    def test_held_lock_warns_and_writes_nothing(self):
        with ingest_lock(self.lock_path):
            resp = self.client.post("/incident/tier", data={
                "signal_id": "S_UNCONF", "new_level": "confirmed"})
        self.assertIn("Ingestion in progress", resp.text)
        self.assertEqual(self.scalar(
            "SELECT incident_evidence_level FROM signals WHERE signal_id='S_UNCONF'"),
            "unconfirmed_early_warning")
        self.assertEqual(self.edit_count(), 0)


if __name__ == "__main__":
    unittest.main()
