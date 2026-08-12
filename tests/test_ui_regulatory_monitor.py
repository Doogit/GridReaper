"""Regulatory Monitor data + page AppTest suite (R8.4, R7.2, D8).

Hermetic: a temp-file SQLite DB (not :memory: — AppTest opens its own
connection) built by apply_migrations + fixture rows, pointed at the page via
the GRIDSIGNALS_DB env override. AppTest runs no server and no network.

Covers the definition of non-graduated regulatory chatter: only regulatory
sources, only raw_events with NO signals row referencing them; the headline is
the source's own text quoted verbatim with no action verb; no signal fields
leak; nerc_pages snapshots shape correctly; the anchor flag is descriptive; a
malformed payload survives; and the page renders/disclaims graduation.
"""
import json
import os
import sqlite3
import tempfile
import unittest

from streamlit.testing.v1 import AppTest

from app.db.migrate import apply_migrations
from app.ui import data

PAGE = "app/ui/pages/6_Regulatory_Monitor.py"

# Exact Federal Register title of the non-graduated chatter record; the headline
# must quote it verbatim.
CHATTER_TITLE = "Combined Notice of Filings; Meeting Notices; Sunshine Act"
GRAD_TITLE = "Critical Infrastructure Protection Reliability Standards"
NONREG_TITLE = "Acme Energy names new CISO"


def _add_raw_event(conn, raw_event_id, source_id, event_date, payload, url=""):
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, payload, "
        "url, first_seen_at) VALUES (?,?,?,?,?,?)",
        (raw_event_id, source_id, event_date, json.dumps(payload), url,
         event_date + "T00:00:00+00:00"))


def seed(conn):
    # Source policies (FK targets). Two regulatory + one non-regulatory.
    conn.executemany(
        "INSERT INTO source_policies (source_id, name, enabled, ttl, "
        "access_method, evidence_rank) VALUES (?,?,1,3600,?,?)",
        [("federal_register", "Federal Register", "json", 1),
         ("nerc_pages", "NERC Standards Pages", "html", 1),
         ("presswire", "Press Wire", "rss", 3)])

    # A trigger + entity the graduated signal needs (FK).
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('nerc_cip_revision','CIP',5,600)")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name) "
        "VALUES ('E_ACME','Acme Energy')")

    # (1) GRADUATED regulatory: a federal_register Rule WITH a signals row ->
    # must be EXCLUDED.
    _add_raw_event(conn, "re_reg_grad", "federal_register", "2026-08-05", {
        "title": GRAD_TITLE, "type": "Rule", "effective_on": "2026-10-01",
        "agencies": [{"name": "Federal Energy Regulatory Commission",
                      "slug": "federal-energy-regulatory-commission"}]},
        url="http://fr/grad")
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, signal_scope, "
        "trigger_id, event_date, headline, evidence_snippet, source_url, "
        "confidence, evidence_quality, customer_facing_allowed, score, status) "
        "VALUES ('sig_grad','re_reg_grad',NULL,'sector','nerc_cip_revision',"
        "'2026-08-05','FERC final rule','ev','http://fr/grad',0.8,'PR',1,2.75,"
        "'active')")

    # (2) NON-GRADUATED regulatory chatter: a federal_register Notice, no signal
    # -> must be INCLUDED. No effective_on / comments_close_on -> no anchor.
    _add_raw_event(conn, "re_reg_chatter", "federal_register", "2026-08-10", {
        "title": CHATTER_TITLE, "type": "Notice",
        "agencies": [{"name": "Federal Energy Regulatory Commission",
                      "slug": "federal-energy-regulatory-commission"}],
        "docket_ids": ["ER26-1"]},
        url="http://fr/chatter")

    # (3) NON-REGULATORY: a presswire event with no signal -> must be EXCLUDED.
    _add_raw_event(conn, "re_nonreg", "presswire", "2026-08-11",
                   {"title": NONREG_TITLE}, url="http://pw/1")


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


class RegulatoryTestBase(unittest.TestCase):
    def setUp(self):
        self.path = make_db()
        os.environ["GRIDSIGNALS_DB"] = self.path
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")

    def tearDown(self):
        self.conn.close()
        os.environ.pop("GRIDSIGNALS_DB", None)
        try:
            os.remove(self.path)
        except OSError:
            pass


class TestRegulatoryData(RegulatoryTestBase):
    def test_returns_only_non_graduated_regulatory(self):
        rows = data.regulatory_monitor(self.conn)
        self.assertEqual([r["raw_event_id"] for r in rows], ["re_reg_chatter"])

    def test_headline_verbatim_and_verbless(self):
        row = data.regulatory_monitor(self.conn)[0]
        self.assertEqual(
            row["headline"],
            f'Federal Energy Regulatory Commission notice: "{CHATTER_TITLE}"')
        self.assertIn(CHATTER_TITLE, row["headline"])
        for verb in ("adopts", "directs", "approves", "requires", "mandates"):
            self.assertNotIn(verb, row["headline"].lower())

    def test_long_federal_register_headline_balances_quotes(self):
        # P1b: a long title is truncated BEFORE the quotes are composed, so the
        # closing quote is always appended - the pair never dangles.
        long_title = (
            "Reliability Standard CIP-015-1 Internal Network Security "
            "Monitoring and the Associated Implementation Plan and Compliance "
            "Obligations for Registered Entities")
        _add_raw_event(self.conn, "re_long", "federal_register", "2026-08-04", {
            "title": long_title, "type": "Proposed Rule",
            "agencies": [{"name": "FERC"}]})
        self.conn.commit()
        rows = {r["raw_event_id"]: r for r in data.regulatory_monitor(self.conn)}
        headline = rows["re_long"]["headline"]
        self.assertLessEqual(len(headline), 140)
        self.assertTrue(headline.endswith('"'))       # closing quote present
        self.assertEqual(headline.count('"'), 2)       # exactly one balanced pair
        self.assertTrue(headline.rstrip('"').endswith("…"))  # ellipsis before it

    def test_no_signal_fields_leak(self):
        row = data.regulatory_monitor(self.conn)[0]
        for k in ("score", "confidence", "signal_scope", "entity_name",
                  "signal_id"):
            self.assertNotIn(k, row)

    def test_nerc_pages_row_shape(self):
        _add_raw_event(self.conn, "re_nerc", "nerc_pages", "2026-08-12", {
            "page_url": "https://www.nerc.com/pa/Stand/Pages/CIPStandards.aspx",
            "fetched_date": "2026-08-12",
            "text": "CIP-013-2 in effect"},
            url="https://www.nerc.com/pa/Stand/Pages/CIPStandards.aspx")
        self.conn.commit()
        rows = {r["raw_event_id"]: r for r in data.regulatory_monitor(self.conn)}
        nerc = rows["re_nerc"]
        self.assertEqual(nerc["agency"], "NERC")
        self.assertEqual(nerc["doc_type_label"], "page snapshot")
        # P1a: the headline uses the URL's last path segment, UNQUOTED (a raw URL
        # wrapped in quotes reads as a mis-parsed title).
        self.assertEqual(nerc["headline"], "NERC page snapshot: CIPStandards.aspx")
        self.assertNotIn('"', nerc["headline"])
        self.assertNotIn("https://", nerc["headline"])  # full URL stays in Source
        self.assertFalse(nerc["has_anchor"])

    def test_nerc_pages_missing_title_and_type_no_crash(self):
        _add_raw_event(self.conn, "re_nerc_bare", "nerc_pages", "2026-08-12",
                       {"text": "no page_url here"})
        self.conn.commit()
        rows = {r["raw_event_id"]: r for r in data.regulatory_monitor(self.conn)}
        # No page_url and no url -> title falls back to a safe snippet, no crash.
        self.assertIn("re_nerc_bare", rows)
        self.assertTrue(rows["re_nerc_bare"]["headline"])

    def test_anchor_flag(self):
        # chatter fixture has no anchor
        self.assertFalse(data.regulatory_monitor(self.conn)[0]["has_anchor"])
        # an anchored-but-still-chatter event (has comments_close_on, no signal)
        _add_raw_event(self.conn, "re_anchor", "federal_register", "2026-08-09", {
            "title": "Notice of Proposed Rulemaking on Something",
            "type": "Proposed Rule", "comments_close_on": "2026-09-30",
            "agencies": [{"name": "FERC"}]})
        self.conn.commit()
        rows = {r["raw_event_id"]: r for r in data.regulatory_monitor(self.conn)}
        self.assertTrue(rows["re_anchor"]["has_anchor"])
        self.assertEqual(rows["re_anchor"]["comments_close_on"], "2026-09-30")

    def test_malformed_payload_survives(self):
        self.conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
            "payload, url, first_seen_at) VALUES "
            "('re_bad','federal_register','2026-08-08','not json at all',"
            "'http://fr/bad','2026-08-08T00:00:00+00:00')")
        self.conn.commit()
        rows = {r["raw_event_id"]: r for r in data.regulatory_monitor(self.conn)}
        self.assertIn("re_bad", rows)                 # returned, not dropped
        self.assertTrue(rows["re_bad"]["headline"])   # no exception, has text

    def test_non_dict_json_payload_survives(self):
        # Valid JSON that is NOT an object (a list / a bare string) must hit the
        # isinstance(payload, dict) guard, not crash.
        self.conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
            "payload, url, first_seen_at) VALUES "
            "('re_list','federal_register','2026-08-07','[]',"
            "'http://fr/list','2026-08-07T00:00:00+00:00')")
        self.conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
            "payload, url, first_seen_at) VALUES "
            "('re_str','nerc_pages','2026-08-06','\"x\"',"
            "'http://nerc/str','2026-08-06T00:00:00+00:00')")
        self.conn.commit()
        rows = {r["raw_event_id"]: r for r in data.regulatory_monitor(self.conn)}
        for rid in ("re_list", "re_str"):
            self.assertIn(rid, rows)
            self.assertTrue(rows[rid]["headline"])


def _all_markdown(at):
    chunks = [el.value for el in at.markdown]
    chunks += [el.value for el in at.caption]
    chunks += [el.value for el in at.info]
    return "\n".join(chunks)


class TestRegulatoryPage(RegulatoryTestBase):
    def _run(self):
        at = AppTest.from_file(PAGE, default_timeout=30).run()
        self.assertEqual(
            list(at.exception), [],
            msg="; ".join(f"{e.type}: {e.message}" for e in at.exception))
        return at

    def test_renders_no_exception_and_chatter_shown(self):
        dom = _all_markdown(self._run())
        self.assertIn(CHATTER_TITLE, dom)
        # graduated + non-regulatory titles must NOT appear
        self.assertNotIn(GRAD_TITLE, dom)
        self.assertNotIn(NONREG_TITLE, dom)

    def test_framing_disclaims_graduation(self):
        dom = _all_markdown(self._run())
        # The load-bearing disclaimer sentence, verbatim from the banner.
        self.assertIn(
            "These are raw Federal Register / NERC records, **NOT** scored "
            "signals.", dom)
        # ...and it names the graduation criteria a record is missing.
        self.assertIn("graduates to the Signal Feed only when", dom)

    def test_chatter_record_has_no_severity_treatment(self):
        """R8.4/D8 regression lock: a chatter record renders as a neutral
        gs-record, never a gs-card with a severity score strip. Checked on the
        record markup only - the injected <style> block legitimately defines the
        .gs-card / sev-* rules, so we look for the class *attribute* on the
        rendered record div, not the classname anywhere in the stylesheet."""
        record = next(
            el.value for el in self._run().markdown
            if "class='gs-record'" in el.value)
        self.assertNotIn("class='gs-card'", record)   # not a signal-card wrapper
        self.assertNotIn("sev-", record)              # no severity band class

    def test_nerc_record_shows_no_compliance_clock_chip(self):
        """P2a: nerc_pages snapshots never carry a compliance clock, so the
        always-true 'no compliance clock' chip is suppressed for them; the
        record is labeled with its 'page snapshot' chip instead."""
        _add_raw_event(self.conn, "re_nerc", "nerc_pages", "2026-08-13", {
            "page_url": "https://www.nerc.com/pa/Stand/Pages/CIPStandards.aspx",
            "fetched_date": "2026-08-13", "text": "CIP-013-2 in effect"},
            url="https://www.nerc.com/pa/Stand/Pages/CIPStandards.aspx")
        self.conn.commit()
        record = next(
            el.value for el in self._run().markdown
            if "CIPStandards.aspx" in el.value and "gs-record" in el.value)
        self.assertNotIn("no compliance clock", record)
        self.assertIn("chatter", record)
        self.assertIn("page snapshot", record)

    def test_empty_state_renders(self):
        # Remove the only chatter row so the page hits its empty state.
        self.conn.execute("DELETE FROM raw_events WHERE raw_event_id = ?",
                           ("re_reg_chatter",))
        self.conn.commit()
        dom = _all_markdown(self._run())
        self.assertIn("No non-graduated regulatory chatter", dom)


if __name__ == "__main__":
    unittest.main()
