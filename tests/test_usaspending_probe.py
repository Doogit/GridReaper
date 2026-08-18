"""USAspending.gov spike tests (R1, R4 of the combo-engine/account-signal
plan).

Hermetic: real migrations against SQLite, canned USAspending JSON fixtures
shaped like the real ``spending_by_award`` response, no network.
``TestFetchAgency``/``TestCollect`` monkeypatch ``probe.fetch_page`` (and
``probe.time.sleep``) so the pagination/politeness logic is exercised without
ever touching the network.
"""
import http.client
import io
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db.migrate import apply_migrations
from app.spikes import usaspending_probe as probe

REPO = Path(__file__).resolve().parent.parent
PIPELINE = REPO / "deploy" / "ingest_pipeline.sh"
MODULE_SRC = Path(probe.__file__).read_text(encoding="utf-8")


def _network_call_forbidden(*args, **kwargs):
    raise AssertionError(
        "unexpected live network call in a hermetic test -- every test that "
        "reaches collect()/report()/fetch_agency*() must monkeypatch "
        "probe.fetch_page or probe.fetch_agency first")


def setUpModule():
    """Reviewer-caught (testing): the hermeticity claim ('zero live network
    calls') was previously proven only by an ad hoc run outside this file,
    not by an assertion inside it. This module-level guard makes it
    self-enforcing: any test that forgets to stub fetch_page/fetch_agency
    before reaching real urllib now fails loudly and locally instead of
    attempting (or silently succeeding at) a real HTTP call."""
    global _ORIG_URLOPEN
    _ORIG_URLOPEN = urllib.request.urlopen
    urllib.request.urlopen = _network_call_forbidden


def tearDownModule():
    urllib.request.urlopen = _ORIG_URLOPEN

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)   # cutoff 2024-08-18

ENTITIES = [
    ("E0001", "NextEra Energy", "0000753308", "NEE"),
    ("E0002", "Duke Energy Indiana", "0000020290", "DEI"),
    ("E0004", "Dominion Energy", "0000715957", "D"),
    ("E0005", "American Electric Power", "0000004904", "AEP"),
    ("E0006", "Southern Company", "0000092122", "SO"),
    ("E0007", "Entergy Corporation", "0000065984", "ETR"),
    ("E0008", "PG&E", "0001004980", "PCG"),
]
ALIASES = [("E0008", "Pacific Gas and Electric")]
COLLISIONS = [("E0004", "Dominion")]


def seed(conn):
    for eid, name, cik, ticker in ENTITIES:
        conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, cik, ticker) "
            "VALUES (?, ?, ?, ?)", (eid, name, cik, ticker))
    for eid, alias in ALIASES:
        conn.execute("INSERT INTO entity_aliases (entity_id, alias) "
                     "VALUES (?, ?)", (eid, alias))
    for eid, term in COLLISIONS:
        conn.execute("INSERT INTO entity_collision_terms (entity_id, term) "
                     "VALUES (?, ?)", (eid, term))
    conn.commit()


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    seed(conn)
    return conn


def award(award_id, recipient, agency, start_date, description="",
          subagency=""):
    return {"Award ID": award_id, "Recipient Name": recipient,
            "Awarding Agency": agency, "Awarding Sub Agency": subagency,
            "Start Date": start_date, "Description": description}


SIX_DISTINCT_JSON = json.dumps({"results": [
    # 6 distinct watchlist accounts, in-window, qualifying agency.
    award("DE001", "NEXTERA ENERGY INC", "Department of Energy",
          "2026-01-15",
          "GRID RESILIENCE AND INNOVATION PARTNERSHIPS PROGRAM AWARD FOR "
          "TRANSMISSION HARDENING AND SUBSTATION MODERNIZATION"),
    award("DE002", "DUKE ENERGY INDIANA LLC", "Department of Energy",
          "2025-06-01",
          "COOPERATIVE AGREEMENT FOR ADVANCED GRID MONITORING AND STORM "
          "HARDENING INFRASTRUCTURE ACROSS THE INDIANA SERVICE TERRITORY"),
    # alias-driven match (KTD5 shape: legal filing name via entity_aliases,
    # not the watchlist row's own short name "PG&E") -- also a DHS+CISA
    # sub-agency award, proving the CISA-subagency qualifying path.
    award("DH001", "PACIFIC GAS AND ELECTRIC COMPANY",
          "Department of Homeland Security", "2025-09-09",
          "GRANT FUNDING OPERATIONAL TECHNOLOGY SECURITY UPGRADES ACROSS "
          "DISTRIBUTION SUBSTATIONS",
          subagency="Cybersecurity and Infrastructure Security Agency"),
    award("DE003", "DOMINION ENERGY INC", "Department of Energy",
          "2026-03-01",
          "PROJECT GRANT FOR OFFSHORE TRANSMISSION INTERCONNECTION "
          "ENGINEERING AND DESIGN STUDIES"),
    award("DE004", "AMERICAN ELECTRIC POWER COMPANY INC",
          "Department of Energy", "2024-09-01",
          "COOPERATIVE AGREEMENT FOR TRANSMISSION RESILIENCE PLANNING AND "
          "VEGETATION MANAGEMENT MODERNIZATION"),
    award("DH002", "SOUTHERN COMPANY", "Department of Homeland Security",
          "2025-12-12",
          "GRANT FOR PHYSICAL SECURITY HARDENING AT GENERATION FACILITIES"),
    # non-qualifying funding agency -- Entergy is on the watchlist but this
    # award must NOT count toward the distinct-account total.
    award("DOD001", "ENTERGY CORPORATION", "Department of Defense",
          "2026-01-01", "MICROGRID RESILIENCE DEMONSTRATION PROJECT"),
    # off-list recipient -- unmatched, must not count.
    award("DE005", "ZEPHYR SOLAR COOPERATIVE", "Department of Energy",
          "2026-01-01", "COMMUNITY SOLAR DEMONSTRATION GRANT"),
    # fuzzy near-miss -- must review-queue, never silently auto-match.
    award("DE006", "AMERICAN ELECTRIC POWER OF OHIO",
          "Department of Energy", "2025-05-05",
          "SUBSIDIARY-LEVEL GRID MODERNIZATION PLANNING GRANT"),
    # out-of-window -- must not affect the count either way.
    award("DE007", "NEXTERA ENERGY INC", "Department of Energy",
          "2019-01-01", "OLD AWARD OUTSIDE THE TRAILING 24-MONTH WINDOW"),
]})

THREE_DISTINCT_JSON = json.dumps({"results": [
    award("DE101", "NEXTERA ENERGY INC", "Department of Energy",
          "2026-01-15", "GRID RESILIENCE AWARD"),
    award("DE102", "DUKE ENERGY INDIANA LLC", "Department of Energy",
          "2025-06-01", "STORM HARDENING COOPERATIVE AGREEMENT"),
    award("DE103", "DOMINION ENERGY INC", "Department of Energy",
          "2026-03-01", "TRANSMISSION INTERCONNECTION STUDY GRANT"),
]})

ZERO_RESULTS_JSON = json.dumps({"results": []})


class TestParseAwards(unittest.TestCase):
    def test_parses_recognised_fields_only(self):
        records = probe.parse_awards(SIX_DISTINCT_JSON)
        self.assertEqual(records[0], probe.AwardRecord(
            award_id="DE001", recipient_name="NEXTERA ENERGY INC",
            awarding_agency="Department of Energy", awarding_subagency="",
            award_date="2026-01-15",
            description="GRID RESILIENCE AND INNOVATION PARTNERSHIPS "
                        "PROGRAM AWARD FOR TRANSMISSION HARDENING AND "
                        "SUBSTATION MODERNIZATION"))

    def test_row_missing_recipient_name_is_dropped(self):
        text = json.dumps({"results": [
            {"Award ID": "X1", "Awarding Agency": "Department of Energy",
             "Start Date": "2026-01-01", "Description": "no recipient"}]})
        self.assertEqual(probe.parse_awards(text), [])

    def test_row_missing_awarding_agency_is_dropped(self):
        text = json.dumps({"results": [
            {"Award ID": "X1", "Recipient Name": "Acme Power",
             "Start Date": "2026-01-01", "Description": "no agency"}]})
        self.assertEqual(probe.parse_awards(text), [])

    def test_non_dict_result_rows_are_skipped(self):
        text = json.dumps({"results": ["not a dict", None, 5]})
        self.assertEqual(probe.parse_awards(text), [])

    def test_missing_results_key_raises_with_observed_keys(self):
        with self.assertRaises(ValueError) as ctx:
            probe.parse_awards(json.dumps({"page_metadata": {}}))
        self.assertIn("page_metadata", str(ctx.exception))

    def test_results_not_a_list_raises(self):
        with self.assertRaises(ValueError):
            probe.parse_awards(json.dumps({"results": "oops"}))

    def test_zero_results_parses_cleanly(self):
        self.assertEqual(probe.parse_awards(ZERO_RESULTS_JSON), [])

    def test_non_string_field_value_drops_the_row_instead_of_crashing(self):
        """Reviewer-caught: a schema tweak that sends a non-string value for
        a recognized field (nested object, list, number) must not raise --
        _clean() treats it as absent text and the row is dropped by the
        same missing-required-field path, not a crash that would abort the
        whole live run partway through."""
        text = json.dumps({"results": [
            {"Award ID": 12345, "Recipient Name": "Acme Power",
             "Awarding Agency": "Department of Energy",
             "Start Date": "2026-01-01",
             "Description": {"nested": "object, not a string"}}]})
        records = probe.parse_awards(text)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].award_id, "")
        self.assertEqual(records[0].description, "")

    def test_non_string_recipient_name_drops_the_row(self):
        text = json.dumps({"results": [
            {"Award ID": "X1", "Recipient Name": ["not", "a", "string"],
             "Awarding Agency": "Department of Energy",
             "Start Date": "2026-01-01"}]})
        self.assertEqual(probe.parse_awards(text), [])


class TestQualifyingAgency(unittest.TestCase):
    def _award(self, agency, subagency=""):
        return probe.AwardRecord("A1", "Acme Power", agency, subagency,
                                 "2026-01-01", "desc")

    def test_doe_toptier_qualifies(self):
        self.assertTrue(probe.is_qualifying_agency(
            self._award("Department of Energy")))

    def test_dhs_toptier_qualifies(self):
        self.assertTrue(probe.is_qualifying_agency(
            self._award("Department of Homeland Security")))

    def test_cisa_subagency_qualifies_even_under_a_different_toptier_label(self):
        self.assertTrue(probe.is_qualifying_agency(self._award(
            "Some Other Toptier Label",
            subagency="Cybersecurity and Infrastructure Security Agency")))

    def test_department_of_defense_does_not_qualify(self):
        self.assertFalse(probe.is_qualifying_agency(
            self._award("Department of Defense")))

    def test_toptier_match_is_case_insensitive(self):
        """Reviewer-caught: the CISA sub-agency check is already
        casefold()-insensitive; the toptier check must match that
        discipline rather than requiring exact-case agreement with
        live-observed casing."""
        self.assertTrue(probe.is_qualifying_agency(
            self._award("department of energy")))
        self.assertTrue(probe.is_qualifying_agency(
            self._award("DEPARTMENT OF HOMELAND SECURITY")))


class TestWindow(unittest.TestCase):
    def test_cutoff_is_the_24_month_anniversary(self):
        self.assertEqual(probe.window_cutoff(NOW), "2024-08-18")

    def test_leap_day_cutoff_falls_back_to_feb_28(self):
        leap = datetime(2028, 2, 29, tzinfo=timezone.utc)
        self.assertEqual(probe.window_cutoff(leap), "2026-02-28")


class TestAnalysisSixDistinct(unittest.TestCase):
    """THE PINNED THRESHOLD CASE: 6 distinct qualifying accounts clears
    BUILD_THRESHOLD (5) -> recommends building U4."""

    @classmethod
    def setUpClass(cls):
        cls.conn = fixture_conn()
        awards = probe.parse_awards(SIX_DISTINCT_JSON)
        cls.result = probe.analyze(cls.conn, awards,
                                   source_status={
                                       "Department of Energy": "ok",
                                       "Department of Homeland Security": "ok"},
                                   now=NOW)
        cls.text = probe.format_report(cls.result)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_distinct_account_count_is_six(self):
        self.assertEqual(self.result["distinct_account_count"], 6)
        self.assertEqual(
            [h["entity_id"] for h in self.result["hits"]],
            ["E0001", "E0002", "E0004", "E0005", "E0006", "E0008"])

    def test_recommends_build(self):
        self.assertIn("BUILD the capital_project trigger (U4)",
                      self.result["recommendation"])
        self.assertNotIn("DO NOT BUILD", self.result["recommendation"])

    def test_alias_variant_resolves_to_the_correct_entity(self):
        """KTD5 shape: the legal filing name only matches via the curated
        alias table, not the watchlist row's own short name."""
        pge = next(h for h in self.result["hits"] if h["entity_id"] == "E0008")
        self.assertEqual(pge["recipient_names"],
                         ["PACIFIC GAS AND ELECTRIC COMPANY"])

    def test_non_qualifying_agency_award_is_excluded(self):
        """Entergy is on the watchlist, but its only in-window award is
        Department of Defense -- must not count as a hit."""
        self.assertNotIn("E0007", [h["entity_id"] for h in self.result["hits"]])
        self.assertEqual(self.result["excluded_agency_awards"], 1)

    def test_cisa_subagency_award_counts_as_qualifying(self):
        pge = next(h for h in self.result["hits"] if h["entity_id"] == "E0008")
        self.assertEqual(pge["award_count"], 1)

    def test_off_list_recipient_is_unmatched_not_counted(self):
        self.assertEqual(self.result["unmatched_count"], 1)
        self.assertNotIn("ZEPHYR SOLAR COOPERATIVE", self.text)

    def test_fuzzy_near_miss_is_review_queued_not_auto_matched(self):
        review = {r["recipient_name"]: r for r in self.result["review"]}
        self.assertIn("AMERICAN ELECTRIC POWER OF OHIO", review)
        eid = review["AMERICAN ELECTRIC POWER OF OHIO"]["candidates"][0][0]
        self.assertEqual(eid, "E0005")
        self.assertNotIn("AMERICAN ELECTRIC POWER OF OHIO",
                         [n for h in self.result["hits"]
                          for n in h["recipient_names"]])

    def test_out_of_window_award_does_not_affect_the_count(self):
        self.assertEqual(self.result["undated_awards"], 0)
        nextera = next(h for h in self.result["hits"] if h["entity_id"] == "E0001")
        self.assertEqual(nextera["award_count"], 1)

    def test_report_states_the_count_and_definition(self):
        self.assertIn("DISTINCT ACCOUNT COUNT: 6", self.text)
        self.assertIn("trailing 24 months", self.text)
        self.assertIn("award start date >= 2024-08-18", self.text)

    def test_report_wording_is_ascii(self):
        self.text.encode("ascii")


class TestAnalysisCollisionAndDedup(unittest.TestCase):
    """Reviewer-caught gaps: (1) COLLISIONS was seeded into every fixture
    but never actually driven through analyze() -- the collision_term
    review path was untested despite the fixture existing for it. (2) a
    recipient with more than one qualifying award was previously resolved
    and appended to review/unmatched once PER AWARD, inflating those
    printed counts; analyze() now resolves once per distinct recipient
    name."""

    def test_bare_collision_term_is_review_queued_never_auto_matched(self):
        """"DOMINION" alone is a collision term (R6.3): it must never
        auto-match E0004 on its own, even though it equals the entity's
        alias-free positive term after normalization."""
        conn = fixture_conn()
        try:
            awards = probe.parse_awards(json.dumps({"results": [
                award("DE900", "DOMINION", "Department of Energy",
                      "2026-01-01", "UNRELATED PROGRAM DESCRIPTION")]}))
            result = probe.analyze(conn, awards, now=NOW)
        finally:
            conn.close()
        self.assertNotIn("E0004", [h["entity_id"] for h in result["hits"]])
        review = {r["recipient_name"]: r for r in result["review"]}
        self.assertIn("DOMINION", review)
        self.assertEqual(review["DOMINION"]["reason"], "collision_term")

    def test_repeated_unmatched_recipient_is_counted_once_not_per_award(self):
        conn = fixture_conn()
        try:
            awards = probe.parse_awards(json.dumps({"results": [
                award("DE901", "ZEPHYR SOLAR COOPERATIVE",
                      "Department of Energy", "2026-01-01", "Award one"),
                award("DE902", "ZEPHYR SOLAR COOPERATIVE",
                      "Department of Energy", "2026-02-01", "Award two"),
            ]}))
            result = probe.analyze(conn, awards, now=NOW)
        finally:
            conn.close()
        self.assertEqual(result["unmatched_count"], 1)

    def test_repeated_review_queued_recipient_is_listed_once_not_per_award(self):
        conn = fixture_conn()
        try:
            awards = probe.parse_awards(json.dumps({"results": [
                award("DE903", "AMERICAN ELECTRIC POWER OF OHIO",
                      "Department of Energy", "2026-01-01", "Award one"),
                award("DE904", "AMERICAN ELECTRIC POWER OF OHIO",
                      "Department of Energy", "2026-02-01", "Award two"),
            ]}))
            result = probe.analyze(conn, awards, now=NOW)
        finally:
            conn.close()
        self.assertEqual(len(result["review"]), 1)

    def test_repeated_matched_recipient_still_counts_every_award(self):
        """The per-award-name dedup must not lose award_count for a
        genuinely matched, repeatedly-awarded entity."""
        conn = fixture_conn()
        try:
            awards = probe.parse_awards(json.dumps({"results": [
                award("DE905", "NEXTERA ENERGY INC", "Department of Energy",
                      "2026-01-01", "Award one"),
                award("DE906", "NEXTERA ENERGY INC", "Department of Energy",
                      "2026-02-01", "Award two"),
            ]}))
            result = probe.analyze(conn, awards, now=NOW)
        finally:
            conn.close()
        self.assertEqual(result["distinct_account_count"], 1)
        self.assertEqual(result["hits"][0]["award_count"], 2)


class TestAnalysisThreeDistinct(unittest.TestCase):
    """3 distinct accounts is below BUILD_THRESHOLD (5) -> do-not-build."""

    @classmethod
    def setUpClass(cls):
        cls.conn = fixture_conn()
        awards = probe.parse_awards(THREE_DISTINCT_JSON)
        cls.result = probe.analyze(cls.conn, awards,
                                   source_status={"Department of Energy": "ok"},
                                   now=NOW)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_distinct_account_count_is_three(self):
        self.assertEqual(self.result["distinct_account_count"], 3)

    def test_recommends_do_not_build(self):
        self.assertIn("DO NOT BUILD the capital_project trigger (U4)",
                      self.result["recommendation"])


class TestZeroResults(unittest.TestCase):
    """A zero-result fixture reports zero cleanly -- no crash, and never
    conflated with a source that failed to fetch."""

    def test_zero_results_report_cleanly(self):
        conn = fixture_conn()
        try:
            awards = probe.parse_awards(ZERO_RESULTS_JSON)
            result = probe.analyze(
                conn, awards,
                source_status={"Department of Energy": "ok",
                               "Department of Homeland Security": "ok"},
                now=NOW)
        finally:
            conn.close()
        self.assertEqual(result["distinct_account_count"], 0)
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["total_awards"], 0)
        self.assertIn("DO NOT BUILD", result["recommendation"])
        text = probe.format_report(result)
        text.encode("ascii")
        self.assertIn("DISTINCT ACCOUNT COUNT: 0", text)

    def test_a_failed_agency_is_not_a_measured_zero(self):
        conn = fixture_conn()
        try:
            result = probe.analyze(
                conn, [],
                source_status={"Department of Energy": "FAILED (HTTPError)",
                               "Department of Homeland Security": "ok"},
                now=NOW)
        finally:
            conn.close()
        self.assertIn("PARTIAL MEASUREMENT", result["recommendation"])
        self.assertIn("Department of Energy", result["recommendation"])


class TestRichnessSample(unittest.TestCase):
    """The unit's own additional requirement: sample and characterize
    description text length/content, independent of the account count."""

    def _award(self, description):
        return probe.AwardRecord("A", "Acme Power", "Department of Energy",
                                 "", "2026-01-01", description)

    def test_substantive_descriptions_are_characterized_as_such(self):
        awards = [self._award(d) for d in [
            "GRID RESILIENCE AND INNOVATION PARTNERSHIPS PROGRAM AWARD FOR "
            "TRANSMISSION HARDENING AND SUBSTATION MODERNIZATION",
            "COOPERATIVE AGREEMENT FOR ADVANCED GRID MONITORING AND STORM "
            "HARDENING INFRASTRUCTURE ACROSS THE SERVICE TERRITORY",
            "PROJECT GRANT FOR OFFSHORE TRANSMISSION INTERCONNECTION "
            "ENGINEERING AND DESIGN STUDIES",
        ]]
        result = probe.richness_sample(awards)
        self.assertIn("SUBSTANTIVE", result["characterization"])
        self.assertEqual(result["boilerplate_count"], 0)
        self.assertEqual(result["sampled"], 3)

    def test_boilerplate_descriptions_are_characterized_as_such(self):
        """The exact risk the unit brief calls out: a two-word program
        label clearing the account count while carrying no real free text."""
        awards = [self._award(d) for d in [
            "Pipeline Safety Grant", "Grid Modernization", "Storm Hardening",
            "Solar Grant", "Security Upgrade",
        ]]
        result = probe.richness_sample(awards)
        self.assertIn("BOILERPLATE-DOMINATED", result["characterization"])
        self.assertEqual(result["boilerplate_count"], 5)

    def test_exactly_threshold_word_count_is_not_boilerplate(self):
        """Pins the inclusive/exclusive boundary: BOILERPLATE_WORD_THRESHOLD
        (6) descriptions use ``wc < 6``, so an exactly-6-word description is
        NOT boilerplate. Every other fixture in this class was clearly
        short (2-3 words) or clearly long (10+), so a `<` vs `<=` flip
        would have passed the suite silently before this test."""
        self.assertEqual(probe.BOILERPLATE_WORD_THRESHOLD, 6)
        awards = [self._award("One two three four five six")]
        result = probe.richness_sample(awards)
        self.assertEqual(result["boilerplate_count"], 0)

    def test_one_below_threshold_word_count_is_boilerplate(self):
        awards = [self._award("One two three four five")]
        result = probe.richness_sample(awards)
        self.assertEqual(result["boilerplate_count"], 1)

    def test_no_descriptions_at_all_is_reported_distinctly(self):
        awards = [probe.AwardRecord("A", "Acme Power", "Department of Energy",
                                    "", "2026-01-01", "")]
        result = probe.richness_sample(awards)
        self.assertIn("NO DESCRIPTIONS", result["characterization"])
        self.assertEqual(result["sampled"], 0)
        self.assertEqual(result["missing_description"], 1)

    def test_examples_are_the_shortest_descriptions_first(self):
        awards = [self._award(d) for d in [
            "A fairly long and substantive description of real project work",
            "Short one",
        ]]
        result = probe.richness_sample(awards)
        self.assertEqual(result["examples"][0]["description"], "Short one")


class TestFetchAgency(unittest.TestCase):
    """Hermetic: probe.fetch_page is replaced, no network. sleep=0 is passed
    explicitly rather than monkeypatching the stdlib time module."""

    def setUp(self):
        self._orig_fetch_page = probe.fetch_page

    def tearDown(self):
        probe.fetch_page = self._orig_fetch_page

    def _page_json(self, n):
        return json.dumps({"results": [
            award(f"A{i}", f"Recipient {i}", "Department of Energy",
                  "2026-01-01") for i in range(n)]})

    def test_a_short_page_stops_after_one_fetch(self):
        calls = []

        def fake(agency, start, end, page, search_terms=None):
            calls.append(page)
            return self._page_json(3)
        probe.fetch_page = fake
        awards, status = probe.fetch_agency("Department of Energy",
                                            "2024-08-18", "2026-08-18", sleep=0)
        self.assertEqual(len(awards), 3)
        self.assertEqual(status, "ok")
        self.assertEqual(calls, [1])

    def test_a_full_page_triggers_a_second_fetch(self):
        pages = {1: probe.PAGE_LIMIT, 2: 4}

        def fake(agency, start, end, page, search_terms=None):
            return self._page_json(pages[page])
        probe.fetch_page = fake
        awards, status = probe.fetch_agency("Department of Energy",
                                            "2024-08-18", "2026-08-18", sleep=0)
        self.assertEqual(len(awards), probe.PAGE_LIMIT + 4)
        self.assertEqual(status, "ok")

    def test_first_page_failure_reports_failed_with_no_awards(self):
        def fake(agency, start, end, page, search_terms=None):
            raise urllib.error.URLError("connection refused")
        probe.fetch_page = fake
        awards, status = probe.fetch_agency("Department of Energy",
                                            "2024-08-18", "2026-08-18", sleep=0)
        self.assertEqual(awards, [])
        self.assertIn("FAILED", status)

    def test_second_page_failure_keeps_first_page_awards_as_partial(self):
        def fake(agency, start, end, page, search_terms=None):
            if page == 1:
                return self._page_json(probe.PAGE_LIMIT)
            raise TimeoutError("timed out")
        probe.fetch_page = fake
        awards, status = probe.fetch_agency("Department of Energy",
                                            "2024-08-18", "2026-08-18", sleep=0)
        self.assertEqual(len(awards), probe.PAGE_LIMIT)
        self.assertIn("PARTIAL", status)
        self.assertIn("page 2", status)

    def test_exhausting_max_pages_reports_truncated(self):
        def fake(agency, start, end, page, search_terms=None):
            return self._page_json(probe.PAGE_LIMIT)
        probe.fetch_page = fake
        awards, status = probe.fetch_agency("Department of Energy",
                                            "2024-08-18", "2026-08-18",
                                            max_pages=2, sleep=0)
        self.assertEqual(len(awards), probe.PAGE_LIMIT * 2)
        self.assertIn("TRUNCATED", status)

    def test_search_terms_are_forwarded_to_fetch_page(self):
        seen = []

        def fake(agency, start, end, page, search_terms=None):
            seen.append(search_terms)
            return self._page_json(1)
        probe.fetch_page = fake
        probe.fetch_agency("Department of Energy", "2024-08-18",
                           "2026-08-18", search_terms=["NextEra Energy"],
                           sleep=0)
        self.assertEqual(seen, [["NextEra Energy"]])


class TestCombineBatchStatuses(unittest.TestCase):
    def test_all_ok_is_ok(self):
        self.assertEqual(probe._combine_batch_statuses(["ok", "ok"]), "ok")

    def test_all_failed_is_failed(self):
        text = probe._combine_batch_statuses(["FAILED (x)", "FAILED (y)"])
        self.assertIn("FAILED", text)
        self.assertIn("all 2", text)

    def test_some_failed_is_partial(self):
        text = probe._combine_batch_statuses(["ok", "FAILED (x)", "ok"])
        self.assertIn("PARTIAL", text)
        self.assertIn("1/3", text)

    def test_truncated_batch_is_distinguished_from_a_failed_one(self):
        """Reviewer-caught: a batch that hit MAX_PAGES (TRUNCATED) is a
        different condition from one whose request actually failed --
        the rollup wording must say so, not lump both under "failed"."""
        text = probe._combine_batch_statuses(
            ["ok", "TRUNCATED (hit the 5-page bound...)", "FAILED (x)"])
        self.assertIn("PARTIAL", text)
        self.assertIn("1 truncated", text)
        self.assertIn("1 failed", text)

    def test_all_truncated_says_truncated_not_failed_in_the_breakdown(self):
        text = probe._combine_batch_statuses(
            ["TRUNCATED (a)", "TRUNCATED (b)"])
        self.assertIn("2 truncated", text)
        self.assertNotIn("2 failed", text)


class TestFetchAgencyBatched(unittest.TestCase):
    """SEARCH_TERM_BATCH_SIZE=10 is a live-discovered API limit (see the
    module docstring): a recipient_search_text array of 14+ terms 503s.
    Hermetic: probe.fetch_agency is replaced, no network."""

    def setUp(self):
        self._orig = probe.fetch_agency

    def tearDown(self):
        probe.fetch_agency = self._orig

    def test_no_search_terms_falls_back_to_a_single_unscoped_call(self):
        calls = []

        def fake(agency, start, end, search_terms=None, max_pages=None,
                 sleep=None):
            calls.append(search_terms)
            return [], "ok"
        probe.fetch_agency = fake
        probe.fetch_agency_batched("Department of Energy", "2024-08-18",
                                   "2026-08-18", [], sleep=0)
        self.assertEqual(calls, [None])

    def test_terms_are_split_into_batches_of_batch_size(self):
        seen_batches = []

        def fake(agency, start, end, search_terms=None, max_pages=None,
                 sleep=None):
            seen_batches.append(list(search_terms))
            return [], "ok"
        probe.fetch_agency = fake
        terms = [f"Entity {i}" for i in range(25)]
        probe.fetch_agency_batched("Department of Energy", "2024-08-18",
                                   "2026-08-18", terms, sleep=0, batch_size=10)
        self.assertEqual(len(seen_batches), 3)
        self.assertEqual(len(seen_batches[0]), 10)
        self.assertEqual(len(seen_batches[1]), 10)
        self.assertEqual(len(seen_batches[2]), 5)

    def test_awards_from_every_batch_are_combined(self):
        def fake(agency, start, end, search_terms=None, max_pages=None,
                 sleep=None):
            name = search_terms[0]
            return ([probe.AwardRecord(f"A-{name}", name, agency, "",
                                       "2026-01-01", "x")], "ok")
        probe.fetch_agency = fake
        terms = [f"Entity {i}" for i in range(15)]
        awards, status = probe.fetch_agency_batched(
            "Department of Energy", "2024-08-18", "2026-08-18", terms,
            sleep=0, batch_size=10)
        self.assertEqual(len(awards), 2)
        self.assertEqual(status, "ok")

    def test_one_failed_batch_is_reported_as_partial(self):
        def fake(agency, start, end, search_terms=None, max_pages=None,
                 sleep=None):
            if search_terms[0] == "Entity 0":
                return [], "FAILED (HTTPError 503)"
            return ([probe.AwardRecord("A", "Acme", agency, "", "2026-01-01",
                                       "x")], "ok")
        probe.fetch_agency = fake
        terms = [f"Entity {i}" for i in range(15)]
        awards, status = probe.fetch_agency_batched(
            "Department of Energy", "2024-08-18", "2026-08-18", terms,
            sleep=0, batch_size=10)
        self.assertEqual(len(awards), 1)
        self.assertIn("PARTIAL", status)
        self.assertIn("1/2", status)

    def test_one_truncated_batch_is_reported_as_partial_not_failed(self):
        """The MAX_PAGES x search-term-batching interaction the review was
        asked to check: a real fetch_agency() TRUNCATED status for one
        batch must survive the rollup as PARTIAL/truncated, not read as an
        outright failure."""
        def fake(agency, start, end, search_terms=None, max_pages=None,
                 sleep=None):
            if search_terms[0] == "Entity 0":
                return [], "TRUNCATED (hit the 5-page bound...)"
            return ([probe.AwardRecord("A", "Acme", agency, "", "2026-01-01",
                                       "x")], "ok")
        probe.fetch_agency = fake
        terms = [f"Entity {i}" for i in range(15)]
        awards, status = probe.fetch_agency_batched(
            "Department of Energy", "2024-08-18", "2026-08-18", terms,
            sleep=0, batch_size=10)
        self.assertEqual(len(awards), 1)
        self.assertIn("PARTIAL", status)
        self.assertIn("1 truncated", status)
        self.assertNotIn("1 failed", status)

    def test_duplicate_award_id_across_batches_is_not_double_counted(self):
        """A recipient can substring-match more than one watchlist term
        (e.g. "Southern" and "Southern Company") and so appear in more than
        one batch's results for the SAME real award."""
        def fake(agency, start, end, search_terms=None, max_pages=None,
                 sleep=None):
            return ([probe.AwardRecord("SAME_ID", "Acme Power", agency, "",
                                       "2026-01-01", "x")], "ok")
        probe.fetch_agency = fake
        terms = [f"Entity {i}" for i in range(15)]
        awards, status = probe.fetch_agency_batched(
            "Department of Energy", "2024-08-18", "2026-08-18", terms,
            sleep=0, batch_size=10)
        self.assertEqual(len(awards), 1)


class TestCollect(unittest.TestCase):
    """Hermetic: probe.fetch_agency is replaced, no network. sleep=0 is
    passed explicitly rather than monkeypatching the stdlib time module."""

    def setUp(self):
        self._orig = probe.fetch_agency
        self.conn = fixture_conn()

    def tearDown(self):
        probe.fetch_agency = self._orig
        self.conn.close()

    def test_collects_every_qualifying_agency_in_sequence(self):
        seen = []

        def fake(agency, start, end, search_terms=None, max_pages=None,
                sleep=None):
            seen.append(agency)
            return ([probe.AwardRecord("A", agency, agency, "", "2026-01-01",
                                       "x")], "ok")
        probe.fetch_agency = fake
        awards, status = probe.collect(self.conn, now=NOW, sleep=0)
        self.assertEqual(seen, sorted(probe.QUALIFYING_TOPTIER_AGENCIES))
        self.assertEqual(len(awards), 2)
        self.assertEqual(set(status), probe.QUALIFYING_TOPTIER_AGENCIES)

    def test_one_failed_agency_does_not_drop_the_other(self):
        def fake(agency, start, end, search_terms=None, max_pages=None,
                sleep=None):
            if agency == "Department of Energy":
                return [], "FAILED (HTTPError)"
            return ([probe.AwardRecord("A", agency, agency, "", "2026-01-01",
                                       "x")], "ok")
        probe.fetch_agency = fake
        awards, status = probe.collect(self.conn, now=NOW, sleep=0)
        self.assertEqual(len(awards), 1)
        self.assertIn("FAILED", status["Department of Energy"])
        self.assertEqual(status["Department of Homeland Security"], "ok")

    def test_scopes_the_query_to_watchlist_names_and_aliases(self):
        seen = {}

        def fake(agency, start, end, search_terms=None, max_pages=None,
                sleep=None):
            seen[agency] = search_terms
            return [], "ok"
        probe.fetch_agency = fake
        probe.collect(self.conn, now=NOW, sleep=0)
        terms = seen["Department of Energy"]
        self.assertIn("NextEra Energy", terms)
        self.assertIn("Pacific Gas and Electric", terms)   # alias
        self.assertEqual(terms, sorted(terms))


class TestWatchlistSearchTerms(unittest.TestCase):
    def test_includes_active_entity_names_and_aliases(self):
        conn = fixture_conn()
        try:
            terms = probe.watchlist_search_terms(conn)
        finally:
            conn.close()
        for name in ("NextEra Energy", "Duke Energy Indiana",
                     "Dominion Energy", "PG&E", "Pacific Gas and Electric"):
            self.assertIn(name, terms)


class TestContainment(unittest.TestCase):
    """Same containment discipline as the other spikes: read-only, writes
    nothing, absent from the ingest pipeline, no runner.cli entry point."""

    def test_report_opens_the_store_read_only_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "probe.db")
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON;")
            apply_migrations(conn)
            seed(conn)
            conn.close()

            before = os.path.getsize(db_path)
            out = io.StringIO()
            awards = probe.parse_awards(SIX_DISTINCT_JSON)
            result = probe.report(db_path, awards,
                                  source_status={"Department of Energy": "ok"},
                                  now=NOW, out=out)
            self.assertEqual(result["distinct_account_count"], 6)
            self.assertIn("DISTINCT ACCOUNT COUNT: 6", out.getvalue())
            self.assertEqual(os.path.getsize(db_path), before)

            check = sqlite3.connect(db_path)
            try:
                for table in ("review_queue", "entity_match_decisions",
                              "signals", "raw_events"):
                    self.assertEqual(
                        check.execute(f"SELECT COUNT(*) FROM {table}")
                        .fetchone()[0], 0, f"{table} was written by the probe")
            finally:
                check.close()

    def test_read_only_connect_survives_a_uri_fragment_in_the_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "gridsignals#1.db")
            conn = sqlite3.connect(db_path)
            apply_migrations(conn)
            seed(conn)
            conn.close()

            ro = probe.read_only_connect(db_path)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    ro.execute("INSERT INTO watchlist_entities "
                               "(entity_id, name) VALUES ('EZZZ', 'Written')")
            finally:
                ro.close()

    def test_module_uses_a_read_only_connection_and_no_dml(self):
        self.assertIn("mode=ro", MODULE_SRC)
        for verb in ("INSERT ", "UPDATE ", "DELETE "):
            self.assertNotIn(verb, MODULE_SRC.upper())

    def test_probe_is_absent_from_the_ingest_pipeline(self):
        pipeline = PIPELINE.read_text(encoding="utf-8")
        self.assertNotIn("usaspending_probe", pipeline)
        self.assertNotIn("app.spikes", pipeline)

    def test_module_has_no_runner_cli_entry_point(self):
        self.assertNotIn("runner.cli()", MODULE_SRC)
        self.assertNotIn("app.ingest.runner", MODULE_SRC)


class TestMainExitCode(unittest.TestCase):
    """Reviewer-caught (agent-native): main() previously always returned 0
    even when every source degraded to FAILED/PARTIAL, indistinguishable
    from a clean run to an automated caller checking $? instead of parsing
    the report text. Hermetic: probe.collect/probe.report are replaced."""

    def setUp(self):
        self._orig_collect = probe.collect
        self._orig_report = probe.report
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "probe.db")
        conn = sqlite3.connect(self.db_path)
        apply_migrations(conn)
        conn.close()

    def tearDown(self):
        probe.collect = self._orig_collect
        probe.report = self._orig_report
        self._tmp.cleanup()

    def test_all_ok_status_exits_zero(self):
        probe.collect = lambda conn, now=None: ([], {"Department of Energy": "ok"})
        probe.report = lambda *a, **kw: {"source_status": {"Department of Energy": "ok"}}
        self.assertEqual(probe.main(["--report", "--db", self.db_path]), 0)

    def test_a_failed_source_status_exits_nonzero(self):
        probe.collect = lambda conn, now=None: (
            [], {"Department of Energy": "FAILED (HTTPError 503)"})
        probe.report = lambda *a, **kw: {
            "source_status": {"Department of Energy": "FAILED (HTTPError 503)"}}
        self.assertNotEqual(probe.main(["--report", "--db", self.db_path]), 0)


if __name__ == "__main__":
    unittest.main()
