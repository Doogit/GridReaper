"""Account 360 tests for the FastAPI web UI (R8.3, R6.6, R7.6, R9.1, R4.3).

The TestClient port of tests/test_ui_account.py: same hermetic fixture (temp-file
SQLite via apply_migrations, pointed at the app through GRIDSIGNALS_DB, no
network), same trust assertions carried across the framework change — the header
identifiers/coverage/gov-cloud render, parent/child relationships link to their
own account page, an entity with a signal renders a Timeline row and a Signals
card (reusing the feed card), a dark zero-signal entity renders honest empty
tabs with low-coverage framing and no crash, the selector lists active entities,
and switching accounts via the HTMX body swap works. Price is never in the DOM.
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.db.migrate import apply_migrations
from app.ui_web import render
from app.ui_web.app import app

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
# A non-primary price string that must NEVER reach the DOM (R4.3/R7.11).
FORBIDDEN_PRICE = "$2/GB"


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def seed(conn):
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_lead','Leadership',4,90)")

    # Acme (parent, edgar-visible, has ids + an account signal), its subsidiary
    # (child, dark), and a dark zero-signal muni.
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, cik, ticker, "
        "subsector, parent_id, richness, coverage_flag, gov_cloud_likelihood, "
        "tenant_cloud_environment) VALUES "
        "('E_ACME','Acme Energy','0000123','ACME','iou_electric',NULL,'high',"
        "'edgar-visible','possible','commercial')")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector, parent_id, "
        "richness, coverage_flag, gov_cloud_likelihood, tenant_cloud_environment)"
        " VALUES ('E_SUB','Acme Grid Sub','iou_electric','E_ACME','medium',"
        "'dark','unknown','unknown')")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector, parent_id, "
        "richness, coverage_flag, gov_cloud_likelihood, "
        "tenant_cloud_environment, active) VALUES "
        "('E_SUB&BR','Ampersand Grid Sub','iou_electric','E_ACME','medium',"
        "'dark','unknown','unknown',0)")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector, parent_id, "
        "richness, coverage_flag, gov_cloud_likelihood, tenant_cloud_environment)"
        " VALUES ('E_DARK','Dark Muni Co','muni_public',NULL,'low','dark',"
        "'likely','gcc_high')")
    conn.execute(
        "INSERT INTO entity_relationships (parent_entity_id, child_entity_id, "
        "relationship_type) VALUES ('E_ACME','E_SUB','subsidiary')")
    conn.execute(
        "INSERT INTO entity_relationships (parent_entity_id, child_entity_id, "
        "relationship_type) VALUES ('E_ACME','E_SUB&BR','subsidiary')")

    # product + play + a primary fact for the snapshot provenance
    conn.execute("INSERT INTO products (product_id, name) VALUES "
                 "('p_sentinel','Microsoft Sentinel')")
    conn.execute(
        "INSERT INTO license_play_candidates (play_id, trigger_id, product_id, "
        "recommended_path, discovery_question) VALUES "
        "('play1','t_lead','p_sentinel','Adopt E5 grant','What is your SIEM?')")
    conn.execute(
        "INSERT INTO license_facts (fact_id, product_id, segment, price_note, "
        "source_quality, source_url, verified_date) VALUES "
        "('f_primary','p_sentinel','commercial',?,'primary',"
        "'http://primary','2026-07-01')", (FORBIDDEN_PRICE,))

    # raw event -> account signal on E_ACME (evidence + snapshot)
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, payload, "
        "url) VALUES ('re_acc',NULL,'2026-07-27',"
        "'{\"title\": \"Acme names new CISO\"}','http://acme/8k')")
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, signal_scope, "
        "trigger_id, event_date, headline, evidence_snippet, source_url, "
        "confidence, evidence_quality, customer_facing_allowed, score, status) "
        "VALUES ('S_ACC1','re_acc','E_ACME','account','t_lead','2026-07-27',"
        "'Acme names new CISO','Acme names new CISO','http://acme/8k',0.9,'IR',"
        "1,4.0,'active')")
    conn.execute(
        "INSERT INTO signal_evidence (signal_id, raw_event_id, evidence_text, "
        "evidence_locator, evidence_rank) VALUES ('S_ACC1','re_acc',"
        "'Acme Energy appointed a new Chief Information Security Officer.',"
        "'para 2', 1)")
    conn.execute(
        "INSERT INTO license_play_snapshots (signal_id, play_id, fact_ids, "
        "generated_at, generation_version, display_text, outreach_safe_text) "
        "VALUES ('S_ACC1','play1','[\"f_primary\"]', ?, 'plays/1.0', "
        "'Recommended path: Adopt E5 grant', "
        "'Given the recent development, a licensing check may be timely.')",
        (iso(NOW),))

    # regulatory obligations for the Compliance Calendar tab. Dates are far
    # past / far future so "in effect" vs "upcoming" holds without the test
    # depending on the wall clock (the route reads the real today).
    conn.executemany(
        "INSERT INTO regulatory_obligations (obligation_id, source_url, "
        " regulator, rule_name, affected_scope, applicability_rule, "
        " effective_date, compliance_date, mapped_products, verified_at, "
        " signal_id, derived_at) VALUES (?,?,?,?,?,?,?,NULL,?,NULL,'S_ACC1',?)",
        [("ob_cip", "http://fr/cip", "Federal Energy Regulatory Commission",
          "CIP-003-11 Security Management Controls",
          "NERC-registered BES owners and operators (registration not "
          "verified per account)", "subsector_in:iou_electric;muni_public",
          "2020-01-01", "p_sentinel", iso(NOW)),
         ("ob_future", "http://fr/virt",
          "Federal Energy Regulatory Commission",
          "Virtualization Reliability Standards",
          "NERC-registered BES owners and operators",
          "subsector_in:iou_electric", "2099-01-01", None, iso(NOW)),
         ("ob_pipe", "http://fr/tsa",
          "Transportation Security Administration",
          "Pipeline Security Directive",
          "TSA-designated critical pipeline owners and operators",
          "subsector_in:lng;midstream", "2021-01-01", None, iso(NOW))])

    # badge legend the card reads labels from
    conn.executemany(
        "INSERT INTO badge_legend (badge_kind, code, label, description) "
        "VALUES (?,?,?,?)",
        [("evidence_quality", "IR", "Investor Report", "From an investor filing"),
         ("source_quality", "non-primary", "Non-primary",
          "Not an authoritative source (R4.3)")])


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


class AccountTestBase(unittest.TestCase):
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

    def page(self, **params):
        resp = self.client.get("/account", params=params)
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def body(self, **params):
        resp = self.client.get("/account/body", params=params)
        self.assertEqual(resp.status_code, 200)
        return resp.text


class TestSelector(AccountTestBase):
    def test_selector_lists_all_active_entities(self):
        dom = self.page()
        # name-ordered: Acme Energy, Acme Grid Sub, Dark Muni Co
        self.assertEqual(dom.count("<option "), 3)
        self.assertIn("Acme Energy  ·  E_ACME", dom)
        self.assertIn("Dark Muni Co  ·  E_DARK", dom)

    def test_default_selection_is_first_entity(self):
        dom = self.page()
        # default (no entity_id) resolves to the first, name-ordered
        self.assertIn('value="E_ACME" selected', dom)


class TestHeader(AccountTestBase):
    def test_header_identifiers_subsector_coverage_gov(self):
        dom = self.page(entity_id="E_ACME")
        self.assertIn("Acme Energy", dom)
        self.assertIn("CIK 0000123", dom)          # identifier line
        self.assertIn("iou_electric", dom)          # subsector badge
        self.assertIn("possible", dom)              # gov-cloud likelihood
        self.assertIn("entity_id E_ACME", dom)

    def test_parent_links_to_child_account(self):
        dom = self.page(entity_id="E_ACME")
        # Acme's subsidiary is linked by its own account URL
        self.assertIn('href="/account?entity_id=E_SUB"', dom)
        self.assertIn("Acme Grid Sub", dom)

    def test_child_page_links_to_parent(self):
        dom = self.page(entity_id="E_SUB")
        self.assertIn("Parent:", dom)
        self.assertIn('href="/account?entity_id=E_ACME"', dom)
        self.assertIn("Acme Energy", dom)


class TestTabs(AccountTestBase):
    def test_entity_with_signal_renders_timeline_and_card(self):
        dom = self.page(entity_id="E_ACME")
        # timeline row + card headline both come from the seeded account signal
        self.assertIn("Acme names new CISO", dom)
        # the reused signal card rendered (play provenance in the card body)
        self.assertIn('class="gs-card', dom)
        self.assertIn("Adopt E5 grant", dom)
        # feedback flow reused verbatim from the feed card
        self.assertIn('hx-post="/feedback"', dom)

    def test_price_never_in_dom(self):
        dom = self.page(entity_id="E_ACME")
        self.assertNotIn(FORBIDDEN_PRICE, dom)      # R4.3/R7.11

    def test_dark_zero_signal_entity_low_coverage_honest_empty_no_crash(self):
        dom = self.page(entity_id="E_DARK")
        self.assertIn("Dark Muni Co", dom)
        self.assertIn("muni_public", dom)
        self.assertIn("likely", dom)                # gov-cloud posture shown
        # low-coverage framing, never "no activity"
        self.assertIn("low coverage", dom)
        # honest empty tabs (both Timeline and Signals), no card, no exception
        self.assertIn("low volume by design", dom)
        self.assertNotIn('class="gs-card', dom)


class TestProductsTab(AccountTestBase):
    """R8.3 13a: stored plays for the account's OWN signals, snapshots only."""

    def test_tab_label_and_play_render(self):
        dom = self.page(entity_id="E_ACME")
        self.assertIn(">Products</label>", dom)
        self.assertIn("Microsoft Sentinel", dom)
        self.assertIn("Recommended path: Adopt E5 grant", dom)
        self.assertIn("What is your SIEM?", dom)

    def test_play_links_back_to_its_card(self):
        dom = self.page(entity_id="E_ACME")
        self.assertIn(f'/card/{render.card_key("S_ACC1")}', dom)

    def test_empty_products_tab_names_its_reason(self):
        dom = self.page(entity_id="E_DARK")
        self.assertIn(">Products</label>", dom)
        self.assertIn("No licensing plays for this account yet", dom)
        # no play text leaks in; the product NAME may still appear elsewhere on
        # the page (the Calendar tab maps it to an obligation), so assert on the
        # play's own strings rather than the product name
        self.assertNotIn("Recommended path: Adopt E5 grant", dom)
        self.assertNotIn("What is your SIEM?", dom)


class TestComplianceCalendarTab(AccountTestBase):
    """R8.3 13b: class-scoped obligations, effective dates only (R4.1)."""

    def test_tab_label_and_matching_obligations_render(self):
        dom = self.page(entity_id="E_ACME")
        self.assertIn(">Compliance Calendar</label>", dom)
        self.assertIn("CIP-003-11 Security Management Controls", dom)
        self.assertIn("Virtualization Reliability Standards", dom)
        self.assertIn("Federal Energy Regulatory Commission", dom)

    def test_effective_and_upcoming_are_distinguished(self):
        dom = self.page(entity_id="E_ACME")
        self.assertIn("effective 2020-01-01", dom)
        self.assertIn(">in effect<", dom)
        self.assertIn(">upcoming<", dom)

    def test_no_compliance_deadline_is_shown_or_derived(self):
        """Every derived obligation leaves compliance_date NULL; the tab says so
        in words and must never present the effective date as a deadline."""
        dom = self.page(entity_id="E_ACME")
        # both rendered obligations disclose the absence, and neither relabels
        # its effective date as a compliance date
        self.assertEqual(
            dom.count(f"compliance date {render.COMPLIANCE_DATE_UNKNOWN}"), 2)
        self.assertNotIn("compliance date 2020-01-01", dom)
        self.assertNotIn("compliance date 2099-01-01", dom)

    def test_other_class_obligation_is_not_shown(self):
        dom = self.page(entity_id="E_ACME")
        self.assertNotIn("Pipeline Security Directive", dom)

    def test_coverage_line_states_what_was_checked(self):
        dom = self.page(entity_id="E_ACME")
        self.assertIn("Checked 3 stored obligations; 2 apply to subsector "
                      "&#39;iou_electric&#39;.", dom)

    def test_subsector_match_is_exact_not_substring(self):
        dom = self.page(entity_id="E_DARK")
        self.assertIn("CIP-003-11 Security Management Controls", dom)
        self.assertNotIn("Virtualization Reliability Standards", dom)
        self.assertIn("Checked 3 stored obligations; 1 apply to subsector "
                      "&#39;muni_public&#39;.", dom)

    def test_calendar_is_labelled_as_a_class_filter_not_a_registration_claim(self):
        dom = self.page(entity_id="E_ACME")
        self.assertIn("does not assert that this account is a registered "
                      "entity", dom)

    def test_empty_calendar_when_no_obligation_applies(self):
        conn = sqlite3.connect(self.path)
        conn.execute("DELETE FROM regulatory_obligations")
        conn.commit()
        conn.close()
        dom = self.page(entity_id="E_ACME")
        # the class wording, which the exclusion wording must not cannibalize
        self.assertIn("No regulatory obligations apply to this account&#39;s "
                      "subsector class.", dom)
        self.assertIn("Checked 0 stored obligations", dom)
        self.assertNotIn("exclude this account by name", dom)

    # The reader only evaluates exclusion clauses naming ids in the canonical
    # 'E' + four digits form the producer writes; the fixture's own ids are not
    # in that form, so the excluded account is added here rather than reused.
    EXCLUDED_ENTITY_ID = "E0999"

    def _exclude_from_both_iou_obligations(self):
        """Narrow both iou_electric rules so they name one account. E0999 then
        shares E_ACME's subsector label and matches nothing."""
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, subsector, "
            "richness, coverage_flag, gov_cloud_likelihood, "
            "tenant_cloud_environment) VALUES "
            "(?,'Canonical Grid Co','iou_electric','medium','dark','unknown',"
            "'unknown')", (self.EXCLUDED_ENTITY_ID,))
        conn.execute(
            "UPDATE regulatory_obligations SET applicability_rule = "
            "applicability_rule || '|exclude_entities:' || ? "
            "WHERE obligation_id IN ('ob_cip','ob_future')",
            (self.EXCLUDED_ENTITY_ID,))
        conn.commit()
        conn.close()

    def test_individually_excluded_account_says_so_in_the_coverage_line(self):
        """Two accounts in one subsector must not make contradictory claims
        about that subsector with nothing explaining the difference (R4.1)."""
        self._exclude_from_both_iou_obligations()
        dom = self.page(entity_id=self.EXCLUDED_ENTITY_ID)
        self.assertIn("Checked 3 stored obligations; 0 apply to subsector "
                      "&#39;iou_electric&#39;.", dom)
        self.assertIn("2 apply to that subsector but exclude this account by "
                      "name (not a plausible owner or operator of the "
                      "regulated assets) and are not shown.", dom)
        # the same-subsector account they DO bind still reads unchanged
        self.assertIn("Checked 3 stored obligations; 2 apply to subsector "
                      "&#39;iou_electric&#39;.", self.page(entity_id="E_ACME"))

    def test_excluded_account_empty_state_does_not_deny_the_class(self):
        self._exclude_from_both_iou_obligations()
        dom = self.page(entity_id=self.EXCLUDED_ENTITY_ID)
        self.assertNotIn("No regulatory obligations apply to this account&#39;s "
                         "subsector class.", dom)
        self.assertIn("Ones that apply to its subsector class exclude this "
                      "account by name", dom)
        # the exclusion is about BES role, never about registration (R4.1)
        self.assertIn("not a plausible owner or operator of the regulated "
                      "assets", dom)
        self.assertNotIn("not registered", dom)

    def test_caption_no_longer_claims_subsector_only_matching(self):
        dom = self.page(entity_id="E_ACME")
        self.assertIn("may also exclude named accounts within that class", dom)
        self.assertNotIn("regulatory obligations that apply to this "
                         "account&#39;s subsector class. This is an "
                         "effective-date calendar", dom)


class TestEntityGraphTab(AccountTestBase):
    """R8.3 13c: a sourced relationship list, not a drawn graph."""

    def test_tab_label_and_sourced_edge_render(self):
        dom = self.page(entity_id="E_ACME")
        self.assertIn(">Entity Graph</label>", dom)
        self.assertIn("Subsidiary of this account", dom)
        self.assertIn("subsidiary", dom)
        self.assertIn('href="/account?entity_id=E_SUB"', dom)

    def test_graph_account_links_urlencode_entity_ids(self):
        dom = self.page(entity_id="E_ACME")
        self.assertIn('href="/account?entity_id=E_SUB%26BR"', dom)
        self.assertNotIn('href="/account?entity_id=E_SUB&amp;BR"', dom)

    def test_unsourced_edge_provenance_is_visible_as_such(self):
        """The seeded edge has no source and no verified_at; the tab must show
        that rather than leaving blank cells that read as verified."""
        dom = self.page(entity_id="E_ACME")
        self.assertIn("not recorded", dom)
        self.assertIn("not verified", dom)

    def test_child_page_shows_the_parent_edge(self):
        dom = self.page(entity_id="E_SUB")
        self.assertIn("Parent of this account", dom)

    def test_empty_graph_says_no_edge_sourced_not_independent(self):
        dom = self.page(entity_id="E_DARK")
        self.assertIn(">Entity Graph</label>", dom)
        self.assertIn("No sourced corporate relationships recorded", dom)
        self.assertIn("not that this account is independent", dom)


class TestBodySwap(AccountTestBase):
    def test_body_partial_switches_entity_without_page_chrome(self):
        # the HTMX entity-switch path returns just the body
        dom = self.body(entity_id="E_SUB")
        self.assertNotIn("gs-topbar", dom)          # body only, no page chrome
        self.assertIn("Acme Grid Sub", dom)
        self.assertIn('href="/account?entity_id=E_ACME"', dom)  # parent link

    def test_body_unknown_entity_falls_back_to_first(self):
        dom = self.body(entity_id="does_not_exist")
        # resolves to the first active entity rather than crashing
        self.assertIn("Acme Energy", dom)

    def test_switching_across_all_entities_never_500s(self):
        for eid in ("E_ACME", "E_SUB", "E_DARK"):
            resp = self.client.get("/account/body", params={"entity_id": eid})
            self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
