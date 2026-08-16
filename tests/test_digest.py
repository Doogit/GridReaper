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
from hashlib import sha256

from fastapi.testclient import TestClient

from app.db.migrate import apply_migrations
from app.ui import data
from app.ui_web import digest as digest_mod
from app.ui_web import render
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
                incident_level=None, raw_event_id=None):
    raw = raw_event_id or ("re_acc" if scope in ("account", "parent") else None)
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
        self.digest_dir = digest_mod.digest_dir(self.path)
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

    def test_top_accounts_are_score_first_not_newest_slice(self):
        conn = sqlite3.connect(self.path)
        try:
            for i in range(digest_mod.TOP_ACCOUNT_CARDS):
                _add_signal(conn, f"S_LOW{i}", "E_ACME", "account", "t_lead",
                            days_ago(i), f"newer low score {i}",
                            cfa=1, score=0.1)
            _add_signal(conn, "S_OLDER_HIGH", "E_ACME", "account", "t_lead",
                        days_ago(90), "older high score", cfa=1, score=9.9)
            conn.commit()
        finally:
            conn.close()

        html = _read(digest_mod.generate(now=NOW))
        self.assertIn("older high score", html)
        self.assertNotIn("newer low score 7", html)

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

    def test_present_file_never_renders_empty_state(self):
        # Coupling guard: when a digest file exists on disk, the in-app view must
        # render its extracted body — never the "No digest generated yet" empty
        # copy. If a template reshape breaks the route's body/timestamp regex, the
        # route would silently fall back to the empty state though a file exists;
        # this pins "absence means absence, not a regex mismatch" (persona-pass D2).
        digest_mod.generate(now=NOW)
        dom = self.client.get("/digest").text
        self.assertNotIn("No digest generated yet", dom)

    def test_present_unparseable_file_never_renders_empty_state(self):
        os.makedirs(self.digest_dir, exist_ok=True)
        with open(os.path.join(self.digest_dir, "digest-2026-08-13.html"), "w",
                  encoding="utf-8") as fh:
            fh.write("<!doctype html><main>saved digest without body tag</main>")

        resp = self.client.get("/digest")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Saved digest file digest-2026-08-13.html could not be displayed",
                      resp.text)
        self.assertNotIn("No digest generated yet", resp.text)

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


# -- U5: R3.7 provenance renders in the digest too ---------------------------
#
# digest.html carries its OWN card macro (it does not include _card.html), which
# is why the R3.7 provenance block used to be a named exception on this surface.
# A digest is the artifact that leaves the tool — a saved file someone forwards —
# so it is the surface where "which raw event produced this" matters most, and
# also the one where a leaked victim name is hardest to recall.

# ransomware.live's permalink path is base64("<Victim>@<crew>"), so both the
# stored raw_event_id and the signal's own source_url spell the victim. A sector
# card must show NEITHER: identity_safe_source_url swaps the link for the
# tracker index, identity_safe_raw_event_ref hashes the id (R3.7 + R4.1).
VICTIM_PERMALINK = ("https://www.ransomware.live/id/"
                    "Q29tbXVuaXR5IENvbm5lY3Rpb25zQHRoZWdlbnRsZW1lbg==")
VICTIM_RAW_ID = f"ransomware_live:{VICTIM_PERMALINK}"
# The security-press peer path is the other name-carrying shape: an RSS
# native_id is the article link, whose slug names the breached company.
PRESS_RAW_ID = ("bleepingcomputer:https://www.bleepingcomputer.com/news/"
                "security/victimcorp-discloses-data-breach/")
ACCOUNT_RAW_ID = "sec_edgar_submissions:0000012345-26-000004"
# Both security_rss peer paths mint FIXED template headlines, so two peer cards
# are literally the same string — the source name is the only discriminator.
PEER_HEADLINE = ("Sector peer disclosed a cybersecurity incident - "
                 "security-press reported")


def _hashed(raw_event_id):
    return sha256(raw_event_id.encode("utf-8")).hexdigest()[
        :data.RAW_EVENT_REF_LEN]


def seed_provenance(conn):
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_peer','Peer incident',3,60)")
    conn.execute("INSERT INTO watchlist_entities (entity_id, name) "
                 "VALUES ('E_ACME','Acme Energy')")
    conn.executemany(
        "INSERT INTO source_policies (source_id, name, evidence_rank) "
        "VALUES (?,?,?)",
        [("ransomware_live", "ransomware.live", 3),
         ("bleepingcomputer", "BleepingComputer", 2),
         ("sec_edgar_submissions", "SEC EDGAR", 1)])
    conn.executemany(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, url) "
        "VALUES (?,?,?,?)",
        [(VICTIM_RAW_ID, "ransomware_live", days_ago(3), VICTIM_PERMALINK),
         (PRESS_RAW_ID, "bleepingcomputer", days_ago(4),
          "https://www.bleepingcomputer.com/news/security/victimcorp-discloses-data-breach/"),
         (ACCOUNT_RAW_ID, "sec_edgar_submissions", days_ago(2),
          "https://www.sec.gov/Archives/8k.htm")])
    _add_signal(conn, "S_PEER_LEAK", None, "sector", "t_peer", days_ago(3),
                PEER_HEADLINE, score=2.2, source_url=VICTIM_PERMALINK,
                raw_event_id=VICTIM_RAW_ID)
    _add_signal(conn, "S_PEER_PRESS", None, "sector", "t_peer", days_ago(4),
                PEER_HEADLINE, score=2.1, source_url="https://example.invalid/x",
                raw_event_id=PRESS_RAW_ID)
    _add_signal(conn, "S_ACC_PROV", "E_ACME", "account", "t_peer", days_ago(2),
                "Acme discloses a material cybersecurity incident", cfa=1,
                score=4.4, source_url="https://www.sec.gov/Archives/8k.htm",
                raw_event_id=ACCOUNT_RAW_ID)


class TestDigestProvenance(DigestTestBase):
    seed_fn = staticmethod(seed_provenance)

    def setUp(self):
        super().setUp()
        self.html = _read(digest_mod.generate(now=NOW))

    def test_sector_card_hashes_its_raw_event_and_never_names_the_victim(self):
        # the provenance block renders at all (the closed exception) ...
        self.assertIn("Provenance", self.html)
        self.assertIn("raw event (hashed)", self.html)
        # ... hashed, with the digest an operator can grep the store for ...
        self.assertIn(_hashed(VICTIM_RAW_ID), self.html)
        # ... and the identity gone from the document entirely: neither the
        # stored id nor the per-victim permalink it embeds reaches the DOM.
        self.assertNotIn(VICTIM_RAW_ID, self.html)
        self.assertNotIn(VICTIM_PERMALINK, self.html)
        self.assertNotIn("Q29tbXVuaXR5", self.html)

    def test_security_press_peer_raw_event_id_is_hashed_too(self):
        self.assertIn(_hashed(PRESS_RAW_ID), self.html)
        self.assertNotIn(PRESS_RAW_ID, self.html)

    def test_account_card_shows_its_raw_event_reference_verbatim(self):
        # An account card already names its entity, so its id discloses nothing
        # new — and reproducing the card needs the real key, not a digest.
        self.assertIn(ACCOUNT_RAW_ID, self.html)
        self.assertNotIn(_hashed(ACCOUNT_RAW_ID), self.html)

    def test_peer_cards_from_different_sources_have_distinguishable_meta(self):
        # Same headline twice; only the meta line tells them apart.
        self.assertEqual(self.html.count(PEER_HEADLINE), 2)
        self.assertIn("ransomware.live", self.html)
        self.assertIn("BleepingComputer", self.html)

        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            legend = data.badge_legend(conn)
            leak = render.card_view(
                data.signal_detail(conn, "S_PEER_LEAK"), legend)
            press = render.card_view(
                data.signal_detail(conn, "S_PEER_PRESS"), legend)
        finally:
            conn.close()
        self.assertEqual(leak["headline"], press["headline"])
        self.assertNotEqual(leak["meta_bits"], press["meta_bits"])
        self.assertIn("ransomware.live", leak["meta_bits"])
        self.assertIn("BleepingComputer", press["meta_bits"])


class TestDigestRetention(unittest.TestCase):
    def test_prune_keeps_newest_and_spares_latest_alias(self):
        # Dated files sort chronologically by name; keep the newest `keep`, prune
        # the rest, and never touch the digest-latest.html alias (R8.8 retention).
        d = tempfile.mkdtemp()
        try:
            for day in range(1, 6):  # digest-2026-01-01 .. digest-2026-01-05
                open(os.path.join(d, f"digest-2026-01-{day:02d}.html"), "w").close()
            open(os.path.join(d, "digest-latest.html"), "w").close()
            digest_mod._prune_old_digests(d, keep=3)
            self.assertEqual(sorted(os.listdir(d)), [
                "digest-2026-01-03.html", "digest-2026-01-04.html",
                "digest-2026-01-05.html", "digest-latest.html"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
