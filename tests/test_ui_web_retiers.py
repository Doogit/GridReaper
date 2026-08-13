"""Central "recent re-tiers" audit panel tests (R8.7 oversight; R7.12, R10.5).

Three hermetic layers, no network:
  * data.recent_retiers — the aggregate reader, in-memory SQLite via
    apply_migrations: newest-first ordering, the gate-raised flag, a
    sector-scoped incident's NULL account, headline join, and the limit.
  * render.recent_retiers_view — pure display shaping of the outreach
    consequence and the em-dash account.
  * GET /retiers — the HTTP surface: honest empty state, the nav link, and a
    real re-tier (driven through POST /incident/tier) surfacing on the panel.
"""
import os
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.db.migrate import apply_migrations
from app.ui import data
from app.ui_web import render
from app.ui_web.app import app


def _seed_config(conn):
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('own_incident','Own incident',6,120)")
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('peer_incident','Peer incident',5,120)")
    conn.execute("INSERT INTO watchlist_entities (entity_id, name, active) "
                 "VALUES ('E_ACME','Acme Energy',1)")
    conn.execute("INSERT INTO source_policies (source_id, name, enabled, ttl, "
                 "access_method, evidence_rank) VALUES ('sp_ok','OK',1,3600,'rss',2)")
    conn.execute("INSERT INTO raw_events (raw_event_id, source_id, event_date, "
                 "payload, url) VALUES ('re1','sp_ok','2026-08-01','{}','http://x/1')")


def _add_signal(conn, sid, trigger_id, scope, entity_id, headline, cfa,
                incident_level):
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, signal_scope, "
        "trigger_id, event_date, headline, evidence_snippet, source_url, "
        "confidence, evidence_quality, incident_evidence_level, "
        "customer_facing_allowed, score, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, "re1", entity_id, scope, trigger_id, "2026-08-01", headline,
         headline, "http://x/1", 0.9, "IR", incident_level, cfa, 3.0, "active"))


def _add_edit(conn, signal_id, old_level, new_level, old_cfa, new_cfa,
              editor="operator", reason="", ts="2026-08-10T00:00:00+00:00"):
    conn.execute(
        "INSERT INTO incident_tier_edits (signal_id, old_level, new_level, "
        "old_cfa, new_cfa, editor, reason, ts) VALUES (?,?,?,?,?,?,?,?)",
        (signal_id, old_level, new_level, old_cfa, new_cfa, editor, reason, ts))


# -- data.recent_retiers (in-memory reader) ----------------------------------

class TestRecentRetiersReader(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        apply_migrations(self.conn)
        _seed_config(self.conn)
        _add_signal(self.conn, "S_OWN", "own_incident", "account", "E_ACME",
                    "Acme discloses incident", cfa=1, incident_level="confirmed")
        _add_signal(self.conn, "S_PEER", "peer_incident", "sector", None,
                    "A utility named on leak site", cfa=0,
                    incident_level="unconfirmed_early_warning")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_empty_when_no_edits(self):
        self.assertEqual(data.recent_retiers(self.conn), [])

    def test_newest_first_by_edit_id(self):
        _add_edit(self.conn, "S_OWN", "corroborated", "confirmed", 1, 1,
                  ts="2026-08-05T00:00:00+00:00")
        _add_edit(self.conn, "S_PEER", "unconfirmed_early_warning",
                  "corroborated", 0, 1, reason="press corroboration",
                  ts="2026-08-04T00:00:00+00:00")
        self.conn.commit()
        rows = data.recent_retiers(self.conn)
        # append order, not ts: the second insert is the newest edit_id
        self.assertEqual([r["signal_id"] for r in rows], ["S_PEER", "S_OWN"])

    def test_gate_raised_flag(self):
        _add_edit(self.conn, "S_PEER", "unconfirmed_early_warning", "confirmed",
                  0, 1, reason="confirmed by filing")   # clears outreach
        _add_edit(self.conn, "S_OWN", "confirmed", "unconfirmed_early_warning",
                  1, 0)                                   # suppresses outreach
        _add_edit(self.conn, "S_OWN", "confirmed", "corroborated", 1, 1)  # lateral
        self.conn.commit()
        by_id = {(r["signal_id"], r["new_level"]): r
                 for r in data.recent_retiers(self.conn)}
        self.assertTrue(by_id[("S_PEER", "confirmed")]["gate_raised"])
        self.assertFalse(
            by_id[("S_OWN", "unconfirmed_early_warning")]["gate_raised"])
        self.assertFalse(by_id[("S_OWN", "corroborated")]["gate_raised"])

    def test_joins_headline_and_account(self):
        _add_edit(self.conn, "S_OWN", "corroborated", "confirmed", 1, 1)
        self.conn.commit()
        row = data.recent_retiers(self.conn)[0]
        self.assertEqual(row["headline"], "Acme discloses incident")
        self.assertEqual(row["entity_name"], "Acme Energy")
        self.assertEqual(row["new_level"], "confirmed")

    def test_sector_incident_has_null_account(self):
        _add_edit(self.conn, "S_PEER", "unconfirmed_early_warning",
                  "corroborated", 0, 1, reason="two-source corroboration")
        self.conn.commit()
        row = data.recent_retiers(self.conn)[0]
        self.assertIsNone(row["entity_name"])
        self.assertEqual(row["headline"], "A utility named on leak site")

    def test_limit_caps_rows(self):
        for i in range(5):
            _add_edit(self.conn, "S_OWN", "confirmed", "corroborated", 1, 1,
                      ts=f"2026-08-1{i}T00:00:00+00:00")
        self.conn.commit()
        self.assertEqual(len(data.recent_retiers(self.conn, limit=3)), 3)


# -- render.recent_retiers_view (pure) ---------------------------------------

class TestRecentRetiersView(unittest.TestCase):
    def _row(self, **over):
        base = {"edit_id": 1, "signal_id": "S", "old_level": "confirmed",
                "new_level": "confirmed", "old_cfa": 1, "new_cfa": 1,
                "editor": "operator", "reason": "", "ts": "2026-08-10T00:00:00+00:00",
                "headline": "H", "signal_scope": "account", "entity_id": "E",
                "current_level": "confirmed", "entity_name": "Acme Energy",
                "gate_raised": False}
        base.update(over)
        return base

    def test_gate_raise_reads_cleared(self):
        v = render.recent_retiers_view([self._row(
            old_cfa=0, new_cfa=1, gate_raised=True,
            old_level="unconfirmed_early_warning", new_level="confirmed")])[0]
        self.assertEqual(v["outreach"], "cleared for outreach")
        self.assertTrue(v["gate_raised"])
        self.assertEqual(v["change"], "unconfirmed_early_warning → confirmed")

    def test_suppress_reads_suppressed(self):
        v = render.recent_retiers_view([self._row(old_cfa=1, new_cfa=0)])[0]
        self.assertEqual(v["outreach"], "suppressed")

    def test_lateral_reads_dash(self):
        v = render.recent_retiers_view([self._row(old_cfa=1, new_cfa=1)])[0]
        self.assertEqual(v["outreach"], "—")

    def test_null_account_reads_dash(self):
        v = render.recent_retiers_view([self._row(entity_name=None)])[0]
        self.assertEqual(v["account"], "—")


# -- GET /retiers (HTTP surface) ---------------------------------------------

def _make_db(seed_fn):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    apply_migrations(conn)
    seed_fn(conn)
    conn.commit()
    conn.close()
    return path


class RetiersRouteBase(unittest.TestCase):
    seed_fn = None

    def setUp(self):
        self.path = _make_db(type(self).seed_fn)
        os.environ["GRIDSIGNALS_DB"] = self.path
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("GRIDSIGNALS_DB", None)
        try:
            os.remove(self.path)
        except OSError:
            pass


def _seed_unconfirmed(conn):
    _seed_config(conn)
    _add_signal(conn, "S_UNCONF", "own_incident", "account", "E_ACME",
                "Acme named on leak site", cfa=0,
                incident_level="unconfirmed_early_warning")


class TestRetiersRoute(RetiersRouteBase):
    seed_fn = staticmethod(_seed_unconfirmed)

    def test_empty_state_is_honest(self):
        resp = self.client.get("/retiers")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("No re-tiers yet", resp.text)

    def test_nav_link_present_and_active(self):
        dom = self.client.get("/retiers").text
        self.assertIn('href="/retiers"', dom)
        self.assertIn('class="active">Re-tiers', dom)

    def test_real_retier_surfaces_on_panel(self):
        # drive the actual write path, then read it back through the panel
        self.client.post("/incident/tier", data={
            "signal_id": "S_UNCONF", "new_level": "confirmed",
            "reason": "confirmed via SEC 8-K Item 1.05"})
        dom = self.client.get("/retiers").text
        self.assertIn("Acme named on leak site", dom)
        self.assertIn("confirmed via SEC 8-K Item 1.05", dom)
        self.assertIn("cleared for outreach", dom)
        self.assertIn("unconfirmed_early_warning → confirmed", dom)


if __name__ == "__main__":
    unittest.main()
