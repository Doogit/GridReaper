"""Home signal feed tests for the FastAPI web UI (R8.1, R7.2, R6.6, R9.1, R4.3).

The TestClient port of tests/test_ui_feed.py: same hermetic fixture (temp-file
SQLite via apply_migrations, pointed at the app through GRIDSIGNALS_DB, no
network), same trust assertions carried across the framework change — cards
render scope-separated, price never reaches the DOM, outreach is gated, the
status filter hides decayed/superseded by default, feedback writes a row and
not_useful requires a reason (enforced server-side), and upstream text is
HTML-escaped. Adds keyset load-more coverage the Streamlit feed exercised via
session_state.
"""
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db.migrate import apply_migrations
from app.ui_web.app import app

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
# A non-primary price string that must NEVER reach the DOM (R4.3/R7.11).
FORBIDDEN_PRICE = "$9/GB rumored"


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def days_ago(n):
    return (NOW.date() - timedelta(days=n)).isoformat()


URLISH_SIGNAL_ID = "t_lead:presswire_prnewswire:https://example.com/a?x=1&y=2:E_ACME"


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

    _add_signal(conn, "S_ACC1", "E_ACME", "account", "t_lead", days_ago(5),
                "Acme names new CISO", cfa=1, score=4.2)
    _add_signal(conn, "S_ACC2", "E_ACME", "account", "t_lead", days_ago(9),
                "Acme restricted outreach event", cfa=0, score=3.0)
    _add_signal(conn, URLISH_SIGNAL_ID, "E_ACME", "account", "t_lead", days_ago(7),
                "URL-shaped raw event id stays HTMX-safe", cfa=1, score=3.2,
                source_url="javascript:alert(1)")
    _add_signal(conn, "S_SEC", None, "sector", "t_reg", days_ago(2),
                "FERC final rule CIP revision", cfa=1, score=2.75)
    _add_signal(conn, "S_REG", None, "regulatory_calendar", "t_reg", days_ago(1),
                "FERC proposed rule virtualization", cfa=1, score=2.6)
    _add_signal(conn, "S_DEAD", "E_ACME", "account", "t_lead", days_ago(400),
                "Old decayed exec note", cfa=1, score=0.8, status="decayed")
    _add_signal(conn, "S_SUP", None, "regulatory_calendar", "t_reg", days_ago(20),
                "Superseded NOPR", cfa=1, score=2.0, status="superseded")
    _add_signal(conn, "S_RET", "E_ACME", "account", "t_lead", days_ago(12),
                "Retracted stale peer card", cfa=1, score=2.1, status="retracted")

    conn.execute(
        "INSERT INTO signal_evidence (signal_id, raw_event_id, evidence_text, "
        "evidence_locator, evidence_rank) VALUES ('S_ACC1','re_acc',"
        "'Acme appointed a new CISO.','para 2', 1)")
    conn.execute(
        "INSERT INTO signal_evidence (signal_id, raw_event_id, evidence_text, "
        "evidence_locator, evidence_rank) VALUES (?, 're_acc', "
        "'URL-ish id evidence.','para 4', 1)",
        (URLISH_SIGNAL_ID,))
    conn.execute(
        "INSERT INTO license_play_snapshots (signal_id, play_id, fact_ids, "
        "generated_at, generation_version, display_text, outreach_safe_text) "
        "VALUES ('S_ACC1','play1', '[\"f_primary\",\"f_nonprimary\"]', ?, "
        "'plays/1.0', 'Recommended path: Adopt E5 grant', "
        "'Given the recent leadership change, a licensing review may be timely.')",
        (iso(NOW),))
    conn.execute(
        "INSERT INTO license_play_snapshots (signal_id, play_id, fact_ids, "
        "generated_at, generation_version, display_text, outreach_safe_text) "
        "VALUES ('S_ACC2','play1', '[]', ?, 'plays/1.0', "
        "'Recommended path: Adopt E5 grant', "
        "'SECRET_OUTREACH_SHOULD_NOT_APPEAR for restricted account.')",
        (iso(NOW),))
    conn.executemany(
        "INSERT INTO badge_legend (badge_kind, code, label, description) "
        "VALUES (?,?,?,?)",
        [("evidence_quality", "IR", "Investor Report", "From an investor filing"),
         ("source_quality", "non-primary", "Non-primary",
          "Not an authoritative source (R4.3)")])


def make_db(seed_fn):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    apply_migrations(conn)
    seed_fn(conn)
    conn.commit()
    conn.close()
    return path


class FeedTestBase(unittest.TestCase):
    seed_fn = staticmethod(seed)

    def setUp(self):
        self.path = make_db(self.seed_fn)
        os.environ["GRIDSIGNALS_DB"] = self.path
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("GRIDSIGNALS_DB", None)
        try:
            os.remove(self.path)
        except OSError:
            pass

    def home(self, **params):
        resp = self.client.get("/", params=params)
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def feedback_rows(self):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(
                "SELECT signal_id, verdict, reason_code FROM feedback").fetchall()
        finally:
            conn.close()


class TestCardsRender(FeedTestBase):
    def test_account_and_sector_cards_present(self):
        dom = self.home()
        self.assertIn("Acme names new CISO", dom)
        self.assertIn("FERC final rule CIP revision", dom)
        self.assertIn("gs-divider", dom)
        # divider label is html-escaped in the DOM
        self.assertIn("Sector &amp; regulatory", dom)

    def test_base_layout_chrome(self):
        dom = self.home()
        self.assertIn("gs-topbar", dom)
        self.assertIn("app.css", dom)
        self.assertIn("htmx.min.js", dom)

    def test_feed_body_auto_refreshes_every_120s(self):
        # R8.1: the feed body polls every 120s so a background first-load ingest
        # (and later scored cards) surface without a manual reload. hx-include
        # carries BOTH view controls through the poll — dropping either would
        # reset the operator's view every two minutes.
        dom = self.home()
        self.assertIn('id="gs-feed-body"', dom)
        self.assertIn('hx-trigger="every 120s"', dom)
        self.assertIn('hx-get="/feed"', dom)
        self.assertIn("hx-include=\"[name='status'],[name='sort']\"", dom)

    def test_non_primary_chip_badged_and_no_price_in_dom(self):
        dom = self.home()
        self.assertIn("gs-badge nonprimary", dom)   # R4.3 chip rendered
        self.assertIn("Non-primary", dom)            # legend label used
        # R4.3/R7.11: the non-primary price string must never reach the DOM
        self.assertNotIn(FORBIDDEN_PRICE, dom)
        self.assertNotIn("$9", dom)
        self.assertNotIn("$2/GB", dom)

    def test_urlish_signal_id_does_not_break_htmx_selectors(self):
        dom = self.home()
        self.assertIn("URL-shaped raw event id stays HTMX-safe", dom)
        self.assertNotIn(f'id="gs-fb-{URLISH_SIGNAL_ID}"', dom)
        self.assertIn('hx-target="#gs-fb-', dom)
        self.assertIn("signal_id=t_lead%3Apresswire_prnewswire%3Ahttps", dom)
        self.assertIn("%26y%3D2%3AE_ACME", dom)

    def test_non_http_source_url_is_not_rendered_as_link(self):
        dom = self.home()
        self.assertNotIn("javascript:alert(1)", dom)


class TestStatusFilter(FeedTestBase):
    def test_decayed_and_superseded_hidden_by_default(self):
        dom = self.home()
        self.assertNotIn("Old decayed exec note", dom)
        self.assertNotIn("Superseded NOPR", dom)
        self.assertNotIn("Retracted stale peer card", dom)

    def test_widen_status_to_all_reveals_them(self):
        dom = self.home(status="all")
        self.assertIn("Old decayed exec note", dom)
        self.assertIn("Superseded NOPR", dom)
        self.assertIn("Retracted stale peer card", dom)

    def test_retracted_status_filter_reveals_only_retracted_cards(self):
        dom = self.home(status="retracted")
        self.assertIn("Retracted stale peer card", dom)
        self.assertNotIn("Acme names new CISO", dom)
        self.assertNotIn("Old decayed exec note", dom)

    def test_feed_body_partial_honors_status(self):
        # the HTMX status-filter swap path (/feed) returns just the body
        resp = self.client.get("/feed", params={"status": "all"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Superseded NOPR", resp.text)
        self.assertNotIn("gs-topbar", resp.text)     # body only, no page chrome


class TestOutreachGate(FeedTestBase):
    def test_outreach_shown_only_when_customer_facing_allowed(self):
        dom = self.home()
        self.assertIn("a licensing review may be timely", dom)     # cfa=1
        self.assertNotIn("SECRET_OUTREACH_SHOULD_NOT_APPEAR", dom)  # cfa=0


class TestFeedback(FeedTestBase):
    def test_useful_records_row(self):
        resp = self.client.post(
            "/feedback", data={"signal_id": "S_ACC1", "verdict": "useful"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Feedback recorded.", resp.text)
        rows = self.feedback_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0][0], rows[0][1]), ("S_ACC1", "useful"))

    def test_reason_form_revealed_on_not_useful(self):
        resp = self.client.get("/feedback/reason", params={"signal_id": "S_ACC1"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('name="reason_code"', resp.text)
        self.assertIn("wrong_entity", resp.text)
        self.assertIn('hx-post="/feedback"', resp.text)

    def test_reason_form_target_uses_safe_dom_id(self):
        resp = self.client.get("/feedback/reason", params={"signal_id": URLISH_SIGNAL_ID})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('hx-target="#gs-fb-', resp.text)
        self.assertNotIn(f'hx-target="#gs-fb-{URLISH_SIGNAL_ID}"', resp.text)

    def test_not_useful_without_reason_is_rejected_server_side(self):
        resp = self.client.post(
            "/feedback", data={"signal_id": "S_ACC1", "verdict": "not_useful"})
        self.assertEqual(resp.status_code, 200)
        # no row written, and the reason form comes back with the error (R9.1)
        self.assertEqual(len(self.feedback_rows()), 0)
        self.assertIn("reason_code is required", resp.text)
        self.assertIn('name="reason_code"', resp.text)

    def test_not_useful_with_reason_records_row(self):
        resp = self.client.post("/feedback", data={
            "signal_id": "S_ACC1", "verdict": "not_useful",
            "reason_code": "wrong_entity", "note": "misattributed"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Feedback recorded.", resp.text)
        rows = self.feedback_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(tuple(rows[0]), ("S_ACC1", "not_useful", "wrong_entity"))


def seed_paginated(conn):
    """26 active account signals for one entity — one full page + one over."""
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_lead','Leadership',4,90)")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector, richness, "
        "coverage_flag) VALUES ('E_ACME','Acme Energy','iou_electric','high',"
        "'edgar-visible')")
    # account signals carry raw_event_id='re_acc' (see _add_signal); satisfy the
    # FK chain signals -> raw_events -> source_policies.
    conn.execute(
        "INSERT INTO source_policies (source_id, name, enabled, ttl, "
        "access_method, evidence_rank) VALUES ('sp_ok','OK',1,3600,'rss',2)")
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, payload, url) "
        "VALUES ('re_acc','sp_ok', ?, '{\"title\": \"Acme\"}', 'http://acme/8k')",
        (days_ago(1),))
    for i in range(1, 27):
        _add_signal(conn, f"S{i:02d}", "E_ACME", "account", "t_lead",
                    days_ago(i), f"Paginated account signal {i}", score=3.0)


class TestKeysetPagination(FeedTestBase):
    seed_fn = staticmethod(seed_paginated)

    def test_first_page_caps_and_offers_load_more(self):
        # keyset paging is the 'recent' view; the score view is a capped ranking
        dom = self.home(sort="recent")
        self.assertEqual(dom.count('<article class="gs-card'), 25)
        self.assertIn("gs-loadmore", dom)
        # oldest signal (26th) is below the fold, not on page 1
        self.assertNotIn("Paginated account signal 26", dom)

    def test_score_view_caps_without_a_dead_load_more(self):
        """A score ranking has no chronological keyset, so it caps and says so
        rather than offering a Load more that cannot resume."""
        dom = self.home(sort="score")
        self.assertEqual(dom.count('<article class="gs-card'), 25)
        self.assertNotIn("gs-loadmore", dom)
        self.assertIn("Capped at 25 per section", dom)

    def test_load_more_returns_remainder_and_stops(self):
        dom = self.home(sort="recent")
        url = re.search(r'hx-get="(/feed/more[^"]*)"', dom).group(1)
        resp = self.client.get(url.replace("&amp;", "&"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Paginated account signal 26", resp.text)
        self.assertEqual(resp.text.count('<article class="gs-card'), 1)
        self.assertNotIn("gs-loadmore", resp.text)   # end of the list


TOP_HEADLINE = "Top-scoring account signal"
_HEADLINE_RE = re.compile(r'<div class="gs-headline">([^<]*)</div>')


def seed_buried_top(conn):
    """The B3 shape: the highest-scoring card is the OLDEST, and a pile of
    recent retracted peer cards sits on top of it. Date ordering buries it onto
    page 2 at status=all; score ordering leads with it."""
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_lead','Leadership',4,90)")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector, richness, "
        "coverage_flag) VALUES ('E_ACME','Acme Energy','iou_electric','high',"
        "'edgar-visible')")
    conn.execute(
        "INSERT INTO source_policies (source_id, name, enabled, ttl, "
        "access_method, evidence_rank) VALUES ('sp_ok','OK',1,3600,'rss',2)")
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, payload, url) "
        "VALUES ('re_acc','sp_ok', ?, '{\"title\": \"Acme\"}', 'http://acme/8k')",
        (days_ago(1),))
    _add_signal(conn, "S_TOP", "E_ACME", "account", "t_lead", days_ago(90),
                TOP_HEADLINE, cfa=1, score=4.9)
    _add_signal(conn, "S_MID", "E_ACME", "account", "t_lead", days_ago(2),
                "Newer but weaker account signal", cfa=1, score=2.0)
    _add_signal(conn, "S_NOSCORE", "E_ACME", "account", "t_lead", days_ago(1),
                "Newest but unscored account signal", cfa=1, score=None)
    for i in range(1, 30):
        _add_signal(conn, f"S_R{i:02d}", "E_ACME", "account", "t_lead",
                    days_ago(i), f"Retracted peer card {i}", score=1.2,
                    status="retracted")


class TestSortControl(FeedTestBase):
    """B3 / R8.1: the feed must surface what deserves action, and must not lose
    the chosen view to the 120s auto-refresh."""

    seed_fn = staticmethod(seed_buried_top)

    def headlines(self, dom):
        return [h.strip() for h in _HEADLINE_RE.findall(dom)]

    def test_default_screen_leads_with_the_top_scoring_card(self):
        """The proving test: no paging, no control fiddling — the top-scoring
        active card is the first card on the default screen, even though it is
        the oldest and a newer, weaker card exists."""
        heads = self.headlines(self.home())
        self.assertEqual(heads[0], TOP_HEADLINE)
        self.assertIn("Newer but weaker account signal", heads)

    def test_unscored_cards_rank_last_not_first(self):
        heads = self.headlines(self.home())
        self.assertEqual(heads[-1], "Newest but unscored account signal")

    def test_date_order_buries_the_top_card_off_page_one(self):
        """Why the sort exists: at status=all the retracted backlog pushes the
        best card off the first screen entirely under date ordering."""
        dated = self.home(sort="recent", status="all")
        self.assertNotIn(TOP_HEADLINE, self.headlines(dated))
        self.assertIn("gs-loadmore", dated)
        scored = self.home(sort="score", status="all")
        self.assertEqual(self.headlines(scored)[0], TOP_HEADLINE)

    def test_auto_refresh_fragment_preserves_the_chosen_view(self):
        """The trap: the 120s poll hits /feed with the hx-include'd controls.
        The fragment must come back in the same view, not reset to the default."""
        dom = self.home(sort="recent", status="all")
        # both controls are outside the swapped body, so the poll can include them
        self.assertIn("hx-include=\"[name='status'],[name='sort']\"", dom)
        frag = self.client.get("/feed", params={"sort": "recent", "status": "all"})
        self.assertEqual(frag.status_code, 200)
        self.assertNotIn(TOP_HEADLINE, self.headlines(frag.text))
        self.assertIn("gs-loadmore", frag.text)
        frag = self.client.get("/feed", params={"sort": "score", "status": "all"})
        self.assertEqual(self.headlines(frag.text)[0], TOP_HEADLINE)

    def test_each_control_carries_the_other(self):
        dom = self.home(sort="recent", status="all")
        select_status = re.search(r'<select name="status".*?>', dom, re.DOTALL).group(0)
        select_sort = re.search(r'<select name="sort".*?>', dom, re.DOTALL).group(0)
        self.assertIn("[name='sort']", select_status)
        self.assertIn("[name='status']", select_sort)
        # the chosen sort round-trips into the control itself
        self.assertIn('<option value="recent" selected>', dom)

    def test_unknown_sort_falls_back_to_the_default_view(self):
        heads = self.headlines(self.home(sort="not-a-sort"))
        self.assertEqual(heads[0], TOP_HEADLINE)


def seed_unconfirmed(conn):
    """A single unconfirmed early-warning own-incident card (R10.5/R7.12)."""
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('own_incident','Own incident',5,270)")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector, richness, "
        "coverage_flag) VALUES ('E_ACME','Acme Energy','iou_electric','high',"
        "'edgar-visible')")
    conn.execute(
        "INSERT INTO source_policies (source_id, name, enabled, ttl, "
        "access_method, evidence_rank) VALUES ('ransomware_live','RW',1,86400,'api',3)")
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, payload, url) "
        "VALUES ('re_acc','ransomware_live', ?, '{}', 'http://leak/x')",
        (days_ago(3),))
    _add_signal(conn, "S_UNCONF", "E_ACME", "account", "own_incident", days_ago(3),
                "Listed on the AcmeLocker ransomware leak site - unverified early warning",
                cfa=0, score=3.0, incident_level="unconfirmed_early_warning")
    conn.execute(
        "INSERT INTO signal_evidence (signal_id, raw_event_id, evidence_text, "
        "evidence_locator, evidence_rank) VALUES ('S_UNCONF','re_acc',"
        "'ransomware.live lists Acme Energy as a victim.','victim', 3)")
    conn.executemany(
        "INSERT INTO badge_legend (badge_kind, code, label, description) "
        "VALUES (?,?,?,?)",
        [("evidence_quality", "IR", "Investor Report", "x")])


class UnconfirmedCardTests(FeedTestBase):
    seed_fn = staticmethod(seed_unconfirmed)

    def test_verification_first_treatment(self):
        dom = self.home(status="all")
        # the R10.5 verification-first warning + badge + card accent
        self.assertIn("No company, regulator, or SEC confirmation yet", dom)
        self.assertIn("gs-badge unconfirmed", dom)
        self.assertIn("tier-unconfirmed", dom)
        # outreach opener suppressed, verification-first note shown instead
        self.assertIn("Outreach withheld", dom)
        self.assertNotIn("licensing review may be timely", dom)


if __name__ == "__main__":
    unittest.main()
