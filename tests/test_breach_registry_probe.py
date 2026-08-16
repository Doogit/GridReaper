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
from datetime import datetime, timezone
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

# WA Socrata shape, including two resident/individual columns that must never
# reach the analysis or the report (R10.6).
PII_COUNT = "4210"
PII_NAME = "Jane Q Public"
WA_JSON = json.dumps([
    {"name_of_business": "Dominion Energy, Inc.",
     "date_reported": "2026-01-15T00:00:00.000",
     "number_of_washingtonians_affected": PII_COUNT,
     "individual_contact_name": PII_NAME},
    {"name_of_business": "Zephyr Bakery Collective",
     "date_reported": "2015-08-01T00:00:00.000"},        # outside the window
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

    def test_wa_dataset_drops_individual_fields(self):
        rows = probe.parse_wa_dataset(WA_JSON)
        self.assertEqual(rows[0], probe.BreachRow("WA", "Dominion Energy, Inc.",
                                                  "2026-01-15"))
        blob = repr(rows)
        self.assertNotIn(PII_NAME, blob)
        self.assertNotIn(PII_COUNT, blob)

    def test_wa_dataset_with_unknown_fields_raises_with_observed_keys(self):
        """A renamed Socrata column must fail loudly: a silent empty parse is
        indistinguishable from a measured zero, and the live run happens once."""
        with self.assertRaises(ValueError) as ctx:
            probe.parse_wa_dataset('[{"entity_label": "Acme Power"}]')
        self.assertIn("entity_label", str(ctx.exception))


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

    def test_report_wording_is_ascii(self):
        """The live run prints to a Windows console; a smart dash in the
        report's own wording would come back mojibake in the PR body."""
        self.text.encode("ascii")

    def test_report_states_the_hit_count_and_the_definition(self):
        self.assertIn("HIT COUNT (pinned definition): 2", self.text)
        self.assertIn("trailing 24 months", self.text)
        self.assertIn("reported_date >= 2024-08-16", self.text)


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
