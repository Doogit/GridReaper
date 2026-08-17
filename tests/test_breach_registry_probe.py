"""Breach-registry spike tests (R5.5, R9.6 Stage 2, R10.6).

Hermetic: real migrations against SQLite, canned registry fixtures, no network.
The point of this file is that the number the operator reads in the PR body is
the number this code computes — the pinned hit definition (a DISTINCT WATCHLIST
ENTITY named at least once in CA or WA with a reported date inside the trailing
24 months) is asserted here against a fixture whose rows deliberately straddle
the window and name one entity in both states.
"""
import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.db.migrate import apply_migrations
from app.spikes import breach_registry_probe as probe

REPO = Path(__file__).resolve().parent.parent
PIPELINE = REPO / "deploy" / "ingest_pipeline.sh"
MODULE_SRC = Path(probe.__file__).read_text(encoding="utf-8")

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)   # cutoff 2024-08-16

ENTITIES = [
    ("E0004", "Dominion Energy", "0000715957", "D"),
    ("E0005", "American Electric Power", "0000004904", "AEP"),
    ("E0008", "PG&E", "0001004980", "PCG"),
    # a parent with no alias for its operating subsidiary -- the KTD5 shape
    ("E0031", "Edison International", "0000827052", "EIX"),
]
ALIASES = [("E0008", "Pacific Gas and Electric")]
COLLISIONS = [("E0004", "Dominion")]

# CA OAG CSV export shape: the real header, plus the breach-date column the
# probe must drop.
CA_CSV = (
    '"Organization Name","Date(s) of Breach (if known)","Reported Date"\n'
    '"Dominion Energy","01/02/2026","03/04/2026"\n'
    '"Pacific Gas and Electric Company","","08/01/2025"\n'
    '"Dominion","","09/09/2025"\n'
    '"American Electric Power","","01/01/2019"\n'          # outside the window
    '"American Electric Power of Ohio","","02/02/2026"\n'   # fuzzy near-miss
    '"Zephyr Bakery Collective","","05/05/2026"\n'          # off-list
)

# WA Socrata shape, using the dataset's REAL column names (verified 2026-08-16
# from https://data.wa.gov/api/views/sb4j-ca4h.json): the organization is
# `name` and the reported date is `datesubmitted`, a calendar_date carrying a
# time component. `dateaware` describes the breach, not the notification, and
# must not be read as the reported date. The two resident/individual columns
# must never reach the analysis or the report (R10.6).
PII_COUNT = "4210"
PII_NAME = "Jane Q Public"
WA_JSON = json.dumps([
    {"name": "Dominion Energy, Inc.",
     "datesubmitted": "2026-01-15T00:00:00.000",
     "dateaware": "2025-11-02T00:00:00.000",
     "washingtoniansaffected": PII_COUNT,
     "contactname": PII_NAME},
    {"name": "Zephyr Bakery Collective",
     "datesubmitted": "2015-08-01T00:00:00.000"},        # outside the window
])


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


def fixture_rows():
    return probe.parse_ca_export(CA_CSV) + probe.parse_wa_dataset(WA_JSON)


class TestParsers(unittest.TestCase):
    def test_ca_export_yields_org_and_reported_date_only(self):
        rows = probe.parse_ca_export(CA_CSV)
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0],
                         probe.BreachRow("CA", "Dominion Energy", "2026-03-04"))
        self.assertEqual(rows[0]._fields, ("state", "organization",
                                           "reported_date"))
        # the breach-date column is dropped at the row boundary
        self.assertNotIn("2026-01-02", [r.reported_date for r in rows])

    def test_ca_export_tolerates_bom_and_missing_dates(self):
        rows = probe.parse_ca_export(
            '﻿"Organization Name","Reported Date"\n"Acme Power",""\n')
        self.assertEqual(rows, [probe.BreachRow("CA", "Acme Power", "")])

    def test_ca_export_without_org_column_raises(self):
        with self.assertRaises(ValueError):
            probe.parse_ca_export('"Reported Date"\n"03/04/2026"\n')

    def test_ca_export_without_date_column_raises_with_observed_headers(self):
        """The CA twin of the WA regression: a missing date HEADER would make
        every CA row undated, every row out of window, and CA's contribution
        zero, under status=ok. Both halves of the allowlist fail loud."""
        with self.assertRaises(ValueError) as ctx:
            probe.parse_ca_export(
                '"Organization Name","Date(s) of Breach (if known)"\n'
                '"Acme Power","01/02/2026"\n')
        message = str(ctx.exception)
        self.assertIn("reported-date", message)
        self.assertIn("date(s) of breach (if known)", message)

    def test_wa_dataset_reads_the_real_columns_and_drops_individual_fields(self):
        """Real column names, and the calendar_date time component stripped."""
        rows = probe.parse_wa_dataset(WA_JSON)
        self.assertEqual(rows[0], probe.BreachRow("WA", "Dominion Energy, Inc.",
                                                  "2026-01-15"))
        blob = repr(rows)
        self.assertNotIn(PII_NAME, blob)
        self.assertNotIn(PII_COUNT, blob)
        self.assertNotIn("2025-11-02", blob)   # dateaware is not a reported date

    def test_wa_dataset_prefers_datesubmitted_over_speculative_date_keys(self):
        rows = probe.parse_wa_dataset(json.dumps(
            [{"name": "Acme Power", "datesubmitted": "2026-01-15",
              "date_reported": "2019-01-15"}]))
        self.assertEqual(rows[0].reported_date, "2026-01-15")

    def test_wa_dataset_with_unknown_org_field_raises_with_observed_keys(self):
        """A renamed Socrata column must fail loudly: a silent empty parse is
        indistinguishable from a measured zero, and the live run happens once."""
        with self.assertRaises(ValueError) as ctx:
            probe.parse_wa_dataset(
                '[{"entity_label": "Acme Power", "datesubmitted": "2026-01-15"}]')
        self.assertIn("entity_label", str(ctx.exception))

    def test_wa_dataset_with_unknown_date_field_raises_with_observed_keys(self):
        """REGRESSION (live run, 2026-08-16): with no recognised date column
        every row parsed as undated, so 1,629 real WA rows produced
        in_window=0 and a hit count of 0 under status=ok. A missing date half
        of the allowlist must fail as loudly as a missing org half -- an
        unparseable dataset is untested, not zero."""
        payload = ('[{"name": "Acme Power", "dateaware": "2026-01-15", '
                   '"datestart": "2026-01-01"}]')
        with self.assertRaises(ValueError) as ctx:
            probe.parse_wa_dataset(payload)
        message = str(ctx.exception)
        self.assertIn("reported-date", message)
        self.assertIn("dateaware", message)
        self.assertIn("name", message)


    def test_iso_date_handles_calendar_date_and_us_formats(self):
        # Socrata calendar_date arrives with a time component; CA renders M/D/Y.
        self.assertEqual(probe._iso_date("2025-03-14T00:00:00.000"), "2025-03-14")
        self.assertEqual(probe._iso_date("2025-03-14"), "2025-03-14")
        self.assertEqual(probe._iso_date("3/4/2026"), "2026-03-04")
        # unrecognised or absent -> undated, never guessed
        self.assertEqual(probe._iso_date(None), "")
        self.assertEqual(probe._iso_date("March 2026"), "")

    def test_out_of_range_date_is_undated_not_a_fabricated_month(self):
        """A DD/MM/YYYY value with day > 12 must fail closed. Formatting it
        blind produced "2026-14-03", which sorts as in-window and would have
        counted toward the number."""
        self.assertEqual(probe._iso_date("14/03/2026"), "")
        self.assertEqual(probe._iso_date("2026-13-45"), "")
        self.assertEqual(probe._iso_date("02/30/2026"), "")


class TestWindow(unittest.TestCase):
    def test_cutoff_is_the_24_month_anniversary(self):
        self.assertEqual(probe.window_cutoff(NOW), "2024-08-16")

    def test_leap_day_cutoff_falls_back_to_feb_28(self):
        leap = datetime(2028, 2, 29, tzinfo=timezone.utc)
        self.assertEqual(probe.window_cutoff(leap), "2026-02-28")


class TestAnalysis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = fixture_conn()
        cls.result = probe.analyze(cls.conn, fixture_rows(),
                                   state_status={"CA": "ok", "WA": "ok"},
                                   now=NOW)
        cls.text = probe.format_report(cls.result)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_hit_count_is_distinct_in_window_entities(self):
        """THE PINNED NUMBER. Six distinct in-window organization names name
        two distinct watchlist entities: Dominion Energy (CA *and* WA -> one
        hit) and PG&E (its legal filing name). American Electric Power is named
        in CA but outside the window, so it is not a hit -- ignoring the window
        would report 3."""
        self.assertEqual(self.result["hit_count"], 2)
        self.assertEqual([h["entity_id"] for h in self.result["hits"]],
                         ["E0004", "E0008"])

    def test_entity_named_in_both_states_counts_once(self):
        dominion = self.result["hits"][0]
        self.assertEqual(dominion["states"], ["CA", "WA"])
        self.assertEqual(dominion["names"],
                         ["Dominion Energy", "Dominion Energy, Inc."])

    def test_legal_filing_name_matches_through_the_alias_table(self):
        pge = self.result["hits"][1]
        self.assertEqual(pge["entity_id"], "E0008")
        self.assertEqual(pge["names"], ["Pacific Gas and Electric Company"])

    def test_context_counts_are_reported_beside_the_hit_count(self):
        self.assertEqual(self.result["states"]["CA"],
                         {"status": "ok", "rows": 6, "in_window": 5,
                          "undated": 0})
        self.assertEqual(self.result["states"]["WA"],
                         {"status": "ok", "rows": 2, "in_window": 1,
                          "undated": 0})
        self.assertEqual(self.result["total_rows"], 8)
        self.assertEqual(self.result["total_in_window"], 6)
        self.assertEqual(self.result["distinct_orgs_in_window"], 6)

    def test_collision_name_is_review_queued_never_matched(self):
        review = {r["organization"]: r for r in self.result["review"]}
        self.assertIn("Dominion", review)
        self.assertEqual(review["Dominion"]["reason"], "collision_term")
        self.assertNotIn("Dominion", [n for h in self.result["hits"]
                                      for n in h["names"]])

    def test_review_and_near_misses_carry_names_and_scores(self):
        """The adjudication pass depends on these, so pin them: an operator
        cannot confirm a hit the report never showed them."""
        near = {r["organization"]: r for r in self.result["near_misses"]}
        self.assertIn("American Electric Power of Ohio", near)
        eid, name, score = near["American Electric Power of Ohio"]["candidates"][0]
        self.assertEqual((eid, name), ("E0005", "American Electric Power"))
        self.assertTrue(0.75 <= score < 0.90, score)
        # ordered by score, best first
        self.assertEqual(self.result["near_misses"][0]["organization"],
                         "American Electric Power of Ohio")
        self.assertIn("American Electric Power of Ohio", self.text)
        self.assertIn(str(score), self.text)

    def test_near_misses_exclude_reasons_with_no_computed_similarity(self):
        """collision_term / ambiguous_* carry the constant REVIEW_THRESHOLD,
        not a similarity ratio, so printing them under "score desc" would
        publish 0.75 as if it measured resemblance. They stay in the
        review-queued list, which is where an adjudicator needs them."""
        self.assertEqual([r["organization"] for r in self.result["near_misses"]],
                         ["American Electric Power of Ohio"])
        self.assertIn("Dominion", [r["organization"]
                                   for r in self.result["review"]])

    def test_off_list_organization_is_counted_but_not_named(self):
        self.assertEqual(self.result["unmatched_count"], 1)
        self.assertEqual(self.result["unmatched_sample"], [])
        self.assertNotIn("Zephyr Bakery Collective", self.text)

    def test_individual_fields_never_reach_the_report(self):
        self.assertNotIn(PII_NAME, self.text)
        self.assertNotIn(PII_COUNT, self.text)
        self.assertNotIn(PII_NAME, json.dumps(self.result, default=str))
        self.assertNotIn(PII_COUNT, json.dumps(self.result, default=str))

    def test_unmatched_sample_is_opt_in(self):
        result = probe.analyze(self.conn, fixture_rows(), now=NOW,
                               unmatched_sample=5)
        self.assertEqual(result["unmatched_sample"], ["Zephyr Bakery Collective"])

    def test_unmatched_sample_holds_the_remainder_not_a_second_copy(self):
        """The sample reaches the names nothing else reaches; a name already
        listed by token overlap is not printed twice."""
        rows = fixture_rows() + [probe.BreachRow(
            "CA", "Southern California Edison Company", "2026-01-05")]
        result = probe.analyze(self.conn, rows, now=NOW, unmatched_sample=5)
        self.assertEqual([e["organization"]
                          for e in result["unmatched_token_overlap"]],
                         ["Southern California Edison Company"])
        self.assertEqual(result["unmatched_sample"],
                         ["Zephyr Bakery Collective"])
        self.assertEqual(result["unmatched_count"], 2)

    def test_report_wording_is_ascii(self):
        """The live run prints to a Windows console; a smart dash in the
        report's own wording would come back mojibake in the PR body."""
        self.text.encode("ascii")

    def test_report_states_the_hit_count_and_the_definition(self):
        self.assertIn("HIT COUNT (pinned definition): 2", self.text)
        self.assertIn("trailing 24 months", self.text)
        self.assertIn("reported_date >= 2024-08-16", self.text)


class TestWindowBoundary(unittest.TestCase):
    """The window is the single choice that can swing the number by ~5x and
    cross the build line on its own (CA reaches back to 2024-01, WA to
    2015-07), so its inclusive edge gets a fixture sitting exactly on it. The
    dates are derived from window_cutoff so the pair stays adjacent to the
    boundary whatever `now` is; TestWindow pins the cutoff's value itself."""

    def _analyze(self, reported_date):
        conn = fixture_conn()
        try:
            return probe.analyze(
                conn, [probe.BreachRow("CA", "Dominion Energy", reported_date)],
                state_status={"CA": "ok"}, now=NOW)
        finally:
            conn.close()

    def test_row_exactly_on_the_cutoff_counts(self):
        result = self._analyze(probe.window_cutoff(NOW))
        self.assertEqual(result["states"]["CA"]["in_window"], 1)
        self.assertEqual(result["hit_count"], 1)

    def test_row_one_day_before_the_cutoff_does_not_count(self):
        day_before = (date.fromisoformat(probe.window_cutoff(NOW))
                      - timedelta(days=1)).isoformat()
        result = self._analyze(day_before)
        self.assertEqual(result["states"]["CA"]["in_window"], 0)
        self.assertEqual(result["hit_count"], 0)


class TestUnparseableDates(unittest.TestCase):
    """A column that EXISTS but whose values do not parse is the same silent
    zero one layer down: the allowlist proves the field is there, never that
    _iso_date understood it. Left alone, the operator reads a STOP band off a
    parsing failure."""

    CSV = ('"Organization Name","Reported Date"\n'
           '"Dominion Energy","March 4, 2026"\n'
           '"Pacific Gas and Electric Company","14/03/2026"\n')

    @classmethod
    def setUpClass(cls):
        cls.conn = fixture_conn()
        cls.result = probe.analyze(cls.conn, probe.parse_ca_export(cls.CSV),
                                   state_status={"CA": "ok"}, now=NOW)
        cls.text = probe.format_report(cls.result)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_state_status_degrades_when_every_row_is_undated(self):
        self.assertEqual(self.result["states"]["CA"]["rows"], 2)
        self.assertEqual(self.result["states"]["CA"]["undated"], 2)
        self.assertEqual(self.result["states"]["CA"]["in_window"], 0)
        self.assertNotEqual(self.result["states"]["CA"]["status"], "ok")
        self.assertIn("DATES UNPARSEABLE", self.result["states"]["CA"]["status"])

    def test_the_zero_is_reported_as_partial_never_as_a_stop_band(self):
        self.assertEqual(self.result["hit_count"], 0)
        self.assertIn("PARTIAL MEASUREMENT", self.result["recommendation"])
        self.assertIn("PARTIAL MEASUREMENT", self.text)

    def test_the_undated_counter_renders_in_the_report(self):
        # the live WA defect presented as `in_window=0 undated=1629`; the
        # counter that made it visible has to be on the page.
        self.assertIn("in_window=0  undated=2", self.text)


class TestTokenOverlap(unittest.TestCase):
    """The resolver's `none` bucket is the one adjudication surface with no
    score to rank by, and the KTD5 shape -- an operating subsidiary against a
    parent with no alias -- lands squarely in it."""

    def test_subsidiary_surfaces_by_its_shared_distinctive_token(self):
        conn = fixture_conn()
        try:
            result = probe.analyze(
                conn,
                [probe.BreachRow("CA", "Southern California Edison Company",
                                 "2026-01-05")],
                state_status={"CA": "ok"}, now=NOW)
        finally:
            conn.close()
        self.assertEqual(result["hit_count"], 0)      # the resolver declines it
        self.assertEqual(result["unmatched_count"], 1)
        entry = result["unmatched_token_overlap"][0]
        self.assertEqual(entry["organization"],
                         "Southern California Edison Company")
        self.assertEqual(entry["tokens"], ["edison"])
        self.assertIn(("E0031", "Edison International"), entry["entities"])
        text = probe.format_report(result)
        self.assertIn("Southern California Edison Company", text)
        self.assertIn("shares=edison", text)

    def test_a_generic_token_alone_is_not_an_overlap(self):
        conn = fixture_conn()
        try:
            self.assertEqual(
                probe.token_overlap(conn, ["Riverbend Energy Company"], {}), [])
            self.assertTrue(
                probe.token_overlap(conn, ["Riverbend Edison Company"], {}))
        finally:
            conn.close()


class TestCollect(unittest.TestCase):
    """collect() is the only place a live failure can be absorbed, so its
    per-state status is what keeps one dead source from costing the other its
    measurement. Hermetic: probe.fetch is replaced, no network."""

    def _collect(self, ca, wa):
        def fake_fetch(url, accept):
            payload = ca if "oag" in url else wa
            if isinstance(payload, Exception):
                raise payload
            return payload

        original = probe.fetch
        probe.fetch = fake_fetch
        try:
            return probe.collect("https://oag.example/list-export",
                                 "https://data.example/wa.json", sleep=0)
        finally:
            probe.fetch = original

    def test_a_dropped_wa_connection_keeps_the_ca_rows(self):
        # ConnectionResetError is an OSError but NOT a URLError, so the old
        # enumerated tuple let it escape and abort the run after a good CA
        # fetch, before a single report line printed.
        rows, status = self._collect(CA_CSV, ConnectionResetError("dropped"))
        self.assertEqual(len(rows), 6)
        self.assertEqual(status["CA"], "ok")
        self.assertIn("FAILED", status["WA"])
        self.assertIn("ConnectionResetError", status["WA"])

    def test_a_malformed_ca_export_does_not_abort_the_wa_fetch(self):
        rows, status = self._collect('"Organization Name"\n"Acme Power"\n',
                                     WA_JSON)
        self.assertIn("FAILED", status["CA"])
        self.assertEqual(status["WA"], "ok")
        self.assertEqual([r.state for r in rows], ["WA", "WA"])

    def test_a_truncated_socrata_page_is_not_ok(self):
        # Socrata returns exactly $limit rows when the dataset is larger, with
        # no error and no marker: a truncated fetch must not read as complete.
        original_cap = probe.WA_ROW_CAP
        probe.WA_ROW_CAP = 2
        try:
            rows, status = self._collect(CA_CSV, WA_JSON)
        finally:
            probe.WA_ROW_CAP = original_cap
        self.assertEqual(len(rows), 8)
        self.assertEqual(status["CA"], "ok")
        self.assertIn("TRUNCATED", status["WA"])
        self.assertIn("PARTIAL MEASUREMENT",
                      probe.recommendation(2, {"CA": {"status": status["CA"]},
                                               "WA": {"status": status["WA"]}}))


class TestOtherPublishingStates(unittest.TestCase):
    """U36: the 13 non-CA/WA publishing states, checked live and found not
    machine-accessible. A state confirmed structurally inaccessible must be
    reported as such, not silently omitted -- that omission is exactly what
    U5 left behind ("not tested" read as a gap, not a finding)."""

    def test_thirteen_states_are_recorded_and_ca_wa_are_not(self):
        self.assertEqual(len(probe.OTHER_PUBLISHING_STATES), 13)
        self.assertNotIn("CA", probe.OTHER_PUBLISHING_STATES)
        self.assertNotIn("WA", probe.OTHER_PUBLISHING_STATES)

    def test_every_state_carries_a_url_and_a_non_empty_reason(self):
        for state, (url, reason) in probe.OTHER_PUBLISHING_STATES.items():
            self.assertTrue(url.startswith("https://"), state)
            self.assertTrue(reason, state)

    def test_analyze_reports_all_thirteen_unconditionally(self):
        """Reported beside a non-zero hit count too, not just under a STOP
        band -- coverage is a fact about the measurement, not a caveat that
        only matters when the number is 0."""
        conn = fixture_conn()
        try:
            result = probe.analyze(conn, fixture_rows(),
                                   state_status={"CA": "ok", "WA": "ok"},
                                   now=NOW)
        finally:
            conn.close()
        self.assertEqual(result["hit_count"], 2)
        self.assertEqual(set(result["other_publishing_states"]),
                         set(probe.OTHER_PUBLISHING_STATES))

    def test_report_lists_every_other_state_and_its_reason(self):
        conn = fixture_conn()
        try:
            result = probe.analyze(conn, fixture_rows(),
                                   state_status={"CA": "ok", "WA": "ok"},
                                   now=NOW)
        finally:
            conn.close()
        text = probe.format_report(result)
        text.encode("ascii")   # same discipline as the rest of the report
        for state, (_, reason) in probe.OTHER_PUBLISHING_STATES.items():
            self.assertIn(state, text)
            self.assertIn(reason, text)
        self.assertIn("15 US states", text)

    def test_negative_finding_cites_the_live_check_not_an_absence_of_one(self):
        self.assertIn("checked live", probe.NEGATIVE_FINDING)
        self.assertNotIn("untested", probe.NEGATIVE_FINDING)


class TestRecommendation(unittest.TestCase):
    OK = {"CA": {"status": "ok"}, "WA": {"status": "ok"}}

    def test_zero_records_the_precise_negative(self):
        text = probe.recommendation(0, self.OK)
        self.assertIn("STOP", text)
        self.assertIn(probe.NEGATIVE_FINDING, text)
        self.assertNotIn("closed on the free public record", text)

    def test_one_to_four_asks_for_an_operator_ruling(self):
        self.assertIn("OPERATOR RULING", probe.recommendation(4, self.OK))

    def test_five_or_more_builds_u6(self):
        self.assertIn("BUILD U6", probe.recommendation(5, self.OK))

    def test_failed_state_is_not_a_measured_zero(self):
        text = probe.recommendation(0, {"CA": {"status": "ok"},
                                        "WA": {"status": "FAILED (HTTPError)"}})
        self.assertIn("PARTIAL MEASUREMENT", text)
        self.assertIn("WA", text)


class TestContainment(unittest.TestCase):
    """#74's precedent: a measurement harness that could write is not a
    measurement, and one that is wired into the pipeline is a fetcher."""

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
            result = probe.report(db_path, fixture_rows(),
                                  state_status={"CA": "ok", "WA": "ok"},
                                  now=NOW, out=out)
            self.assertEqual(result["hit_count"], 2)
            self.assertIn("HIT COUNT (pinned definition): 2", out.getvalue())
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
        """A '#' starts the fragment in a SQLite URI, so pasting the path into
        an f-string truncated it and took ?mode=ro with it -- the connection
        then opened READ-WRITE, silently, and the only guarantee this module
        makes about the store was gone."""
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

            out = io.StringIO()
            probe.report(db_path, fixture_rows(),
                         state_status={"CA": "ok", "WA": "ok"}, now=NOW,
                         out=out)
            self.assertIn("HIT COUNT (pinned definition): 2", out.getvalue())

    def test_module_uses_a_read_only_connection_and_no_dml(self):
        self.assertIn("mode=ro", MODULE_SRC)
        for verb in ("INSERT ", "UPDATE ", "DELETE "):
            self.assertNotIn(verb, MODULE_SRC.upper())

    def test_probe_is_absent_from_the_ingest_pipeline(self):
        pipeline = PIPELINE.read_text(encoding="utf-8")
        self.assertNotIn("breach_registry_probe", pipeline)
        self.assertNotIn("app.spikes", pipeline)


if __name__ == "__main__":
    unittest.main()
