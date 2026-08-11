"""Tests for the SEC EDGAR submissions fetcher (R5.2, R5.5, R10.4).

Hermetic: in-memory SQLite via apply_migrations, FK on; the HTTP layer is
mocked with canned submissions JSON. What must not regress: the form and
window filters, event-dict shape (accession id, archive URL, payload
contents), items parsing, the limit contract, and idempotent re-runs
through the runner.
"""
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.db.migrate import apply_migrations
from app.ingest import edgar, runner

TODAY = datetime.now(timezone.utc).date()
RECENT = (TODAY - timedelta(days=10)).isoformat()
OLD = (TODAY - timedelta(days=400)).isoformat()

RECENT_KEYS = ["accessionNumber", "filingDate", "form", "items",
               "primaryDocument"]


def submissions(filings):
    """Build a canned submissions payload from per-filing dicts."""
    recent = {k: [f.get(k, "") for f in filings] for k in RECENT_KEYS}
    return {"filings": {"recent": recent}}


def filing(accession, form="8-K", date=RECENT, items="", doc="doc.htm"):
    return {"accessionNumber": accession, "filingDate": date, "form": form,
            "items": items, "primaryDocument": doc}


class EdgarTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO source_policies (source_id, name, ttl, enabled) "
            "VALUES ('sec_edgar_submissions', 'SEC EDGAR', 86400, 1)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def add_entity(self, entity_id, cik):
        self.conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, cik) "
            "VALUES (?, ?, ?)", (entity_id, f"{entity_id} Corp", cik))
        self.conn.commit()

    def fetch_all(self, canned, window_days=365, limit=None):
        """Run fetch_events with _get_json returning canned data: either one
        submissions payload for every URL, or a {url: payload} map."""
        calls = []

        def fake_get_json(url, retries=2):
            calls.append(url)
            return canned[url] if "filings" not in canned else canned

        with mock.patch.object(edgar, "_get_json", side_effect=fake_get_json), \
                mock.patch("time.sleep"):
            events = list(edgar.fetch_events(self.conn, window_days, limit))
        return events, calls


class TestFetchEvents(EdgarTestCase):
    def test_form_and_window_filter(self):
        self.add_entity("E1", "1466593")
        canned = submissions([
            filing("acc-1", form="8-K", date=RECENT),
            filing("acc-2", form="4", date=RECENT),          # wrong form
            filing("acc-3", form="10-K", date=OLD),           # outside window
            filing("acc-4", form="10-K/A", date=RECENT),
            filing("acc-5", form="8-K/A", date=RECENT),
        ])
        events, _ = self.fetch_all(canned)
        self.assertEqual([e["source_native_id"] for e in events],
                         ["acc-1", "acc-4", "acc-5"])

    def test_event_shape_and_archive_url(self):
        self.add_entity("E1", "1466593")
        canned = submissions([filing(
            "0001466593-26-000123", form="8-K", date=RECENT,
            items="5.02,9.01", doc="dvn-8k.htm")])
        events, _ = self.fetch_all(canned)
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["source_native_id"], "0001466593-26-000123")
        self.assertEqual(e["event_date"], RECENT)
        self.assertEqual(
            e["url"],
            "https://www.sec.gov/Archives/edgar/data/1466593/"
            "000146659326000123/dvn-8k.htm")
        payload = json.loads(e["payload"])
        self.assertEqual(payload["entity_id"], "E1")
        self.assertEqual(payload["cik"], "1466593")
        self.assertEqual(payload["items"], ["5.02", "9.01"])
        self.assertEqual(payload["form"], "8-K")

    def test_empty_items_parsed_to_empty_list(self):
        self.add_entity("E1", "1466593")
        canned = submissions([filing("acc-1", items="")])
        events, _ = self.fetch_all(canned)
        self.assertEqual(json.loads(events[0]["payload"])["items"], [])

    def test_missing_primary_document_falls_back_to_index(self):
        self.add_entity("E1", "1466593")
        canned = submissions([filing("0001466593-26-000001", doc="")])
        events, _ = self.fetch_all(canned)
        self.assertEqual(
            events[0]["url"],
            "https://www.sec.gov/Archives/edgar/data/1466593/"
            "000146659326000001/")

    def test_cik_zero_padded_in_request_url(self):
        self.add_entity("E1", "1466593")
        _, calls = self.fetch_all(submissions([]))
        self.assertEqual(
            calls, ["https://data.sec.gov/submissions/CIK0001466593.json"])

    def test_limit_stops_mid_entity_and_skips_later_fetches(self):
        self.add_entity("E1", "1000")
        self.add_entity("E2", "2000")
        canned = {
            "https://data.sec.gov/submissions/CIK0000001000.json":
                submissions([filing("a-1"), filing("a-2")]),
            "https://data.sec.gov/submissions/CIK0000002000.json":
                submissions([filing("b-1")]),
        }
        events, calls = self.fetch_all(canned, limit=1)
        self.assertEqual([e["source_native_id"] for e in events], ["a-1"])
        self.assertEqual(len(calls), 1)   # second entity never fetched
        events, calls = self.fetch_all(canned, limit=3)
        self.assertEqual(len(events), 3)

    def test_limit_zero_skips_requests(self):
        self.add_entity("E1", "1000")
        events, calls = self.fetch_all(submissions([filing("a-1")]), limit=0)
        self.assertEqual(events, [])
        self.assertEqual(calls, [])


class TestRunSourceIntegration(EdgarTestCase):
    def test_run_and_rerun_dedupe(self):
        self.add_entity("E1", "1466593")
        canned = submissions([filing("acc-1"), filing("acc-2", form="10-K")])
        with mock.patch.object(edgar, "_get_json", return_value=canned), \
                mock.patch("time.sleep"):
            summary = runner.run_source(
                self.conn, edgar.SOURCE_ID, edgar.fetch_events,
                edgar.PARSER_VERSION)
            self.assertEqual(summary["status"], "success")
            self.assertEqual(summary["records_seen"], 2)
            self.assertEqual(summary["records_new"], 2)
            again = runner.run_source(
                self.conn, edgar.SOURCE_ID, edgar.fetch_events,
                edgar.PARSER_VERSION, force=True)
        self.assertEqual(again["status"], "success")
        self.assertEqual(again["records_new"], 0)
        row = self.conn.execute(
            "SELECT * FROM raw_events "
            "WHERE raw_event_id='sec_edgar_submissions:acc-1'").fetchone()
        self.assertEqual(row["source_native_id"], "acc-1")

    def test_http_failure_recorded_as_error_run(self):
        self.add_entity("E1", "1466593")
        with mock.patch.object(edgar, "_get_json",
                               side_effect=OSError("edgar down")), \
                mock.patch("time.sleep"):
            summary = runner.run_source(
                self.conn, edgar.SOURCE_ID, edgar.fetch_events,
                edgar.PARSER_VERSION)
        self.assertEqual(summary["status"], "error")
        self.assertIn("edgar down", summary["error_state"])


if __name__ == "__main__":
    unittest.main()
