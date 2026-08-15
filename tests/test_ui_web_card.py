"""Per-card permalink tests (R8.1 card addressability).

Three hermetic layers, no network:
  * render.card_key — the pure URL-key helper: deterministic, 16 hex chars,
    URL-shaped signal_ids handled (never used raw in a path), matches card_view.
  * GET /card/{card_key} — the HTTP surface: a known signal renders the full
    card (headline + its feedback/re-tier affordances, agent-native parity), an
    unknown key is an honest 404, and the key round-trips from a feed card.
  * the re-tiers panel links each row back to its card via the permalink.
"""
import os
import re
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.db.migrate import apply_migrations
from app.ui import data
from app.ui_web import render
from app.ui_web.app import app
from tests.lock_fixture import redirect_ingest_lock

# A signal_id whose raw_event_id is itself a URL — must never land raw in a path.
URLISH_SIGNAL_ID = "peer_incident:the_record:https://example.com/a?x=1&y=2:sector"


def _seed(conn):
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('own_incident','Own incident',6,120)")
    conn.execute("INSERT INTO watchlist_entities (entity_id, name, active) "
                 "VALUES ('E_ACME','Acme Energy',1)")
    conn.execute("INSERT INTO source_policies (source_id, name, enabled, ttl, "
                 "access_method, evidence_rank) VALUES ('sp_ok','OK',1,3600,'rss',2)")
    conn.execute("INSERT INTO raw_events (raw_event_id, source_id, event_date, "
                 "payload, url) VALUES ('re1','sp_ok','2026-08-01','{}','http://x/1')")
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, signal_scope, "
        "trigger_id, event_date, headline, evidence_snippet, source_url, "
        "confidence, evidence_quality, incident_evidence_level, "
        "customer_facing_allowed, score, status) "
        "VALUES ('S_OWN','re1','E_ACME','account','own_incident','2026-08-01',"
        "'Acme discloses incident','Acme discloses incident','http://x/1',0.9,"
        "'IR','unconfirmed_early_warning',0,3.0,'active')")


# -- render.card_key (pure) --------------------------------------------------

class TestCardKey(unittest.TestCase):
    def test_deterministic_16_hex(self):
        k = render.card_key("S_OWN")
        self.assertEqual(k, render.card_key("S_OWN"))
        self.assertEqual(len(k), 16)
        self.assertRegex(k, r"^[0-9a-f]{16}$")

    def test_distinct_ids_distinct_keys(self):
        self.assertNotEqual(render.card_key("S_OWN"), render.card_key("S_PEER"))

    def test_urlish_signal_id_never_appears_raw(self):
        # the whole point: a URL-shaped id is hashed, not embedded in the path.
        k = render.card_key(URLISH_SIGNAL_ID)
        self.assertRegex(k, r"^[0-9a-f]{16}$")
        self.assertNotIn("http", k)
        self.assertNotIn("/", k)


# -- GET /card/{card_key} + re-tiers link (HTTP surface) ---------------------

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


class CardRouteBase(unittest.TestCase):
    def setUp(self):
        self.path = _make_db(_seed)
        os.environ["GRIDSIGNALS_DB"] = self.path
        self.lock_path = redirect_ingest_lock(self)
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("GRIDSIGNALS_DB", None)
        try:
            os.remove(self.path)
        except OSError:
            pass


class TestCardRoute(CardRouteBase):
    def test_known_key_renders_full_card(self):
        key = render.card_key("S_OWN")
        resp = self.client.get(f"/card/{key}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Acme discloses incident", resp.text)
        # the article carries its in-page anchor id
        self.assertIn(f'id="card-{key}"', resp.text)
        # agent-native parity: the re-tier + feedback affordances render here too
        self.assertIn("/incident/tier", resp.text)
        self.assertIn("/feedback", resp.text)

    def test_unknown_key_is_honest_404(self):
        resp = self.client.get("/card/deadbeefdeadbeef")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("No card at this link", resp.text)

    def test_feed_permalink_round_trips_to_card(self):
        # the feed card exposes /card/{key}; following it lands on the same card
        home = self.client.get("/").text
        m = re.search(r'href="(/card/[0-9a-f]{16})"', home)
        self.assertIsNotNone(m, "feed card is missing its permalink link")
        resp = self.client.get(m.group(1))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Acme discloses incident", resp.text)

    def test_retiers_row_links_to_card(self):
        # drive a real re-tier, then the audit row's signal links to its card
        self.client.post("/incident/tier", data={
            "signal_id": "S_OWN", "new_level": "confirmed",
            "reason": "confirmed via SEC 8-K Item 1.05"})
        dom = self.client.get("/retiers").text
        key = render.card_key("S_OWN")
        self.assertIn(f'href="/card/{key}"', dom)


if __name__ == "__main__":
    unittest.main()
