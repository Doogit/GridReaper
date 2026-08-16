"""Security-press incident classifier tests (R9.6, R10.5, R7.12, R4.1, R6.2).

Hermetic: real migrations against in-memory SQLite, FK on, canned security-press
item payloads (several lifted verbatim from a real backfill window) run
end-to-end through the classification framework (run_classifier). Covers the
disclosure grammar's own-vs-off-list branch, the per-source evidence tier
(The Record / BleepingComputer -> corroborated), BleepingComputer's
leak-adjacent down-tier to unconfirmed_early_warning + outreach gating,
name-free peer cards, collision -> review-queue (no card), disclosure precedence
over a leak marker, vendor/generic-news drops, no over-claiming, and force
idempotence.
"""
import json
import sqlite3
import unittest

from app.classify import security_rss
from app.classify.runner import run_classifier
from app.db.migrate import apply_migrations

RECORD = "the_record"
BLEEP = "bleepingcomputer"


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    for src, name in [(RECORD, "The Record RSS"),
                      (BLEEP, "BleepingComputer RSS")]:
        conn.execute(
            "INSERT INTO source_policies (source_id, name, evidence_rank) "
            "VALUES (?, ?, 2)", (src, name))
    for trigger_id, scopes, strength, hl in [
            ("own_incident", ["account"], 5, 270),
            ("peer_incident", ["sector"], 3, 135)]:
        conn.execute(
            "INSERT INTO triggers (trigger_id, name, base_strength, "
            " decay_half_life_days, mvp_flag, evidence_quality, "
            " allowed_scopes) VALUES (?, ?, ?, ?, 0, 'PC', ?)",
            (trigger_id, trigger_id, strength, hl, json.dumps(scopes)))
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector) "
        "VALUES ('E0001', 'NextEra Energy', 'Electric Utility')")
    # 'Dominion' is a bare collision term for a full name that never appears
    # verbatim in the item, so it can only go to review.
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector) "
        "VALUES ('E0002', 'Dominion Energy', 'Electric Utility')")
    conn.execute(
        "INSERT INTO entity_collision_terms (entity_id, term) "
        "VALUES ('E0002', 'Dominion')")
    conn.commit()
    return conn


def add_item(conn, n, source_id, title, event_date="2026-08-12", **payload):
    payload = {"title": title, "description": payload.pop("description", ""),
               "link": f"https://example.test/{source_id}/{n}", **payload}
    raw_event_id = f"{source_id}:{n}"
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
        " payload, url, first_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
        (raw_event_id, source_id, event_date,
         json.dumps(payload, sort_keys=True), payload["link"],
         f"2026-08-12T00:00:0{n}Z"))
    conn.commit()
    return raw_event_id


class SecurityRssTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def run_src(self, source_id, **kw):
        return run_classifier(self.conn, security_rss.CLASSIFIER_ID, source_id,
                              security_rss.SOURCES[source_id],
                              security_rss.PARSER_VERSION, **kw)

    def signals(self):
        return self.conn.execute(
            "SELECT * FROM signals ORDER BY signal_id").fetchall()


class TestOwnDisclosure(SecurityRssTestCase):
    def test_watchlist_disclosure_yields_one_own_corroborated(self):
        rid = add_item(self.conn, 1, RECORD,
                       "NextEra Energy discloses data breach affecting customers")
        s = self.run_src(RECORD)
        self.assertEqual((s["status"], s["signals_new"]), ("success", 1))
        sigs = self.signals()
        self.assertEqual(len(sigs), 1)                 # own only, not own+peer
        own = sigs[0]
        self.assertEqual(own["trigger_id"], "own_incident")
        self.assertEqual(own["signal_id"], f"own_incident:{rid}:E0001")
        self.assertEqual(own["entity_id"], "E0001")
        self.assertEqual(own["signal_scope"], "account")
        self.assertEqual(own["event_date"], "2026-08-12")
        self.assertEqual(own["incident_evidence_level"], "corroborated")
        self.assertEqual(own["customer_facing_allowed"], 1)   # R7.12
        # headline is the item title, quoted verbatim (R4.1)
        self.assertEqual(own["headline"],
                         "NextEra Energy discloses data breach affecting customers")

    def test_match_logs_a_decision(self):
        """R6.4: the resolution the classifier performed is logged."""
        add_item(self.conn, 1, RECORD,
                 "NextEra Energy confirms ransomware attack")
        self.run_src(RECORD)
        decided = self.conn.execute(
            "SELECT entity_id FROM entity_match_decisions").fetchall()
        self.assertEqual([r["entity_id"] for r in decided], ["E0001"])

    def test_bleepingcomputer_disclosure_is_own_corroborated_not_leak(self):
        """A watchlist company's disclosure on BleepingComputer is corroborated
        (outreach allowed), not the leak-adjacent unconfirmed tier."""
        add_item(self.conn, 1, BLEEP,
                 "NextEra Energy confirms cybersecurity incident")
        self.run_src(BLEEP)
        own = self.signals()[0]
        self.assertEqual(own["trigger_id"], "own_incident")
        self.assertEqual(own["incident_evidence_level"], "corroborated")
        self.assertEqual(own["customer_facing_allowed"], 1)


class TestPeerDisclosure(SecurityRssTestCase):
    def test_offlist_disclosure_names_company_but_hedges_sector_claim(self):
        """The live defect this path shipped, pinned both ways.

        Trezor is a crypto hardware-wallet vendor, and it went out under a flat
        "Sector peer disclosed a cybersecurity incident" headline - a claim this
        classifier never verified. Resolver status "none" means only that the
        name is off-watchlist; it is not evidence of a shared industry, and
        unlike ransomware.py this path has no industry gate to lean on.

        So: NAME the company (operator ruling - naming is acceptable wherever
        the card cites its source) and HEDGE the sector claim. Naming actually
        makes a misfile like this one visible to the operator, where "an
        organization" concealed it.
        """
        rid = add_item(self.conn, 1, BLEEP,
                       "Trezor discloses data breach affecting nearly 14,000 customers")
        s = self.run_src(BLEEP)
        self.assertEqual(s["signals_new"], 1)
        peer = self.signals()[0]
        self.assertEqual(peer["trigger_id"], "peer_incident")
        self.assertEqual(peer["signal_id"], f"peer_incident:{rid}:sector")
        self.assertIsNone(peer["entity_id"])
        self.assertEqual(peer["signal_scope"], "sector")
        self.assertEqual(peer["incident_evidence_level"], "corroborated")
        self.assertEqual(peer["customer_facing_allowed"], 1)
        ev = self.conn.execute(
            "SELECT evidence_text FROM signal_evidence WHERE signal_id = ?",
            (peer["signal_id"],)).fetchone()["evidence_text"]
        for text in (peer["headline"], ev):
            self.assertIn("Trezor", text)
        # The unhedged claim must be GONE, not merely accompanied by a hedge:
        # the headline must not open by asserting a sector peer outright.
        self.assertTrue(peer["headline"].startswith("Possible sector peer:"),
                        peer["headline"])
        self.assertFalse(peer["headline"].startswith("Sector peer"),
                         peer["headline"])
        self.assertIn("Industry match is unverified", ev)


class TestLeakAdjacent(SecurityRssTestCase):
    def test_bleepingcomputer_leak_claim_is_unconfirmed_name_free_peer(self):
        rid = add_item(self.conn, 1, BLEEP,
                       "Ransomware gang claims to have breached Acme Regional Supplier")
        s = self.run_src(BLEEP)
        self.assertEqual(s["signals_new"], 1)
        peer = self.signals()[0]
        self.assertEqual(peer["trigger_id"], "peer_incident")
        self.assertEqual(peer["signal_id"], f"peer_incident:{rid}:sector")
        self.assertIsNone(peer["entity_id"])
        self.assertEqual(peer["incident_evidence_level"],
                         "unconfirmed_early_warning")
        self.assertEqual(peer["customer_facing_allowed"], 0)   # R7.12 suppressed
        # name-free: neither victim nor attacker string is printed
        ev = self.conn.execute(
            "SELECT evidence_text FROM signal_evidence WHERE signal_id = ?",
            (peer["signal_id"],)).fetchone()["evidence_text"]
        for text in (peer["headline"], ev):
            self.assertNotIn("Acme", text)
            self.assertNotIn("Ransomware gang", text)

    def test_the_record_has_no_leak_path(self):
        """The Record is non-leak journalism: a leak-claim headline that fails
        the disclosure grammar mints no card at all on this source."""
        add_item(self.conn, 1, RECORD,
                 "Ransomware gang claims to have breached Acme Regional Supplier")
        s = self.run_src(RECORD)
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(self.signals(), [])

    def test_disclosure_precedence_over_leak_marker(self):
        """A victim confirmation beats a co-occurring leak claim: corroborated,
        not down-tiered."""
        add_item(self.conn, 1, BLEEP,
                 "Acme Utility confirms ransomware attack after gang claims responsibility")
        self.run_src(BLEEP)
        peer = self.signals()[0]
        self.assertEqual(peer["trigger_id"], "peer_incident")   # Acme off-list
        self.assertEqual(peer["incident_evidence_level"], "corroborated")


class TestCollisionReview(SecurityRssTestCase):
    def test_bare_collision_subject_goes_to_review_no_card(self):
        """R6.2/R6.3: a bare collision term never auto-fires; it queues for
        review and mints no card (the operator's queue-only decision)."""
        add_item(self.conn, 1, RECORD, "Dominion confirms cybersecurity incident")
        s = self.run_src(RECORD)
        self.assertEqual((s["signals_new"], s["review_enqueued"]), (0, 1))
        self.assertEqual(self.signals(), [])
        q = self.conn.execute(
            "SELECT candidate_entity_id FROM review_queue "
            "WHERE disposition = 'pending'").fetchall()
        self.assertIn("E0002", [r["candidate_entity_id"] for r in q])


class TestNegative(SecurityRssTestCase):
    def test_generic_security_news_dropped(self):
        # Real backfill headline (no victim company, no disclosure).
        add_item(self.conn, 1, RECORD,
                 "New Mirai variant adds stealth capabilities to notorious botnet code")
        s = self.run_src(RECORD)
        self.assertEqual((s["events_processed"], s["signals_new"]), (1, 0))
        self.assertEqual(self.signals(), [])

    def test_vendor_patch_is_not_a_victim(self):
        # Real backfill headline: a vendor patching is not that vendor's breach.
        add_item(self.conn, 1, BLEEP,
                 "Microsoft patches LegacyHive Windows zero-day vulnerability")
        s = self.run_src(BLEEP)
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(self.signals(), [])

    def test_no_title_dropped(self):
        add_item(self.conn, 1, BLEEP, "")
        s = self.run_src(BLEEP)
        self.assertEqual((s["events_processed"], s["signals_new"]), (1, 0))
        self.assertEqual(self.signals(), [])


class TestIdempotence(SecurityRssTestCase):
    def test_second_pass_emits_nothing_new(self):
        add_item(self.conn, 1, BLEEP,
                 "NextEra Energy confirms data breach")
        add_item(self.conn, 2, BLEEP,
                 "Trezor discloses data breach affecting nearly 14,000 customers")
        self.run_src(BLEEP)
        s2 = self.run_src(BLEEP)
        self.assertEqual((s2["events_processed"], s2["signals_new"]), (0, 0))
        self.assertEqual(len(self.signals()), 2)

    def test_force_reprocesses_without_duplicating(self):
        add_item(self.conn, 1, BLEEP, "NextEra Energy confirms data breach")
        self.run_src(BLEEP)
        s2 = self.run_src(BLEEP, force=True)
        self.assertEqual((s2["events_processed"], s2["signals_new"],
                          s2["signals_existing"]), (1, 0, 1))
        self.assertEqual(len(self.signals()), 1)
        # the R6.4 decision log is guarded too - one decision, not two
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM entity_match_decisions").fetchone()["c"]
        self.assertEqual(n, 1)

    def test_force_does_not_duplicate_review_rows(self):
        add_item(self.conn, 1, RECORD, "Dominion confirms cybersecurity incident")
        self.run_src(RECORD)
        self.run_src(RECORD, force=True)
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM review_queue").fetchone()["c"]
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
