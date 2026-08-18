"""CISA ICS advisories classifier tests (R9.6, R7.2, R4.1, R10.6).

Hermetic: real migrations against in-memory SQLite, FK on, canned advisory
payloads mirroring app/ingest/cisa_ics.py's stored shape, run end-to-end
through the classification framework (run_classifier). Covers the
energy-sector match (including a multi-sector list and an exact-token
match that must not false-positive on a substring), the non-energy drop,
re-run idempotence, the sector-only scope grant (no candidate ever carries
an entity_id), and the dropped_scope failure mode a future seed regression
would hit if the grant were ever removed.
"""
import json
import sqlite3
import unittest

from app.classify import cisa_ics
from app.classify.runner import run_classifier
from app.db.migrate import apply_migrations

SRC = cisa_ics.SOURCE_ID


def fixture_conn(allowed_scopes=("sector",)):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO source_policies (source_id, name, evidence_rank) "
        "VALUES (?, 'CISA ICS Advisories RSS', 1)", (SRC,))
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        " decay_half_life_days, mvp_flag, evidence_quality, "
        " allowed_scopes) VALUES (?, ?, 3, 135, 0, 'IR', ?)",
        (cisa_ics.TRIGGER_ID, cisa_ics.TRIGGER_ID,
         json.dumps(list(allowed_scopes))))
    conn.commit()
    return conn


def _description(sectors, extra=""):
    return (
        "<p><strong>Vendor:</strong> ACME</p>"
        f"<li><strong>Critical Infrastructure Sectors: </strong>{sectors}</li>"
        f"<li><strong>Countries/Areas Deployed: </strong>Worldwide</li>{extra}")


def add_advisory(conn, advisory_id, sectors, title="ACME Widget Controller",
                 event_date="2026-08-13", extra_description=""):
    payload = {
        "title": title,
        "link": f"https://www.cisa.gov/news-events/ics-advisories/{advisory_id}",
        "guid": f"/node/{advisory_id}",
        "description": _description(sectors, extra_description),
        "pubDate": "Thu, 13 Aug 26 12:00:00 +0000",
    }
    raw_event_id = f"{SRC}:{advisory_id}"
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
        " payload, url, first_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
        (raw_event_id, SRC, event_date, json.dumps(payload, sort_keys=True),
         payload["link"], f"{event_date}T00:00:00Z"))
    conn.commit()
    return raw_event_id


class CisaIcsTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def run_it(self, **kw):
        return run_classifier(self.conn, cisa_ics.CLASSIFIER_ID, SRC,
                              cisa_ics.classify_cisa_ics,
                              cisa_ics.PARSER_VERSION, **kw)

    def signals(self):
        return self.conn.execute(
            "SELECT * FROM signals ORDER BY signal_id").fetchall()


class TestEnergySectorMatch(CisaIcsTestCase):
    def test_single_energy_sector_yields_one_sector_signal(self):
        rid = add_advisory(self.conn, "icsa-26-225-05", "Energy")
        s = self.run_it()
        self.assertEqual((s["status"], s["signals_new"]), ("success", 1))
        sigs = self.signals()
        self.assertEqual(len(sigs), 1)
        sig = sigs[0]
        self.assertEqual(sig["trigger_id"], "ics_cve_kev")
        self.assertEqual(sig["signal_id"], f"ics_cve_kev:{rid}:sector")
        self.assertEqual(sig["signal_scope"], "sector")
        self.assertIsNone(sig["entity_id"])
        self.assertEqual(sig["event_date"], "2026-08-13")
        self.assertIn("ACME Widget Controller", sig["headline"])
        ev = self.conn.execute(
            "SELECT evidence_text FROM signal_evidence WHERE signal_id = ?",
            (sig["signal_id"],)).fetchall()
        self.assertTrue(ev)
        self.assertTrue(any("Energy" in row["evidence_text"] for row in ev))

    def test_multi_sector_list_including_energy_matches(self):
        add_advisory(self.conn, "icsa-26-225-06",
                     "Energy, Critical Manufacturing")
        s = self.run_it()
        self.assertEqual(s["signals_new"], 1)

    def test_exact_token_match_does_not_false_positive_on_substring(self):
        # A hypothetical sector name containing "energy" as a substring but
        # not equal to it must NOT match (exact comma-split token compare).
        add_advisory(self.conn, "icsa-26-225-07", "Renewable Energy Storage")
        s = self.run_it()
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(len(self.signals()), 0)


class TestNonEnergyDropped(CisaIcsTestCase):
    def test_non_energy_sector_yields_no_signal(self):
        add_advisory(self.conn, "icsa-26-225-08", "Information Technology")
        s = self.run_it()
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(len(self.signals()), 0)

    def test_no_sectors_list_yields_no_signal(self):
        payload = {
            "title": "No Sectors Advisory",
            "link": "https://www.cisa.gov/news-events/ics-advisories/icsa-26-225-09",
            "guid": "/node/1",
            "description": "<p>No sectors line here.</p>",
            "pubDate": "Thu, 13 Aug 26 12:00:00 +0000",
        }
        self.conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
            " payload, url, first_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
            (f"{SRC}:icsa-26-225-09", SRC, "2026-08-13",
             json.dumps(payload, sort_keys=True), payload["link"],
             "2026-08-13T00:00:00Z"))
        self.conn.commit()
        s = self.run_it()
        self.assertEqual(s["signals_new"], 0)


class TestIdempotence(CisaIcsTestCase):
    def test_rerun_emits_no_new_signals(self):
        add_advisory(self.conn, "icsa-26-225-05", "Energy")
        self.run_it()
        s2 = self.run_it(force=True)
        self.assertEqual(s2["signals_new"], 0)
        self.assertEqual(s2["signals_existing"], 1)
        self.assertEqual(len(self.signals()), 1)


class TestNoEntityEverAttached(CisaIcsTestCase):
    def test_no_signal_ever_carries_an_entity_id(self):
        add_advisory(self.conn, "icsa-26-225-05", "Energy")
        add_advisory(self.conn, "icsa-26-225-06",
                     "Energy, Critical Manufacturing")
        self.run_it()
        sigs = self.signals()
        self.assertGreaterEqual(len(sigs), 2)
        for sig in sigs:
            self.assertIsNone(sig["entity_id"])


class TestScopeGrantEnforced(unittest.TestCase):
    """If a future seed regression drops the sector grant, the candidate must
    be DROPPED (loud, countable), not silently mint an unscoped signal."""

    def test_missing_sector_grant_drops_the_candidate(self):
        conn = fixture_conn(allowed_scopes=())   # no scopes granted at all
        try:
            add_advisory(conn, "icsa-26-225-05", "Energy")
            s = run_classifier(conn, cisa_ics.CLASSIFIER_ID, SRC,
                               cisa_ics.classify_cisa_ics,
                               cisa_ics.PARSER_VERSION)
            self.assertEqual(s["signals_new"], 0)
            self.assertEqual(s["dropped_scope"], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
