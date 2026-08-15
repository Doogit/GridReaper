"""Per-card permalink tests (R8.1 card addressability).

Three hermetic layers, no network:
  * render.card_key — the pure URL-key helper: deterministic, 16 hex chars,
    URL-shaped signal_ids handled (never used raw in a path), matches card_view.
  * GET /card/{card_key} — the HTTP surface: a known signal renders the full
    card (headline + its feedback/re-tier affordances, agent-native parity), an
    unknown key is an honest 404, and the key round-trips from a feed card.
  * the re-tiers panel links each row back to its card via the permalink.

TestProvenanceLine covers the R3.7 provenance block and its mandatory R4.1 gate:
a raw_event_id embeds the source article URL on this corpus, so a sector-scoped
card renders a sha256 digest of it while an account card renders the value —
asserted on /card/, on the feed, and on the generated digest.html artifact.
"""
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import digest as digest_mod
from app.db.migrate import apply_migrations
from app.ui import data
from app.ui_web import render
from app.ui_web.app import app

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


# -- R3.7 provenance line + its mandatory R4.1 privacy gate ------------------

NOW = datetime(2026, 8, 15, 9, 0, 0, tzinfo=timezone.utc)

# The real shape: raw_event_id = "{source_id}:{native_id}", and the RSS
# classifier's native_id is the item guid — the article link, slug and all.
VICTIM_NAME = "Trezor"
VICTIM_RAW_EVENT_ID = (
    "bleepingcomputer:https://www.bleepingcomputer.com/news/security/"
    "trezor-discloses-data-breach-affecting-nearly-14-000-customers/")
ACCOUNT_RAW_EVENT_ID = "sec_edgar_submissions:0000012345-26-000001"


def _seed_provenance(conn):
    """One account card (its own incident, entity named) and one sector peer
    card minted from the victim-naming raw event."""
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('own_incident','Own incident',6,120)")
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('peer_incident','Peer incident',4,60)")
    conn.execute("INSERT INTO watchlist_entities (entity_id, name, active) "
                 "VALUES ('E_ACME','Acme Energy',1)")
    for sid, name, rank in (("bleepingcomputer", "BleepingComputer", 3),
                            ("sec_edgar_submissions", "EDGAR", 1)):
        conn.execute("INSERT INTO source_policies (source_id, name, enabled, "
                     "ttl, access_method, evidence_rank) VALUES (?,?,1,3600,"
                     "'rss',?)", (sid, name, rank))
    conn.execute("INSERT INTO source_runs (run_id, source_id, started_at, "
                 "status, parser_version) VALUES ('run1','bleepingcomputer',"
                 "'2026-08-15T08:00:00+00:00','success','security_rss/1.0')")
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, run_id, event_date, "
        "payload, url) VALUES (?, 'bleepingcomputer','run1','2026-08-14','{}',"
        "'https://www.bleepingcomputer.com/news/security/trezor-discloses/')",
        (VICTIM_RAW_EVENT_ID,))
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, payload, "
        "url) VALUES (?, 'sec_edgar_submissions','2026-08-14','{}',"
        "'https://www.sec.gov/x')", (ACCOUNT_RAW_EVENT_ID,))
    rows = (
        ("S_PEER", VICTIM_RAW_EVENT_ID, None, "sector", "peer_incident",
         "An organization in the sector disclosed a breach", 2.5),
        ("S_OWN", ACCOUNT_RAW_EVENT_ID, "E_ACME", "account", "own_incident",
         "Acme Energy discloses a security incident", 4.5),
    )
    for sid, raw, entity, scope, trig, headline, score in rows:
        conn.execute(
            "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
            "signal_scope, trigger_id, event_date, headline, evidence_snippet, "
            "source_url, confidence, evidence_quality, "
            "incident_evidence_level, customer_facing_allowed, score, status, "
            "score_base, score_decay, score_account_fit, score_scope_fit, "
            "scored_at, scoring_config_version) "
            "VALUES (?,?,?,?,?, '2026-08-14', ?, ?, 'https://example.test/x', "
            "0.8,'IR','confirmed',1,?, 'active', 5, 1.0, 1.0, 0.55, "
            "'2026-08-15T09:00:00+00:00', 'abc123def4567890')",
            (sid, raw, entity, scope, trig, headline, headline, score))
        conn.execute(
            "INSERT INTO signal_evidence (signal_id, raw_event_id, "
            "evidence_text, evidence_locator, evidence_rank, "
            "extraction_version) VALUES (?, ?, 'An organization disclosed a "
            "breach.', 'item', 3, 'incident_security_rss/1.0')", (sid, raw))
    conn.execute(
        "INSERT INTO classified_events (raw_event_id, classifier_id, "
        "parser_version, processed_at, signals_emitted) VALUES "
        "(?, 'incident_security_rss', 'incident_security_rss/1.1', "
        "'2026-08-15T08:05:00+00:00', 1)", (VICTIM_RAW_EVENT_ID,))


class ProvenanceBase(unittest.TestCase):
    def setUp(self):
        self.path = _make_db(_seed_provenance)
        os.environ["GRIDSIGNALS_DB"] = self.path
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("GRIDSIGNALS_DB", None)
        try:
            os.remove(self.path)
        except OSError:
            pass


class TestProvenanceLine(ProvenanceBase):
    def test_account_card_shows_the_raw_event_id_and_both_versions(self):
        resp = self.client.get(f"/card/{render.card_key('S_OWN')}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Provenance", resp.text)
        # an account card already names its entity, so the real key is shown —
        # that is what makes the card reproducible without shell access.
        self.assertIn(ACCOUNT_RAW_EVENT_ID, resp.text)
        self.assertIn("abc123def4567890", resp.text)          # scoring config
        self.assertIn("2026-08-15T09:00:00+00:00", resp.text)  # scored_at

    def test_sector_card_hashes_the_raw_event_id(self):
        resp = self.client.get(f"/card/{render.card_key('S_PEER')}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Provenance", resp.text)
        self.assertIn("raw event (hashed)", resp.text)
        digest = data.identity_safe_raw_event_ref(
            VICTIM_RAW_EVENT_ID, "sector")["ref"]
        self.assertIn(digest, resp.text)
        # the gate: neither the URL nor the victim name reaches the DOM
        self.assertNotIn(VICTIM_RAW_EVENT_ID, resp.text)
        self.assertNotIn("trezor", resp.text.lower())

    def test_classifier_version_is_primary_fetch_version_is_secondary(self):
        resp = self.client.get(f"/card/{render.card_key('S_PEER')}")
        self.assertIn("classifier version: incident_security_rss/1.1", resp.text)
        self.assertIn("fetch version: security_rss/1.0", resp.text)

    def test_feed_never_leaks_the_victim_through_the_provenance_line(self):
        home = self.client.get("/").text
        self.assertIn("Provenance", home)
        self.assertNotIn(VICTIM_RAW_EVENT_ID, home)
        self.assertNotIn("trezor", home.lower())

    def test_unscored_card_reads_pre_versioning_not_a_backfilled_guess(self):
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE signals SET scoring_config_version = NULL, "
                     "scored_at = NULL WHERE signal_id = 'S_OWN'")
        conn.commit()
        conn.close()
        resp = self.client.get(f"/card/{render.card_key('S_OWN')}")
        self.assertIn("pre-versioning", resp.text)
        self.assertIn("never scored", resp.text)


class TestDigestNeverLeaksTheRawEventId(ProvenanceBase):
    """The digest is the file Kevin hand-sends; it is shaped by the same
    render.card_view, so the gate is asserted against the real artifact."""

    def setUp(self):
        super().setUp()
        self.digest_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.digest_dir, ignore_errors=True)
        super().tearDown()

    def _generate(self):
        path = digest_mod.generate(db_path=self.path,
                                   digest_dir=self.digest_dir, now=NOW)
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_generated_digest_carries_the_cards_but_not_the_victim(self):
        html = self._generate()
        self.assertIn("An organization in the sector disclosed a breach", html)
        self.assertIn("Acme Energy discloses a security incident", html)
        self.assertNotIn(VICTIM_RAW_EVENT_ID, html)
        self.assertNotIn(VICTIM_NAME.lower(), html.lower())

    def test_sector_card_view_used_by_the_digest_is_already_hashed(self):
        """Belt and braces: the digest template renders only what card_view
        hands it, and card_view has already hashed the sector reference — so a
        future digest template that prints the provenance block stays safe."""
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            detail = data.signal_detail(conn, "S_PEER")
            card = render.card_view(detail, data.badge_legend(conn))
        finally:
            conn.close()
        self.assertTrue(card["provenance"]["raw_event_hashed"])
        self.assertNotIn("trezor", card["provenance"]["raw_event_ref"].lower())
        self.assertRegex(card["provenance"]["raw_event_ref"], r"^[0-9a-f]{16}$")


if __name__ == "__main__":
    unittest.main()
