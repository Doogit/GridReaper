"""Digest generator + in-app view tests (R8.8, R4.1, R7.12, R6.6, R10.2).

Hermetic: a temp-file SQLite DB via apply_migrations (FK on, no network),
pointed at both the generator and the FastAPI app through GRIDSIGNALS_DB. The
digest reads the same data layer + card shaper the live feed uses, so these tests
carry the same trust invariants across the artifact: outreach appears ONLY on a
customer_facing_allowed card (R7.12), every card keeps its evidence/source (R4.1),
an empty store still writes a VALID digest with honest low-volume copy (R6.6), and
the saved file is self-contained (no url_for, no CDN / external http refs).
"""
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import digest as digest_mod
from app.db.migrate import apply_migrations
from app.ui_web.app import app


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()

NOW = datetime(2026, 8, 13, 9, 30, 0, tzinfo=timezone.utc)
FORBIDDEN_PRICE = "$9/GB rumored"
CONFIRMED_OUTREACH = "a licensing review may be timely for Acme"
WITHHELD_OUTREACH = "SECRET_OUTREACH_SHOULD_NOT_APPEAR"


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def days_ago(n):
    return (NOW.date() - timedelta(days=n)).isoformat()


def _add_signal(conn, sid, entity_id, scope, trigger_id, event_date, headline,
                cfa=0, status="active", score=None, source_url="http://src/doc",
                incident_level=None):
    raw = "re_acc" if scope in ("account", "parent") else None
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, signal_scope, "
        "trigger_id, event_date, headline, evidence_snippet, source_url, "
        "confidence, evidence_quality, incident_evidence_level, "
        "customer_facing_allowed, score, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, raw, entity_id, scope, trigger_id, event_date, headline, headline,
         source_url, 0.9, "IR", incident_level, cfa, score, status))


def seed(conn):
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_lead','Leadership',4,90)")
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_reg','Regulatory',5,600)")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector, richness, "
        "coverage_flag, gov_cloud_likelihood, tenant_cloud_environment) VALUES "
        "('E_ACME','Acme Energy','iou_electric','high','edgar-visible',"
        "'possible','commercial')")
    conn.execute("INSERT INTO products (product_id, name) VALUES "
                 "('p_sentinel','Microsoft Sentinel')")
    conn.execute(
        "INSERT INTO license_play_candidates (play_id, trigger_id, product_id, "
        "recommended_path, discovery_question) VALUES "
        "('play1','t_lead','p_sentinel','Adopt E5 grant','What is your SIEM?')")
    conn.execute(
        "INSERT INTO license_facts (fact_id, product_id, segment, price_note, "
        "source_quality, source_url, verified_date) VALUES "
        "('f_primary','p_sentinel','commercial','$2/GB','primary','http://p', ?)",
        (days_ago(30),))
    conn.execute(
        "INSERT INTO license_facts (fact_id, product_id, segment, price_note, "
        "source_quality, source_url, verified_date) VALUES "
        "('f_nonprimary','p_sentinel','commercial',?,'non-primary','http://b', ?)",
        (FORBIDDEN_PRICE, days_ago(400)))
    conn.execute(
        "INSERT INTO source_policies (source_id, name, enabled, ttl, "
        "access_method, evidence_rank) VALUES ('sp_ok','OK',1,3600,'rss',2)")
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, payload, url) "
        "VALUES ('re_acc','sp_ok', ?, '{\"title\": \"Acme\"}', 'http://acme/8k')",
        (days_ago(5),))

    # account cards: one cleared-for-outreach (highest score), one restricted.
    _add_signal(conn, "S_ACC1", "E_ACME", "account", "t_lead", days_ago(5),
                "Acme names new CISO", cfa=1, score=4.2)
    _add_signal(conn, "S_ACC2", "E_ACME", "account", "t_lead", days_ago(9),
                "Acme restricted outreach event", cfa=0, score=3.0)
    # sector + regulatory cards
    _add_signal(conn, "S_SEC", None, "sector", "t_reg", days_ago(2),
                "FERC final rule CIP revision", cfa=1, score=2.75)
    _add_signal(conn, "S_REG", None, "regulatory_calendar", "t_reg", days_ago(1),
                "FERC proposed rule virtualization", cfa=1, score=2.6)

    conn.execute(
        "INSERT INTO signal_evidence (signal_id, raw_event_id, evidence_text, "
        "evidence_locator, evidence_rank) VALUES ('S_ACC1','re_acc',"
        "'Acme appointed a new CISO.','para 2', 1)")
    conn.execute(
        "INSERT INTO license_play_snapshots (signal_id, play_id, fact_ids, "
        "generated_at, generation_version, display_text, outreach_safe_text) "
        "VALUES ('S_ACC1','play1', '[\"f_primary\",\"f_nonprimary\"]', ?, "
        "'plays/1.0', 'Recommended path: Adopt E5 grant', ?)",
        (iso(NOW), CONFIRMED_OUTREACH))
    conn.execute(
        "INSERT INTO license_play_snapshots (signal_id, play_id, fact_ids, "
        "generated_at, generation_version, display_text, outreach_safe_text) "
        "VALUES ('S_ACC2','play1', '[]', ?, 'plays/1.0', "
        "'Recommended path: Adopt E5 grant', ?)",
        (iso(NOW), WITHHELD_OUTREACH))
    conn.executemany(
        "INSERT INTO badge_legend (badge_kind, code, label, description) "
        "VALUES (?,?,?,?)",
        [("evidence_quality", "IR", "Investor Report", "From an investor filing"),
         ("source_quality", "non-primary", "Non-primary",
          "Not an authoritative source (R4.3)")])


def make_db(seed_fn, db_dir):
    # Each test gets its OWN directory so digests (written to <db-dir>/digests)
    # never leak across tests — the whole point of a per-test snapshot check.
    path = os.path.join(db_dir, "gridsignals.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    apply_migrations(conn)
    seed_fn(conn)
    conn.commit()
    conn.close()
    return path


class DigestTestBase(unittest.TestCase):
    seed_fn = staticmethod(seed)

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = make_db(self.seed_fn, self.tmpdir)
        os.environ["GRIDSIGNALS_DB"] = self.path
        # digests land in <db-dir>/digests (the generator's default), isolated
        # per test so the "no digest yet" case sees a genuinely empty dir.
        self.digest_dir = digest_mod._digest_dir(self.path)
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("GRIDSIGNALS_DB", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)


# -- U2: generator ----------------------------------------------------------

class TestGenerate(DigestTestBase):
    def test_writes_dated_file_with_all_sections(self):
        path = digest_mod.generate(now=NOW)
        self.assertTrue(os.path.exists(path))
        self.assertTrue(path.endswith(f"digest-{NOW.date().isoformat()}.html"))
        html = _read(path)
        # each scope's headline present + the scope divider
        self.assertIn("Acme names new CISO", html)          # account
        self.assertIn("FERC final rule CIP revision", html)  # sector
        self.assertIn("FERC proposed rule virtualization", html)  # regulatory
        self.assertIn("gs-divider", html)
        self.assertIn("Sector &amp; regulatory", html)
        # R10.2 generation timestamp in the header
        self.assertIn(NOW.isoformat(), html)

    def test_outreach_gated_by_customer_facing_allowed(self):
        html = _read(digest_mod.generate(now=NOW))
        # R7.12: cleared card shows its opener; restricted card does not
        self.assertIn(CONFIRMED_OUTREACH, html)
        self.assertNotIn(WITHHELD_OUTREACH, html)

    def test_evidence_and_source_kept_but_no_price(self):
        html = _read(digest_mod.generate(now=NOW))
        self.assertIn("Acme appointed a new CISO.", html)   # evidence (R4.1)
        self.assertIn("http://src/doc", html)               # source link (R4.1)
        # R4.3/R7.11: a non-primary price string never reaches the artifact
        self.assertNotIn(FORBIDDEN_PRICE, html)
        self.assertNotIn("$9", html)
        self.assertNotIn("$2/GB", html)

    def test_saved_file_is_self_contained(self):
        html = _read(digest_mod.generate(now=NOW))
        self.assertIn("<!doctype html>", html.lower())
        self.assertNotIn("url_for", html)
        # no external stylesheet/script/CDN refs — styles are inlined
        self.assertNotIn("<link", html.lower())
        self.assertNotIn("app.css", html)
        # the only http(s) refs are per-card source links (R4.1), which live in
        # href="..."; there must be no src="http" (script/img) or cdn host.
        self.assertNotIn('src="http', html)
        self.assertNotIn("cdn", html.lower())

    def test_latest_alias_written(self):
        digest_mod.generate(now=NOW)
        self.assertTrue(os.path.exists(
            os.path.join(self.digest_dir, "digest-latest.html")))


class TestGenerateEmpty(DigestTestBase):
    seed_fn = staticmethod(lambda conn: None)  # migrated but no data

    def test_empty_store_writes_valid_low_volume_digest(self):
        path = digest_mod.generate(now=NOW)
        self.assertTrue(os.path.exists(path))          # file still written (R6.6)
        html = _read(path)
        self.assertIn("<!doctype html>", html.lower())
        self.assertIn("low-volume by design", html)    # honest copy, not "broken"
        self.assertIn(NOW.isoformat(), html)           # still timestamped (R10.2)


# -- U4: in-app /digest view ------------------------------------------------

class TestDigestRoute(DigestTestBase):
    def test_route_serves_saved_digest_with_as_of(self):
        digest_mod.generate(now=NOW)
        resp = self.client.get("/digest")
        self.assertEqual(resp.status_code, 200)
        dom = resp.text
        self.assertIn("Acme names new CISO", dom)       # seeded headline
        self.assertIn("gs-divider", dom)                # scope divider
        # a visible "generated at" as-of timestamp marks it a snapshot (D2)
        self.assertIn("Generated at", dom)
        self.assertIn(NOW.isoformat(), dom)

    def test_nav_link_present_and_active(self):
        digest_mod.generate(now=NOW)
        dom = self.client.get("/digest").text
        self.assertIn('href="/digest"', dom)
        self.assertRegex(dom, r'href="/digest"[^>]*class="[^"]*active')

    def test_no_saved_digest_is_honest_not_500(self):
        # no generate() called → no file on disk
        resp = self.client.get("/digest")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("No digest generated yet", resp.text)


if __name__ == "__main__":
    unittest.main()
