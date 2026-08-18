"""FERC eLibrary spike tests (R9.6, R5.5).

Hermetic: real migrations against SQLite, canned document fixtures, no
network. ``TestLiveProbes`` monkeypatches ``urllib.request.urlopen`` to prove
the module never sends an API key and correctly classifies both known-live
blocked shapes (403 API_KEY_MISSING, and the Angular ``<app-root>`` shell)
without ever touching the network.
"""
import io
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db.migrate import apply_migrations
from app.spikes import ferc_elibrary_probe as probe

REPO = Path(__file__).resolve().parent.parent
PIPELINE = REPO / "deploy" / "ingest_pipeline.sh"
MODULE_SRC = Path(probe.__file__).read_text(encoding="utf-8")

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)   # cutoff 2024-08-17

ENTITIES = [
    ("E0004", "Dominion Energy", "0000715957", "D"),
    ("E0005", "American Electric Power", "0000004904", "AEP"),
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


# One doc naming a watchlist entity (no CIP ref), one with a CIP-### ref
# whose party is off-list, one naming neither, plus an out-of-window doc.
DOCS_JSON = json.dumps([
    {"party_name": "Dominion Energy", "filed_date": "2026-03-04",
     "text": "Tariff filing under Docket No. ER26-100."},
    {"party_name": "Zephyr Bakery Collective", "filed_date": "2026-01-15",
     "text": "Order approving compliance with Reliability Standard CIP-005-7."},
    {"party_name": "Acme Widgets LLC", "filed_date": "2026-02-02",
     "text": "Unrelated rate case filing, no security standard mentioned."},
    {"party_name": "American Electric Power", "filed_date": "2019-01-01",
     "text": "Old filing, outside the trailing 24-month window."},
])


def fixture_docs():
    return probe.parse_documents(DOCS_JSON)


class TestParsers(unittest.TestCase):
    def test_parses_party_filed_date_and_text(self):
        docs = probe.parse_documents(DOCS_JSON)
        self.assertEqual(len(docs), 4)
        self.assertEqual(docs[0], probe.FercDocument(
            "Dominion Energy", "2026-03-04",
            "Tariff filing under Docket No. ER26-100."))
        self.assertEqual(docs[0]._fields, ("party_name", "filed_date", "text"))

    def test_row_missing_party_name_is_dropped(self):
        docs = probe.parse_documents(json.dumps(
            [{"filed_date": "2026-01-01", "text": "CIP-005 reference"}]))
        self.assertEqual(docs, [])

    def test_row_missing_text_is_dropped(self):
        docs = probe.parse_documents(json.dumps(
            [{"party_name": "Acme Power", "filed_date": "2026-01-01"}]))
        self.assertEqual(docs, [])

    def test_undated_row_is_kept_but_carries_no_filed_date(self):
        docs = probe.parse_documents(json.dumps(
            [{"party_name": "Acme Power", "text": "no date field"}]))
        self.assertEqual(docs, [probe.FercDocument("Acme Power", "", "no date field")])

    def test_non_array_payload_raises(self):
        with self.assertRaises(ValueError):
            probe.parse_documents(json.dumps({"party_name": "Acme Power"}))


class TestWindow(unittest.TestCase):
    def test_cutoff_is_the_24_month_anniversary(self):
        self.assertEqual(probe.window_cutoff(NOW), "2024-08-17")

    def test_leap_day_cutoff_falls_back_to_feb_28(self):
        leap = datetime(2028, 2, 29, tzinfo=timezone.utc)
        self.assertEqual(probe.window_cutoff(leap), "2026-02-28")


class TestAnalysis(unittest.TestCase):
    """The four scenarios pinned by the unit brief: a watchlist-entity match,
    a CIP-### reference counted independent of party, a document naming
    neither, and window exclusion."""

    @classmethod
    def setUpClass(cls):
        cls.conn = fixture_conn()
        cls.result = probe.analyze(cls.conn, fixture_docs(),
                                   source_status={"data_ferc_gov": "ok",
                                                  "elibrary_general_search": "ok"},
                                   now=NOW)
        cls.text = probe.format_report(cls.result)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_watchlist_entity_named_as_party_is_a_hit(self):
        self.assertEqual(self.result["entity_hit_count"], 1)
        self.assertEqual(self.result["entity_hits"][0]["entity_id"], "E0004")
        self.assertEqual(self.result["entity_hits"][0]["party_names"],
                         ["Dominion Energy"])

    def test_cip_reference_counts_regardless_of_party_resolution(self):
        """The CIP-005 document's party ("Zephyr Bakery Collective") is
        off-list and resolves to nothing -- count 2 must still see it, and
        never be conflated with count 1."""
        self.assertEqual(self.result["cip_document_count"], 1)
        self.assertEqual(self.result["cip_standards_referenced"], ["CIP-005"])
        self.assertNotIn("Zephyr Bakery Collective",
                         [h["party_names"] for h in self.result["entity_hits"]])

    def test_document_naming_neither_is_unmatched_in_both_counts(self):
        """"Acme Widgets LLC" resolves to no entity and its text has no CIP
        reference -- it must not appear as a hit or inflate the CIP count."""
        self.assertNotIn("Acme Widgets LLC",
                         [n for h in self.result["entity_hits"]
                          for n in h["party_names"]])
        self.assertEqual(self.result["unmatched_count"], 2)  # Zephyr + Acme
        self.assertEqual(self.result["cip_document_count"], 1)

    def test_out_of_window_document_is_excluded_from_both_counts(self):
        """American Electric Power is named in 2019, outside the trailing
        24-month window, so it contributes to neither count."""
        self.assertEqual(self.result["in_window_documents"], 3)
        self.assertEqual(self.result["total_documents"], 4)
        self.assertNotIn("E0005",
                         [h["entity_id"] for h in self.result["entity_hits"]])

    def test_collision_name_is_review_queued_never_matched(self):
        result = probe.analyze(
            self.conn,
            [probe.FercDocument("Dominion", "2026-01-01", "no cip ref")],
            source_status={}, now=NOW)
        self.assertEqual(result["entity_hit_count"], 0)
        self.assertEqual(len(result["review"]), 1)
        self.assertEqual(result["review"][0]["reason"], "collision_term")

    def test_report_wording_is_ascii(self):
        self.text.encode("ascii")

    def test_report_states_both_counts_with_their_definitions(self):
        self.assertIn("COUNT 1 (account-recall-shaped)", self.text)
        self.assertIn("distinct watchlist entities named as filer/party: 1",
                      self.text)
        self.assertIn("COUNT 2 (independent of party)", self.text)
        self.assertIn("documents with a CIP-### reference: 1", self.text)


class TestRecommendation(unittest.TestCase):
    OK = {"data_ferc_gov": "ok", "elibrary_general_search": "ok"}
    BLOCKED = {"data_ferc_gov": "BLOCKED (403 Forbidden: API_KEY_MISSING)",
               "elibrary_general_search": "BLOCKED (Angular shell)"}

    def test_zero_on_both_is_stop_and_records_the_negative(self):
        text = probe.recommendation(0, 0, self.OK)
        self.assertIn("STOP", text)
        self.assertIn(probe.NEGATIVE_FINDING, text)

    def test_one_or_more_on_either_asks_for_an_operator_ruling(self):
        self.assertIn("OPERATOR RULING", probe.recommendation(1, 0, self.OK))
        self.assertIn("OPERATOR RULING", probe.recommendation(0, 1, self.OK))

    def test_blocked_source_is_not_a_measured_zero(self):
        text = probe.recommendation(0, 0, self.BLOCKED)
        self.assertIn("BLOCKED MEASUREMENT", text)
        self.assertIn("data_ferc_gov", text)
        self.assertIn("elibrary_general_search", text)
        self.assertIn("not because a completed search found nothing", text)


class TestLiveProbes(unittest.TestCase):
    """collect()/probe_*() are the only places a live request happens.
    Hermetic: urllib.request.urlopen is replaced with fakes that reproduce
    the two shapes actually observed live (see the module docstring), and no
    real socket is opened."""

    def _patch_urlopen(self, fake):
        original = probe.urllib.request.urlopen
        probe.urllib.request.urlopen = fake
        self.addCleanup(setattr, probe.urllib.request, "urlopen", original)

    def test_data_ferc_gov_403_is_reported_as_blocked_not_ok(self):
        def fake_urlopen(req, timeout=None):
            body = json.dumps({"error": {"code": "API_KEY_MISSING",
                                         "message": "No api_key was supplied."}})
            raise urllib.error.HTTPError(
                req.full_url, 403, "Forbidden", {},
                io.BytesIO(body.encode("utf-8")))
        self._patch_urlopen(fake_urlopen)
        status = probe.probe_data_ferc_gov()
        self.assertIn("BLOCKED", status)
        self.assertIn("403", status)
        self.assertIn("API_KEY_MISSING", status)

    def test_data_ferc_gov_request_never_carries_an_api_key(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.header_items())
            captured["url"] = req.full_url
            raise urllib.error.HTTPError(
                req.full_url, 403, "Forbidden", {}, io.BytesIO(b"{}"))
        self._patch_urlopen(fake_urlopen)
        probe.probe_data_ferc_gov()
        self.assertNotIn("X-Api-Key", captured["headers"])
        self.assertNotIn("api_key", captured["url"].lower())

    def test_elibrary_angular_shell_is_reported_as_blocked_not_ok(self):
        shell = "<!DOCTYPE html><html><body><app-root></app-root></body></html>"

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return shell.encode("utf-8")

        self._patch_urlopen(lambda req, timeout=None: FakeResponse())
        status = probe.probe_elibrary()
        self.assertIn("BLOCKED", status)
        self.assertIn("client-rendered Angular SPA shell", status)

    def test_collect_never_yields_documents_when_both_sources_are_blocked(self):
        """Reproduces the two shapes actually observed live: data.ferc.gov
        403s with API_KEY_MISSING, elibrary 200s with the Angular shell."""
        shell = "<!DOCTYPE html><html><body><app-root></app-root></body></html>"

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return shell.encode("utf-8")

        def fake_urlopen(req, timeout=None):
            if "api.data.ferc.gov" in req.full_url:
                raise urllib.error.HTTPError(
                    req.full_url, 403, "Forbidden", {}, io.BytesIO(b"{}"))
            return FakeResponse()
        self._patch_urlopen(fake_urlopen)
        original_sleep = probe.time.sleep
        probe.time.sleep = lambda s: None
        try:
            docs, status = probe.collect()
        finally:
            probe.time.sleep = original_sleep
        self.assertEqual(docs, [])
        self.assertIn("BLOCKED", status["data_ferc_gov"])
        self.assertIn("BLOCKED", status["elibrary_general_search"])


class FakeJsonResponse:
    """Minimal context-manager response used by TestCatalogEnumeration --
    mirrors TestLiveProbes' FakeResponse but for JSON dataset bodies."""

    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body.encode("utf-8")


def _dataset_body(dataset_id, title, description, row_count=1):
    return json.dumps({"metadata": [{
        "id": dataset_id, "security-level": "Public", "title": title,
        "description": description, "industry": "Natural Gas",
        "url": f"https://data.ferc.gov/{dataset_id}"}],
        "row_count": row_count})


class TestCatalogEnumeration(unittest.TestCase):
    """U8c: probe_data_ferc_gov_catalog() / _fetch_dataset_details() are the
    only places the authenticated catalog request happens. Hermetic:
    urllib.request.urlopen is replaced with fakes keyed on the request URL's
    dataset id, no real socket is opened, and time.sleep is a no-op."""

    FAKE_KEY = "test-key-not-a-real-secret"

    def setUp(self):
        original_sleep = probe.time.sleep
        probe.time.sleep = lambda s: None
        self.addCleanup(setattr, probe.time, "sleep", original_sleep)

    def _patch_urlopen(self, responder):
        """``responder(dataset_id, req)`` -> a FakeJsonResponse, or raises
        urllib.error.HTTPError to simulate a 404/other failure."""
        def fake_urlopen(req, timeout=None):
            dataset_id = int(req.full_url.split("/dataset/")[1].split("/")[0])
            return responder(dataset_id, req)
        original = probe.urllib.request.urlopen
        probe.urllib.request.urlopen = fake_urlopen
        self.addCleanup(setattr, probe.urllib.request, "urlopen", original)

    @staticmethod
    def _http_404(req):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {},
                                     io.BytesIO(b"{}"))

    def test_200_response_is_parsed_into_id_title_description(self):
        def responder(dataset_id, req):
            self.assertEqual(dataset_id, 0)
            return FakeJsonResponse(_dataset_body(
                0, "Form 552 Master Table",
                "Database of natural gas transactions."))
        self._patch_urlopen(lambda dataset_id, req: responder(dataset_id, req)
                            if dataset_id == 0 else self._http_404(req))
        datasets, summary = probe.probe_data_ferc_gov_catalog(
            self.FAKE_KEY, max_id=0, max_consecutive_404s=1)
        self.assertEqual(len(datasets), 1)
        self.assertEqual(datasets[0]["id"], 0)
        self.assertEqual(datasets[0]["title"], "Form 552 Master Table")
        self.assertEqual(datasets[0]["description"],
                         "Database of natural gas transactions.")
        self.assertEqual(summary["ids_probed"], 1)
        self.assertEqual(summary["hit_count"], 1)

    def test_consecutive_404s_stop_enumeration_at_the_threshold(self):
        calls = []

        def responder(dataset_id, req):
            calls.append(dataset_id)
            self._http_404(req)  # every id 404s
        self._patch_urlopen(responder)
        datasets, summary = probe.probe_data_ferc_gov_catalog(
            self.FAKE_KEY, max_id=25, max_consecutive_404s=3)
        self.assertEqual(datasets, [])
        self.assertEqual(calls, [0, 1, 2])  # stops after the 3rd 404
        self.assertEqual(summary["ids_probed"], 3)
        self.assertTrue(summary["stopped_early"])

    def test_non_404_response_resets_the_consecutive_404_counter(self):
        # 404, 404, ok, 404, 404, 404 -- the "ok" at id 2 must reset the
        # counter, so the run does not stop until ids 3-5 give 3 in a row.
        def responder(dataset_id, req):
            if dataset_id == 2:
                return FakeJsonResponse(_dataset_body(
                    2, "Oil Assessment Table", "Oil Annual Charges Assessment"))
            self._http_404(req)
        self._patch_urlopen(responder)
        datasets, summary = probe.probe_data_ferc_gov_catalog(
            self.FAKE_KEY, max_id=25, max_consecutive_404s=3)
        self.assertEqual(len(datasets), 1)
        self.assertEqual(datasets[0]["id"], 2)
        self.assertEqual(summary["ids_probed"], 6)  # 0,1,2,3,4,5
        self.assertTrue(summary["stopped_early"])

    def test_key_never_appears_in_the_returned_dataset_url(self):
        captured = {}

        def responder(dataset_id, req):
            captured["url"] = req.full_url
            return FakeJsonResponse(_dataset_body(
                0, "Form 552 Master Table", "Natural gas transactions."))
        self._patch_urlopen(responder)
        datasets, _ = probe.probe_data_ferc_gov_catalog(
            self.FAKE_KEY, max_id=0, max_consecutive_404s=1)
        # The real request DID carry the key (proving auth happened)...
        self.assertIn(self.FAKE_KEY, captured["url"])
        # ...but the reported dataset url never does.
        self.assertNotIn(self.FAKE_KEY, datasets[0]["url"])
        self.assertNotIn("api_key", datasets[0]["url"])
        self.assertEqual(datasets[0]["url"],
                         "https://api.data.ferc.gov/v1/dataset/0/details/")

    def test_docket_filing_shaped_title_is_flagged_elibrary_adjacent(self):
        self.assertTrue(probe.is_elibrary_adjacent(
            "eLibrary Docket Filings Index",
            "Index of dockets, filings, orders, and correspondence."))

    def test_hydropower_statistical_title_is_not_flagged(self):
        self.assertFalse(probe.is_elibrary_adjacent(
            "Form 552 Master Table",
            "Database of Page 1 (Identification of Respondent) Database and "
            "Page 4 (Purchase and Sales Information) of Annual Report of "
            "Natural Gas Transactions Form (FERC No. 552)"))
        self.assertFalse(probe.is_elibrary_adjacent(
            "Active Hydropower Projects",
            "Database of active hydropower project licenses."))

    def test_bare_docket_number_or_order_citation_is_not_flagged(self):
        """Real datasets found live (PR body): both cite a docket/order in
        passing -- as an identifier field or a legal-basis citation -- while
        remaining structured administrative rosters, not filing indexes.
        Regression pin for the false positives this classifier produced
        before "docket"/"order" were dropped from the keyword list."""
        self.assertFalse(probe.is_elibrary_adjacent(
            "MBR Authorizations",
            "Basic information about a seller's market-based rate "
            "authorization, including the docket number in which it was "
            "first granted market-based rate authority and the associated "
            "tariff effective date."))
        self.assertFalse(probe.is_elibrary_adjacent(
            "MBR Operating Reserves",
            "Basic information about sellers authorized to make "
            "third-party sales of operating reserves to a public utility "
            "that is purchasing ancillary services to satisfy its own open "
            "access transmission tariff requirements to offer ancillary "
            "services to its own customers. See Order No. 784."))


class TestContainment(unittest.TestCase):
    """Same #74 precedent as breach_registry_probe: a measurement harness
    that could write is not a measurement, and one wired into the pipeline
    is a fetcher."""

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
            result = probe.report(db_path, fixture_docs(),
                                  source_status={}, now=NOW, out=out)
            self.assertEqual(result["entity_hit_count"], 1)
            self.assertIn("distinct watchlist entities named as filer/party: 1",
                          out.getvalue())
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

    def test_module_never_hardcodes_an_api_key(self):
        """The unit brief is explicit: a required key must be reported as a
        finding, never silently worked around. There is no key literal, no
        X-Api-Key header, and no api_key query parameter anywhere."""
        lowered = MODULE_SRC.lower()
        self.assertNotIn("x-api-key", lowered)
        self.assertNotIn("api_key=", lowered)

    def test_probe_is_absent_from_the_ingest_pipeline(self):
        pipeline = PIPELINE.read_text(encoding="utf-8")
        self.assertNotIn("ferc_elibrary_probe", pipeline)
        self.assertNotIn("app.spikes", pipeline)


if __name__ == "__main__":
    unittest.main()
