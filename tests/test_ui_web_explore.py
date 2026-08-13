"""Explore page tests for the FastAPI web UI (U10, R8.5, R4.1, R6.6).

TestClient port over a temp-file SQLite pointed at the app via GRIDSIGNALS_DB
(same hermetic pattern as the other web-route suites; no network). Covers: GET
/explore is 200 with Analytics counts present and the Map tab carrying an inline
<svg>; the nav link is present + active; and — on an empty DB — BOTH tabs show
their own honest empty-state copy (R6.6).
"""
import os
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.db.migrate import apply_migrations
from app.ui_web.app import app


def seed(conn):
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_lead','Leadership',4,90)")
    conn.execute("INSERT INTO watchlist_entities (entity_id, name, subsector) "
                 "VALUES ('E_ACME','Acme Energy','iou_electric')")
    conn.execute("INSERT INTO source_policies (source_id, name, enabled, ttl, "
                 "access_method, evidence_rank) VALUES "
                 "('sp_ok','OK Source',1,3600,'rss',2)")
    conn.execute("INSERT INTO raw_events (raw_event_id, source_id, event_date, "
                 "payload, url) VALUES ('re1','sp_ok','2026-08-01','{}',"
                 "'http://acme/8k')")
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, signal_scope, "
        "trigger_id, event_date, headline, evidence_snippet, source_url, "
        "confidence, evidence_quality, customer_facing_allowed, score, status) "
        "VALUES ('S1','re1','E_ACME','account','t_lead','2026-08-01',"
        "'Acme names new CISO','ev','http://acme/8k',0.9,'IR',1,3.0,'active')")
    # A gated (0.9) TX facility -> the Map tab plots a real point.
    conn.execute(
        "INSERT INTO facility_assets (facility_id, source_id, facility_name, "
        "latitude, longitude, owner_operator_entity_id, "
        "facility_owner_confidence) VALUES "
        "('F1','sp_ok','Austin Plant', 30.3, -97.7, 'E_ACME', 0.9)")


def make_db(with_data=True):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    apply_migrations(conn)
    if with_data:
        seed(conn)
    conn.commit()
    conn.close()
    return path


class ExploreTestBase(unittest.TestCase):
    WITH_DATA = True

    def setUp(self):
        self.path = make_db(self.WITH_DATA)
        os.environ["GRIDSIGNALS_DB"] = self.path
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("GRIDSIGNALS_DB", None)
        try:
            os.remove(self.path)
        except OSError:
            pass


class TestExplorePage(ExploreTestBase):
    def test_get_200_and_default_analytics_tab(self):
        resp = self.client.get("/explore")
        self.assertEqual(resp.status_code, 200)
        dom = resp.text
        # Analytics is the default tab (its input is checked, D4).
        self.assertIn('id="gs-tab-analytics" class="gs-tab-input" checked', dom)

    def test_analytics_counts_present(self):
        dom = self.client.get("/explore").text
        self.assertIn("Trigger Analytics", dom)
        self.assertIn("Leadership", dom)       # trigger name for the seeded signal
        self.assertIn("Signal scope", dom)      # scope table header

    def test_map_tab_contains_inline_svg_with_facility(self):
        dom = self.client.get("/explore").text
        self.assertIn("<svg", dom)              # inline SVG, no external map JS
        self.assertIn("Watchlist Map", dom)
        self.assertIn("<circle", dom)           # the gated TX facility renders
        self.assertIn("Acme Energy", dom)        # its <title> identity

    def test_map_deeplink_opens_map_tab(self):
        dom = self.client.get("/explore?tab=map").text
        self.assertIn('id="gs-tab-map" class="gs-tab-input" checked', dom)

    def test_nav_link_present_and_active(self):
        dom = self.client.get("/explore").text
        self.assertIn('href="/explore"', dom)
        # nav_active drives the active class on the Explore link.
        self.assertIn('href="/explore" class="active"', dom)

    def test_no_external_map_dependency(self):
        # Evidence-safe + self-contained: no tile server / mapping CDN fetch.
        dom = self.client.get("/explore").text
        for needle in ("leaflet", "mapbox", "openstreetmap", "tile.", "d3js"):
            self.assertNotIn(needle, dom.lower())


class TestExploreEmptyStates(ExploreTestBase):
    WITH_DATA = False

    def test_both_tabs_honest_empty_copy(self):
        dom = self.client.get("/explore").text
        self.assertEqual(self.client.get("/explore").status_code, 200)
        # Analytics tab has its OWN empty copy (D4)...
        self.assertIn("No signals yet", dom)
        # ...and the Map tab has the honest no-facility-evidence note (R6.6).
        self.assertIn("No facility-level evidence yet", dom)
        # The base geography still renders even with no data.
        self.assertIn("<svg", dom)
        self.assertNotIn("<circle", dom)


if __name__ == "__main__":
    unittest.main()
