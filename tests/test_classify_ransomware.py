"""ransomware.live incident classifier tests (R9.6, R10.5, R7.12, R4.1, R6.2).

Hermetic: real migrations against in-memory SQLite, FK on, canned
ransomware.live victim payloads mirroring the stored shape, run end-to-end
through the classification framework (run_classifier). Covers the own-vs-
off-list branch, the unconfirmed_early_warning tier + outreach gating, name-
free peer cards, collision -> review-queue (no card), no over-claiming
wording, no-victim drop, and real second-pass / force idempotence.
"""
import json
import sqlite3
import unittest

from app.classify import ransomware
from app.classify.runner import run_classifier
from app.db.migrate import apply_migrations

SRC = ransomware.SOURCE_ID


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO source_policies (source_id, name, evidence_rank) "
        "VALUES (?, 'ransomware.live API', 3)", (SRC,))
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
    # A collision setup: 'Dominion' is a bare collision term for a full name
    # that never appears verbatim in the record, so it can only go to review.
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector) "
        "VALUES ('E0002', 'Dominion Energy', 'Electric Utility')")
    conn.execute(
        "INSERT INTO entity_collision_terms (entity_id, term) "
        "VALUES ('E0002', 'Dominion')")
    # E0003 carries an alias equal to a ransomware GROUP name: proves the
    # attacker-chosen group string can never corroborate/upgrade a resolution.
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector) "
        "VALUES ('E0003', 'Southern Grid', 'Electric Utility')")
    conn.execute(
        "INSERT INTO entity_aliases (entity_id, alias) VALUES ('E0003', 'BlackCat')")
    conn.execute(
        "INSERT INTO entity_collision_terms (entity_id, term) "
        "VALUES ('E0003', 'Southern')")
    conn.commit()
    return conn


def add_victim(conn, n, victim, group="AcmeLocker", **overrides):
    payload = {
        "victim": victim, "group": group, "discovered": "2026-08-05T09:00:00",
        "attackdate": "2026-08-05T09:00:00", "country": "US",
        "activity": "Energy", "url": f"https://www.ransomware.live/id/{n}"}
    payload.update(overrides)
    raw_event_id = f"{SRC}:{n}"
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
        " payload, url, first_seen_at) VALUES (?, ?, '2026-08-05', ?, ?, ?)",
        (raw_event_id, SRC, json.dumps(payload, sort_keys=True),
         payload["url"], f"2026-08-05T00:00:0{n}Z"))
    conn.commit()
    return raw_event_id


class RansomwareTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def run_it(self, **kw):
        return run_classifier(self.conn, ransomware.CLASSIFIER_ID, SRC,
                              ransomware.classify_ransomware,
                              ransomware.PARSER_VERSION, **kw)

    def signals(self):
        return self.conn.execute(
            "SELECT * FROM signals ORDER BY signal_id").fetchall()


class TestOwnMatch(RansomwareTestCase):
    def test_watchlist_victim_yields_one_own_card(self):
        rid = add_victim(self.conn, 1, "NextEra Energy")
        s = self.run_it()
        self.assertEqual((s["status"], s["signals_new"]), ("success", 1))
        sigs = self.signals()
        self.assertEqual(len(sigs), 1)              # own only, not own+peer
        own = sigs[0]
        self.assertEqual(own["trigger_id"], "own_incident")
        self.assertEqual(own["signal_id"], f"own_incident:{rid}:E0001")
        self.assertEqual(own["entity_id"], "E0001")   # framework-resolved
        self.assertEqual(own["signal_scope"], "account")
        self.assertEqual(own["event_date"], "2026-08-05")
        self.assertEqual(own["incident_evidence_level"],
                         "unconfirmed_early_warning")
        self.assertEqual(own["customer_facing_allowed"], 0)   # R7.12

    def test_match_logs_a_decision(self):
        """R6.4: the resolution the classifier performed is logged."""
        add_victim(self.conn, 1, "NextEra Energy")
        self.run_it()
        decided = self.conn.execute(
            "SELECT entity_id FROM entity_match_decisions").fetchall()
        self.assertEqual([r["entity_id"] for r in decided], ["E0001"])

    def test_headline_does_not_over_claim(self):
        """R4.1: an unverified leak-site listing never reads as a breach."""
        add_victim(self.conn, 1, "NextEra Energy", group="AcmeLocker")
        self.run_it()
        own = self.signals()[0]
        self.assertNotIn("breach", own["headline"].lower())
        self.assertIn("unverified", own["headline"].lower())
        self.assertIn("AcmeLocker", own["headline"])
        ev = self.conn.execute(
            "SELECT evidence_text FROM signal_evidence WHERE signal_id = ?",
            (own["signal_id"],)).fetchone()["evidence_text"]
        self.assertNotIn("breach", ev.lower())
        self.assertIn("NextEra Energy", ev)


class TestPeerOffList(RansomwareTestCase):
    def test_offlist_victim_yields_named_peer(self):
        """The off-list victim IS named on the peer card (operator ruling:
        naming is acceptable wherever the card cites its source), and the
        "Sector peer" claim stays unhedged because this path already gated on
        is_peer_industry - unlike the security-press path, the industry claim
        here is source-supported."""
        rid = add_victim(self.conn, 1, "Obscure Regional Water Co")
        s = self.run_it()
        self.assertEqual(s["signals_new"], 1)
        peer = self.signals()[0]
        self.assertEqual(peer["trigger_id"], "peer_incident")
        self.assertEqual(peer["signal_id"], f"peer_incident:{rid}:sector")
        self.assertIsNone(peer["entity_id"])
        self.assertEqual(peer["signal_scope"], "sector")
        self.assertEqual(peer["incident_evidence_level"],
                         "unconfirmed_early_warning")
        self.assertEqual(peer["customer_facing_allowed"], 0)
        self.assertIn("Obscure Regional Water Co", peer["headline"])
        ev = self.conn.execute(
            "SELECT evidence_text FROM signal_evidence WHERE signal_id = ?",
            (peer["signal_id"],)).fetchone()["evidence_text"]
        self.assertIn("Obscure Regional Water Co", ev)
        self.assertIn("Sector peer", peer["headline"])
        # The R10.5 tier caveat must survive naming - naming a victim does not
        # promote an unverified leak-site claim to a confirmed incident.
        self.assertIn("Unverified", ev)

    def test_group_field_is_never_printed_on_a_peer_card(self):
        """The victim is named, but the attacker-controlled `group` string is
        still withheld: a crew can name itself after its victim, and that
        string is authored by the extortionist rather than by any source we
        can cite. Naming the victim is a sourced claim; repeating the crew's
        self-chosen label is not."""
        add_victim(self.conn, 1, "Obscure Regional Water Co",
                   group="Obscure Regional Water Co breach crew")
        self.run_it()
        peer = self.signals()[0]
        ev = self.conn.execute(
            "SELECT evidence_text FROM signal_evidence WHERE signal_id = ?",
            (peer["signal_id"],)).fetchone()["evidence_text"]
        for text in (peer["headline"], ev):
            self.assertNotIn("breach crew", text)


class TestPeerIndustryGate(RansomwareTestCase):
    """A peer card asserts "sector peer", so the tracker's own industry tag has
    to support that claim (R4.1). Industry only - never geography."""

    def test_offindustry_victim_mints_no_card(self):
        add_victim(self.conn, 1, "Obscure Regional Clinic",
                   activity="Healthcare")
        s = self.run_it()
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(self.signals(), [])

    def test_unknown_industry_mints_no_card(self):
        """"Not Found" is 17 of 100 records upstream. An unknown industry is
        not evidence of a match, so it under-fires rather than over-claims."""
        for n, activity in enumerate(["Not Found", ""], start=1):
            add_victim(self.conn, n, f"Obscure Co {n}", activity=activity)
        s = self.run_it()
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(self.signals(), [])

    def test_missing_activity_key_mints_no_card(self):
        add_victim(self.conn, 1, "Obscure Co", activity=None)
        self.assertEqual(self.run_it()["signals_new"], 0)

    def test_energy_variants_all_fire(self):
        """Upstream punctuation/case drift must not silently drop a real peer."""
        for n, activity in enumerate(
                ["Energy & Utilities", "energy and utilities", "Utilities",
                 "Oil and Gas", "  ENERGY  "], start=1):
            add_victim(self.conn, n, f"Obscure Energy Co {n}",
                       activity=activity)
        self.assertEqual(self.run_it()["signals_new"], 5)

    def test_foreign_energy_victim_still_a_peer(self):
        """Operator ruling: peers are same-industry regardless of region."""
        add_victim(self.conn, 1, "Obscure Osaka Power", country="JP",
                   activity="Energy & Utilities")
        self.assertEqual(self.run_it()["signals_new"], 1)
        self.assertEqual(self.signals()[0]["trigger_id"], "peer_incident")

    def test_watchlist_victim_fires_despite_offindustry_tag(self):
        """The own path is attribution, not industry inference: a watchlist
        company mis-tagged upstream must still mint its account card."""
        add_victim(self.conn, 1, "NextEra Energy", activity="Manufacturing")
        self.assertEqual(self.run_it()["signals_new"], 1)
        own = self.signals()[0]
        self.assertEqual(own["trigger_id"], "own_incident")
        self.assertEqual(own["entity_id"], "E0001")

    def test_offindustry_ambiguous_name_still_reaches_review(self):
        """The gate must not swallow a resolution question. An ambiguous name
        might BE a mis-tagged watchlist company, so a human still sees it."""
        add_victim(self.conn, 1, "Dominion", activity="Healthcare")
        self.run_it()
        self.assertEqual(self.signals(), [])
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM review_queue WHERE disposition = 'pending'"
        ).fetchone()[0]
        self.assertEqual(pending, 1)


class TestCollisionReview(RansomwareTestCase):
    def test_ambiguous_victim_goes_to_review_no_card(self):
        """R6.2/R6.3: a bare collision term never auto-fires; it queues for
        review and mints no card at all (the operator's queue-only
        decision)."""
        add_victim(self.conn, 1, "Dominion")
        s = self.run_it()
        self.assertEqual((s["signals_new"], s["review_enqueued"]), (0, 1))
        self.assertEqual(self.signals(), [])
        q = self.conn.execute(
            "SELECT candidate_entity_id FROM review_queue "
            "WHERE disposition = 'pending'").fetchall()
        self.assertIn("E0002", [r["candidate_entity_id"] for r in q])

    def test_injected_group_name_cannot_upgrade_a_collision(self):
        """The classifier is authoritative: it resolves the victim NAME-ONLY,
        so an attacker-chosen group string that happens to match an entity's
        alias ('BlackCat' for E0003) can never corroborate the ambiguous
        'Southern' into a fired, mis-attributed own card. Queue only."""
        add_victim(self.conn, 1, "Southern", group="BlackCat")
        s = self.run_it()
        self.assertEqual(s["signals_new"], 0)
        self.assertEqual(self.signals(), [])
        pending = self.conn.execute(
            "SELECT 1 FROM review_queue WHERE disposition = 'pending'").fetchall()
        self.assertTrue(pending)

    def test_force_does_not_duplicate_review_rows(self):
        add_victim(self.conn, 1, "Dominion")
        self.run_it()
        self.run_it(force=True)
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM review_queue").fetchone()["c"]
        self.assertEqual(n, 1)          # --force stays idempotent (finding 3)


class TestNegative(RansomwareTestCase):
    def test_no_victim_name_dropped(self):
        add_victim(self.conn, 1, "")
        s = self.run_it()
        self.assertEqual((s["events_processed"], s["signals_new"]), (1, 0))
        self.assertEqual(self.signals(), [])


class TestIdempotence(RansomwareTestCase):
    def test_second_pass_emits_nothing_new(self):
        add_victim(self.conn, 1, "NextEra Energy")
        add_victim(self.conn, 2, "Obscure Regional Water Co")
        self.run_it()
        s2 = self.run_it()
        self.assertEqual((s2["events_processed"], s2["signals_new"]), (0, 0))
        self.assertEqual(len(self.signals()), 2)

    def test_force_reprocesses_without_duplicating(self):
        add_victim(self.conn, 1, "NextEra Energy")
        self.run_it()
        s2 = self.run_it(force=True)
        self.assertEqual((s2["events_processed"], s2["signals_new"],
                          s2["signals_existing"]), (1, 0, 1))
        self.assertEqual(len(self.signals()), 1)
        # the R6.4 decision log is guarded too - one decision, not two
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM entity_match_decisions").fetchone()["c"]
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
