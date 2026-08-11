"""Tests for NERC page snapshot ingestion (R3.7, R10.4).

Hermetic: canned HTML via a mocked HTTP layer, in-memory SQLite with FK
enforcement, no network. What must not regress: visible-text extraction,
fetch-date-independent content hashing, and changed-page-becomes-new-event
dedupe semantics.
"""
import json
import sqlite3
import unittest
from unittest import mock

from app.db.migrate import apply_migrations
from app.ingest import nerc_pages, runner

HTML = """<html><head>
<script>var viewstate = "junk123";</script>
<style>.x { color: red; }</style>
</head><body>
  <h1>Standards   Under Development</h1>
  <p>CIP-015-1 &amp; INSM
     status: pending</p>
</body></html>"""


class TestStripHtml(unittest.TestCase):
    def test_script_style_dropped_entities_unescaped_whitespace_collapsed(self):
        text = nerc_pages.strip_html(HTML)
        self.assertEqual(
            text,
            "Standards Under Development CIP-015-1 & INSM status: pending")
        self.assertNotIn("viewstate", text)
        self.assertNotIn("color", text)


class TestPageEvent(unittest.TestCase):
    def test_hash_excludes_fetch_date(self):
        url = "https://www.nerc.com/x.aspx"
        with mock.patch.object(nerc_pages, "_today",
                               return_value="2026-08-01"):
            e1 = nerc_pages.page_event(url, HTML)
        with mock.patch.object(nerc_pages, "_today",
                               return_value="2026-08-02"):
            e2 = nerc_pages.page_event(url, HTML)
        self.assertEqual(e1["content_hash"], e2["content_hash"])
        self.assertNotEqual(e1["payload"], e2["payload"])

    def test_cosmetic_whitespace_does_not_change_hash(self):
        url = "https://www.nerc.com/x.aspx"
        reformatted = HTML.replace("  ", "\n\t ")
        self.assertEqual(nerc_pages.page_event(url, HTML)["content_hash"],
                         nerc_pages.page_event(url, reformatted)["content_hash"])

    def test_different_pages_same_text_hash_differs(self):
        self.assertNotEqual(
            nerc_pages.page_event("https://a.example", HTML)["content_hash"],
            nerc_pages.page_event("https://b.example", HTML)["content_hash"])

    def test_payload_fields(self):
        payload = json.loads(
            nerc_pages.page_event("https://a.example", HTML)["payload"])
        self.assertEqual(set(payload), {"page_url", "fetched_date", "text"})
        self.assertEqual(payload["page_url"], "https://a.example")


class TestRunSourceIntegration(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO source_policies (source_id, name, ttl, enabled) "
            "VALUES ('nerc_pages', 'NERC pages', 3600, 1)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _run(self, html_by_url):
        with mock.patch.object(nerc_pages, "PAGE_URLS",
                               list(html_by_url)), \
             mock.patch.object(nerc_pages, "_get_text",
                               side_effect=lambda u, retries=2: html_by_url[u]):
            return runner.run_source(
                self.conn, "nerc_pages", nerc_pages.fetch_events,
                nerc_pages.PARSER_VERSION, force=True)

    def test_snapshot_then_unchanged_then_changed(self):
        pages = {"https://nerc.example/dev.aspx": HTML,
                 "https://nerc.example/cip.aspx": "<html><body>CIP table"
                                                 "</body></html>"}
        first = self._run(pages)
        self.assertEqual(first["status"], "success")
        self.assertEqual(first["records_new"], 2)

        # unchanged pages (cosmetic whitespace only) -> all 'seen'
        pages_ws = {u: h.replace(" ", "  ") for u, h in pages.items()}
        second = self._run(pages_ws)
        self.assertEqual(second["records_seen"], 2)
        self.assertEqual(second["records_new"], 0)

        # one page's text changes -> exactly one new raw_event
        pages_changed = dict(pages)
        pages_changed["https://nerc.example/cip.aspx"] = (
            "<html><body>CIP table updated</body></html>")
        third = self._run(pages_changed)
        self.assertEqual(third["records_new"], 1)

        ids = [r["raw_event_id"] for r in self.conn.execute(
            "SELECT raw_event_id FROM raw_events").fetchall()]
        self.assertEqual(len(ids), 3)
        self.assertTrue(all(i.startswith("nerc_pages:h:") for i in ids))

    def test_limit(self):
        pages = {"https://nerc.example/a.aspx": "<p>a</p>",
                 "https://nerc.example/b.aspx": "<p>b</p>"}
        with mock.patch.object(nerc_pages, "PAGE_URLS", list(pages)), \
             mock.patch.object(nerc_pages, "_get_text",
                               side_effect=lambda u, retries=2: pages[u]):
            events = list(nerc_pages.fetch_events(None, 365, 1))
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
