"""License-play snapshot tests (R7.6, R7.9, R7.10, R7.11).

Hermetic: real migrations against in-memory SQLite, FK on, canned config and
signals. What must not regress: snapshot immutability across re-runs and
version bumps, the gov-cloud suppression predicate (both arms), sector
phrasing for non-account scopes, and the R7.11 outreach-safety rules — no
price strings from non-primary facts, no assertions about the account's
current tier, discovery phrasing present.
"""
import json
import sqlite3
import unittest

from app.db.migrate import apply_migrations
from app.plays import GENERATION_VERSION, generate_snapshots

SOURCE = "test_source"
FAKE_PRICE = "$123 fake-price"
PRIMARY_PRICE = "$57/user/mo"
COPILOT_GCC_NOTE = "not available — NO (not in GCC/GCC-H/DoD)"
DISCOVERY_ENDPOINT = (
    "What licensing tier are you on today — E3 or E5? If E3, "
    "E3 -> E5 Security addon is the typical next step for "
    "Defender for Endpoint.")
DISCOVERY_COPILOT = (
    "What licensing tier are you on today — E3 or E5? If E3, "
    "E5 upgrade is the typical next step for Security Copilot.")

# tier assertions that must never appear in outreach_safe_text (R7.11)
FORBIDDEN_TIER_ASSERTIONS = [
    "you are on e3", "you are on e5", "you're on e3", "you're on e5",
    "your e3", "your e5", "your current e3", "your current e5",
]


def add_signal(conn, signal_id, entity_id, scope, trigger_id,
               customer_facing_allowed=1):
    # customer_facing_allowed defaults to 1 here to mirror production signals
    # (the framework sets it 1 for non-incident and confirmed-incident cards);
    # pass 0 to exercise the R7.12 opener-suppression gate.
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
        " signal_scope, trigger_id, event_date, headline, "
        " customer_facing_allowed) "
        "VALUES (?, ?, ?, ?, ?, '2026-08-01', 'headline', ?)",
        (signal_id, f"{SOURCE}:1", entity_id, scope, trigger_id,
         customer_facing_allowed))


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    for pid, name in [("def_endpoint", "Defender for Endpoint"),
                      ("sec_copilot", "Security Copilot")]:
        conn.execute(
            "INSERT INTO products (product_id, name) VALUES (?, ?)",
            (pid, name))
    for tid in ("own_incident", "reg_deadline", "t_empty"):
        conn.execute(
            "INSERT INTO triggers (trigger_id, name, base_strength, "
            " decay_half_life_days) VALUES (?, ?, 5, 30)", (tid, tid))
    facts = [
        # (fact_id, family, segment, price_note, source_quality)
        ("defender_endpoint_p2:e5:commercial", "def_endpoint",
         "commercial", PRIMARY_PRICE, "primary"),
        ("defender_endpoint_p2:e5:gcc_high", "def_endpoint",
         "gcc_high", "available — yes", "primary"),
        ("defender_endpoint_p1:e3:commercial", "def_endpoint",
         "commercial", FAKE_PRICE, "non-primary"),
        ("security_copilot_included:e5:commercial", "sec_copilot",
         "commercial", "included with E5 SCU grant", "primary"),
        ("security_copilot_included:e5:gcc_high", "sec_copilot",
         "gcc_high", COPILOT_GCC_NOTE, "primary"),
    ]
    for fact_id, family, segment, note, quality in facts:
        conn.execute(
            "INSERT INTO license_facts (fact_id, product_id, sku_or_plan, "
            " segment, price_note, included_or_addon, prerequisite, "
            " effective_date, verified_date, source_quality, source_url) "
            "VALUES (?, ?, ?, ?, ?, 'addon', '', '', '2026-08-11', ?, "
            " 'https://example.test')",
            (fact_id, family, fact_id.rsplit(":", 1)[0], segment, note,
             quality))
    candidates = [
        ("own_incident:def_endpoint", "own_incident", "def_endpoint",
         "E3 -> E5 Security addon", DISCOVERY_ENDPOINT),
        ("own_incident:sec_copilot", "own_incident", "sec_copilot",
         "E5 upgrade (included SCUs)", DISCOVERY_COPILOT),
        ("reg_deadline:def_endpoint", "reg_deadline", "def_endpoint",
         "E3 -> E5 Security addon", DISCOVERY_ENDPOINT),
    ]
    for play_id, tid, pid, path, question in candidates:
        conn.execute(
            "INSERT INTO license_play_candidates (play_id, trigger_id, "
            " product_id, assumed_baseline, recommended_path, "
            " discovery_question, rank_rule, config_version) "
            "VALUES (?, ?, ?, 'E3', ?, ?, '', 'licensing/1.0')",
            (play_id, tid, pid, path, question))
    for eid, likelihood in [("E_GOV", "likely"), ("E_COM", "unlikely")]:
        conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, "
            " gov_cloud_likelihood) VALUES (?, ?, ?)",
            (eid, eid, likelihood))
    # signals need raw_events + source_policies stubs for FKs
    conn.execute(
        "INSERT INTO source_policies (source_id, name) VALUES (?, 'Test')",
        (SOURCE,))
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id) VALUES (?, ?)",
        (f"{SOURCE}:1", SOURCE))
    add_signal(conn, "sig_gov", "E_GOV", "account", "own_incident")
    add_signal(conn, "sig_com", "E_COM", "account", "own_incident")
    add_signal(conn, "sig_sector", None, "sector", "reg_deadline")
    add_signal(conn, "sig_empty", None, "sector", "t_empty")
    conn.commit()
    return conn


class TestGenerateSnapshots(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def rows(self):
        return self.conn.execute(
            "SELECT * FROM license_play_snapshots "
            "ORDER BY signal_id, play_id").fetchall()

    def pairs(self):
        return {(r["signal_id"], r["play_id"]) for r in self.rows()}

    def by_pair(self):
        return {(r["signal_id"], r["play_id"]): r for r in self.rows()}

    def test_snapshot_rows_and_fact_ids(self):
        s = generate_snapshots(self.conn)
        self.assertEqual(s, {"snapshots_new": 4, "snapshots_existing": 0,
                             "suppressed_gov": 1, "signals_seen": 4})
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        known = {r["fact_id"] for r in self.conn.execute(
            "SELECT fact_id FROM license_facts")}
        for r in rows:
            self.assertEqual(r["generation_version"], GENERATION_VERSION)
            self.assertTrue(r["generated_at"])
            self.assertIn("Recommended path: ", r["display_text"])
            self.assertIn("Discovery: ", r["display_text"])
            ids = json.loads(r["fact_ids"])
            self.assertTrue(ids)
            self.assertTrue(set(ids) <= known)
            self.assertEqual(ids, sorted(ids))   # deterministic order
        # both segments of the family pinned, non-primary fact included
        endpoint = self.by_pair()[("sig_com", "own_incident:def_endpoint")]
        self.assertEqual(json.loads(endpoint["fact_ids"]), [
            "defender_endpoint_p1:e3:commercial",
            "defender_endpoint_p2:e5:commercial",
            "defender_endpoint_p2:e5:gcc_high"])

    def test_rerun_is_noop_even_with_new_version(self):
        generate_snapshots(self.conn)
        before = [tuple(r) for r in self.rows()]
        second = generate_snapshots(self.conn)
        self.assertEqual(second["snapshots_new"], 0)
        self.assertEqual(second["snapshots_existing"], 4)
        self.assertEqual([tuple(r) for r in self.rows()], before)
        # a DIFFERENT generation_version still never overwrites (R7.6)
        third = generate_snapshots(self.conn, generation_version="plays/9.9")
        self.assertEqual(third["snapshots_new"], 0)
        self.assertEqual(third["snapshots_existing"], 4)
        self.assertEqual([tuple(r) for r in self.rows()], before)

    def test_gov_likely_suppresses_sec_copilot_only(self):
        s = generate_snapshots(self.conn)
        self.assertEqual(s["suppressed_gov"], 1)
        pairs = self.pairs()
        self.assertNotIn(("sig_gov", "own_incident:sec_copilot"), pairs)
        self.assertIn(("sig_com", "own_incident:sec_copilot"), pairs)
        # non-copilot plays unaffected for the gov entity
        self.assertIn(("sig_gov", "own_incident:def_endpoint"), pairs)

    def test_tenant_environment_arm_suppresses(self):
        self.conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, "
            " gov_cloud_likelihood, tenant_cloud_environment) "
            "VALUES ('E_TEN', 'E_TEN', 'unlikely', 'gcc_high')")
        add_signal(self.conn, "sig_ten", "E_TEN", "account", "own_incident")
        self.conn.commit()
        s = generate_snapshots(self.conn)
        self.assertEqual(s["suppressed_gov"], 2)
        pairs = self.pairs()
        self.assertNotIn(("sig_ten", "own_incident:sec_copilot"), pairs)
        self.assertIn(("sig_ten", "own_incident:def_endpoint"), pairs)

    def test_sector_signal_never_suppressed_and_sector_phrased(self):
        generate_snapshots(self.conn)
        sector = [r for r in self.rows() if r["signal_id"] == "sig_sector"]
        self.assertEqual(len(sector), 1)
        text = sector[0]["outreach_safe_text"]
        # sector lead-in quotes the signal's own sourced event, then stays
        # class-phrased ("affected operators") - never invented sector color
        self.assertIn("affected operators", text)
        self.assertNotIn("your ", text.lower())
        self.assertNotIn("your recent event", text.lower())
        # account-scoped outreach IS account-phrased
        account = self.by_pair()[("sig_com", "own_incident:def_endpoint")]
        self.assertIn("your organization", account["outreach_safe_text"])

    def test_outreach_safe_no_prices_no_tier_assertions(self):
        generate_snapshots(self.conn)
        rows = self.rows()
        self.assertTrue(rows)
        for r in rows:
            text = r["outreach_safe_text"]
            low = text.lower()
            # R7.11: price_note strings never interpolated (non-primary
            # especially; we exclude all of them)
            self.assertNotIn(FAKE_PRICE, text, r["play_id"])
            self.assertNotIn(PRIMARY_PRICE, text, r["play_id"])
            for pattern in FORBIDDEN_TIER_ASSERTIONS:
                self.assertNotIn(pattern, low, r["play_id"])
            # conditional discovery phrasing present (R7.7/R7.9)
            self.assertIn("If E3", text, r["play_id"])

    def test_customer_facing_zero_suppresses_opener_keeps_snapshot(self):
        """R7.12: an incident not cleared for customer-facing use still
        generates the play/snapshot, but the outreach OPENER is withheld
        (verification-first) - suppress the opener, not the whole play."""
        add_signal(self.conn, "sig_unconf", "E_COM", "account",
                   "own_incident", customer_facing_allowed=0)
        self.conn.commit()
        generate_snapshots(self.conn)
        rows = [r for r in self.rows() if r["signal_id"] == "sig_unconf"]
        self.assertTrue(rows)                       # snapshot still written
        for r in rows:
            self.assertTrue(r["outreach_safe_text"].startswith(
                "Verification-first"), r["play_id"])
            self.assertNotIn("licensing check may be timely",
                             r["outreach_safe_text"], r["play_id"])
            # the operator-facing recommendation is untouched
            self.assertIn("Recommended path", r["display_text"], r["play_id"])

    def test_gov_possible_gets_caution_line_in_display(self):
        self.conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, "
            " gov_cloud_likelihood) VALUES ('E_POSS', 'E_POSS', 'possible')")
        add_signal(self.conn, "sig_poss", "E_POSS", "account", "own_incident")
        self.conn.commit()
        generate_snapshots(self.conn)
        by_pair = self.by_pair()
        # 'possible' does not suppress — play kept, caution shown
        copilot = by_pair[("sig_poss", "own_incident:sec_copilot")]
        self.assertIn("Gov-cloud caution", copilot["display_text"])
        self.assertIn(COPILOT_GCC_NOTE, copilot["display_text"])
        # commercial entity: no caution
        com = by_pair[("sig_com", "own_incident:sec_copilot")]
        self.assertNotIn("Gov-cloud caution", com["display_text"])
        # family without a not-available gcc_high fact: no caution even for
        # a gov-possible entity
        poss_ep = by_pair[("sig_poss", "own_incident:def_endpoint")]
        self.assertNotIn("Gov-cloud caution", poss_ep["display_text"])

    def test_trigger_without_candidates_yields_no_rows(self):
        s = generate_snapshots(self.conn)
        self.assertEqual(s["signals_seen"], 4)
        self.assertEqual(
            [r for r in self.rows() if r["signal_id"] == "sig_empty"], [])


if __name__ == "__main__":
    unittest.main()
