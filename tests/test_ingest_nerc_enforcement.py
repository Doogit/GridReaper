"""Tests for the NERC enforcement docket fetcher (R5.5, R3.7, R10.4).

Hermetic: canned year-page HTML through a mocked HTTP layer, in-memory
SQLite with FK enforcement, no network. What must not regress: extraction
of the embedded window._model blob, header-row rejection, tolerance for the
broken date runs the published tables actually contain, idempotent re-fetch,
and an amended (errata) row becoming a new raw_event.
"""
import json
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest import mock

from app.db.migrate import apply_migrations
from app.ingest import nerc_enforcement, runner

ELIB = "https://elibrary.ferc.gov/eLibrary/filelist?accession_number="

ROW_SIMPLE = """<tr>
<td>NP26-12-000</td>
<td><p><a href="{elib}20260730-5138">July 2026 Spreadsheet Notice of
Penalty under NP26-12</a></p></td>
<td><p>7/30/2026</p></td>
</tr>""".format(elib=ELIB)

ROW_ERRATA = """<tr>
<td>NP26-4-000</td>
<td><p><a href="{elib}20260129-5276">January 2026 Spreadsheet Notice of
Penalty under NP26-4</a></p>
<p><a href="{elib}20260218-5030">Errata to January 2026 Spreadsheet Notice
of Penalty under NP26-4</a></p></td>
<td><p>1/29/2026</p><p>2/18/2026</p></td>
</tr>""".format(elib=ELIB)

# The published 2019 table really does contain '11 / 7 /20 19'.
ROW_BROKEN_DATE = """<tr>
<td>NP26-2-000</td>
<td><p><a href="/programs/enforcement/nop.pdf">Notice of Penalty regarding
an Unidentified Registered Entity under NP26-2</a></p></td>
<td><p>10 / 3 /20 26</p></td>
</tr>"""

HEADER_ROW = ("<tr><th>Docket Number</th><th>Title/Summary</th>"
              "<th>Date</th></tr>")


def _page(rows):
    table = ("<table><tbody>" + HEADER_ROW + "".join(rows)
             + "</tbody></table>")
    model = {"name": "2026",
             "pageModel": {"hero": {"title": "2026 NERC Enforcement Filings"},
                           "content": [{"model": {"heading": "2026 Filings",
                                                  "tableHtml": table}}]}}
    return ("<!DOCTYPE html><html><head><script>\n"
            "        window._model = " + json.dumps(model) + ";\n"
            "</script></head><body><div id=\"app\"></div></body></html>")


ALL_ROWS = [ROW_SIMPLE, ROW_ERRATA, ROW_BROKEN_DATE]


class ExtractModelTest(unittest.TestCase):
    def test_brace_matching_survives_trailing_script(self):
        model = nerc_enforcement.extract_model(_page([ROW_SIMPLE]))
        self.assertEqual(model["name"], "2026")

    def test_missing_blob_is_a_loud_failure(self):
        with self.assertRaises(ValueError):
            nerc_enforcement.extract_model("<html><body>nothing</body></html>")


class ParseDatesTest(unittest.TestCase):
    def test_broken_whitespace_runs(self):
        self.assertEqual(nerc_enforcement.parse_dates("11 / 7 /20 19"),
                         ["2019-11-07"])

    def test_multiple_dates_in_order(self):
        self.assertEqual(nerc_enforcement.parse_dates("1/29/2026 2/18/2026"),
                         ["2026-01-29", "2026-02-18"])

    def test_impossible_date_is_dropped(self):
        self.assertEqual(nerc_enforcement.parse_dates("13/45/2026"), [])


class DocketEventTest(unittest.TestCase):
    def _events(self, rows):
        return [e for e in
                (nerc_enforcement.docket_event("https://p/2026", 2026, cells)
                 for cells in nerc_enforcement.page_rows(_page(rows)))
                if e is not None]

    def test_header_row_is_rejected(self):
        self.assertEqual(len(self._events(ALL_ROWS)), 3)

    def test_docket_title_dates_and_links(self):
        payload = json.loads(self._events([ROW_ERRATA])[0]["payload"])
        self.assertEqual(payload["docket"], "NP26-4-000")
        self.assertIn("January 2026 Spreadsheet Notice of Penalty",
                      payload["title"])
        self.assertEqual(payload["filing_dates"],
                         ["2026-01-29", "2026-02-18"])
        self.assertEqual([lk["href"] for lk in payload["links"]],
                         [ELIB + "20260129-5276", ELIB + "20260218-5030"])
        self.assertEqual(payload["filing_year"], 2026)

    def test_event_date_is_the_first_filing_date(self):
        self.assertEqual([e["event_date"] for e in self._events(ALL_ROWS)],
                         ["2026-07-30", "2026-01-29", "2026-10-03"])

    def test_relative_link_is_absolutised(self):
        payload = json.loads(self._events([ROW_BROKEN_DATE])[0]["payload"])
        self.assertEqual(payload["links"][0]["href"],
                         "https://www.nerc.com/programs/enforcement/nop.pdf")

    def test_hash_excludes_the_page_url(self):
        header, row = nerc_enforcement.page_rows(_page([ROW_SIMPLE]))
        self.assertIsNone(
            nerc_enforcement.docket_event("https://p/2026", 2026, header))
        a = nerc_enforcement.docket_event("https://p/2026", 2026, row)
        b = nerc_enforcement.docket_event("https://other", 2026, row)
        self.assertEqual(a["content_hash"], b["content_hash"])


class FilingYearsTest(unittest.TestCase):
    def test_window_selects_years_newest_first(self):
        this_year = datetime.now(timezone.utc).year
        self.assertEqual(nerc_enforcement.filing_years(0), [this_year])
        self.assertEqual(nerc_enforcement.filing_years(400)[0], this_year)
        self.assertEqual(len(nerc_enforcement.filing_years(400)), 2)

    def test_floored_at_earliest_published_year(self):
        years = nerc_enforcement.filing_years(200000)
        self.assertGreaterEqual(min(years), nerc_enforcement.EARLIEST_YEAR)
        self.assertLessEqual(len(years), nerc_enforcement.MAX_YEARS)


class FetchEventsTest(unittest.TestCase):
    def test_limit_zero_skips_requests(self):
        with mock.patch.object(nerc_enforcement, "_get_text") as get_text:
            events = list(nerc_enforcement.fetch_events(None, 0, 0))
        self.assertEqual(events, [])
        get_text.assert_not_called()

    def test_limit(self):
        with mock.patch.object(nerc_enforcement, "_get_text",
                               lambda url, retries=2: _page(ALL_ROWS)), \
                mock.patch.object(nerc_enforcement.time, "sleep"):
            events = list(nerc_enforcement.fetch_events(None, 0, 2))
        self.assertEqual(len(events), 2)


class RunSourceIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO source_policies (source_id, name, ttl, enabled) "
            "VALUES ('nerc_enforcement', 'NERC enforcement', 3600, 1)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _run(self, rows):
        with mock.patch.object(nerc_enforcement, "_get_text",
                               lambda url, retries=2: _page(rows)), \
                mock.patch.object(nerc_enforcement.time, "sleep"):
            return runner.run_source(
                self.conn, "nerc_enforcement", nerc_enforcement.fetch_events,
                nerc_enforcement.PARSER_VERSION, force=True, window_days=0)

    def test_dated_rows_then_idempotent_rerun_then_errata(self):
        first = self._run(ALL_ROWS)
        self.assertEqual(first["status"], "success")
        self.assertEqual(first["records_new"], 3)
        self.assertEqual(
            [r["event_date"] for r in self.conn.execute(
                "SELECT event_date FROM raw_events ORDER BY event_date")],
            ["2026-01-29", "2026-07-30", "2026-10-03"])

        second = self._run(ALL_ROWS)
        self.assertEqual(second["records_new"], 0)      # R10.4
        self.assertEqual(second["records_seen"], 3)

        # An errata appended to an existing docket row is a changed row, so
        # it becomes a new raw_event rather than being swallowed as 'seen'.
        amended = ROW_SIMPLE.replace(
            "<td><p>7/30/2026</p></td>",
            "<td><p>7/30/2026</p><p>8/12/2026</p></td>")
        third = self._run([amended, ROW_ERRATA, ROW_BROKEN_DATE])
        self.assertEqual(third["records_new"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM raw_events").fetchone()[0], 4)
        ids = [r["raw_event_id"] for r in self.conn.execute(
            "SELECT raw_event_id FROM raw_events")]
        self.assertTrue(all(i.startswith("nerc_enforcement:h:") for i in ids))


if __name__ == "__main__":
    unittest.main()
