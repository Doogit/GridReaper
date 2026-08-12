"""Classification framework tests (R3.7, R4.1, R6.2, R6.4, R6.5, R7.2).

Hermetic: real migrations against in-memory SQLite, FK on, fake classifiers
returning canned candidates. Covers bookkeeping, idempotent re-runs, version
bump reprocessing, the review-queue path (a low-confidence candidate never
becomes a signal), parent rollup, evidence rows, scope gating, and per-event
error containment.
"""
import json
import sqlite3
import unittest

from app.classify.runner import run_classifier, top_level_entity
from app.db.load_seeds import TRIGGER_SCOPES, apply_trigger_scopes
from app.db.migrate import apply_migrations

SOURCE = "test_source"


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO source_policies (source_id, name, evidence_rank) "
        "VALUES (?, 'Test source', 2)", (SOURCE,))
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        " decay_half_life_days, mvp_flag, evidence_quality, allowed_scopes) "
        "VALUES ('t_lead', 'Test leadership', 4, 90, 1, 'PC', ?)",
        (json.dumps(["account"]),))
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        " decay_half_life_days, mvp_flag, evidence_quality, allowed_scopes) "
        "VALUES ('t_reg', 'Test regulatory', 5, 600, 1, 'IR', ?)",
        (json.dumps(["sector", "regulatory_calendar"]),))
    entities = [("EP1", "Parent Energy"), ("EC1", "Child Power"),
                ("EA1", "Acme Utilities"), ("EZ1", "Zeta Power"),
                ("EZ2", "Zeta Gas")]
    for eid, name in entities:
        conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name) VALUES (?, ?)",
            (eid, name))
    conn.execute(
        "INSERT INTO entity_relationships (parent_entity_id, "
        " child_entity_id, relationship_type) VALUES ('EP1', 'EC1', "
        " 'direct_parent')")
    # "Zeta" alone is ambiguous between EZ1/EZ2 -> review path
    for eid in ("EZ1", "EZ2"):
        conn.execute(
            "INSERT INTO entity_aliases (entity_id, alias) VALUES (?, 'Zeta')",
            (eid,))
    for i in (1, 2):
        conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
            " url, first_seen_at) VALUES (?, ?, '2026-08-01', "
            " 'https://example.test/e', ?)",
            (f"{SOURCE}:{i}", SOURCE, f"2026-08-0{i}T00:00:00Z"))
    conn.commit()
    return conn


def make_classifier(candidates_by_event):
    """Fake classifier: raw_event_id -> list of candidate dicts."""
    def classify(conn, raw):
        return candidates_by_event.get(raw["raw_event_id"], [])
    return classify


def account_candidate(**overrides):
    cand = {
        "trigger_id": "t_lead", "signal_scope": "account",
        "entity_id": "EA1", "entity_name_hint": None,
        "event_date": "2026-08-01",
        "headline": "Acme Utilities names new CISO",
        "evidence": [{"text": "Acme Utilities appointed a CISO",
                      "locator": "title"}],
        "confidence": 0.9,
    }
    cand.update(overrides)
    return cand


class TestRunClassifier(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def run_one(self, candidates_by_event, version="clf/1.0", **kwargs):
        return run_classifier(
            self.conn, "clf_test", SOURCE,
            make_classifier(candidates_by_event), version, **kwargs)

    def test_signal_evidence_and_bookkeeping(self):
        s = self.run_one({f"{SOURCE}:1": [account_candidate()]})
        self.assertEqual((s["status"], s["events_processed"],
                          s["signals_new"]), ("success", 2, 1))
        sig = self.conn.execute("SELECT * FROM signals").fetchone()
        self.assertEqual(sig["signal_id"], f"t_lead:{SOURCE}:1:EA1")
        self.assertEqual(sig["entity_id"], "EA1")
        self.assertEqual(sig["signal_scope"], "account")
        self.assertEqual(sig["evidence_quality"], "PC")
        self.assertEqual(sig["customer_facing_allowed"], 1)
        self.assertIsNone(sig["incident_evidence_level"])
        self.assertIsNone(sig["score"])
        self.assertEqual(sig["status"], "active")
        ev = self.conn.execute("SELECT * FROM signal_evidence").fetchone()
        self.assertEqual(ev["signal_id"], sig["signal_id"])
        self.assertEqual(ev["evidence_rank"], 2)   # from source_policies
        self.assertEqual(ev["extraction_version"], "clf/1.0")
        book = self.conn.execute(
            "SELECT * FROM classified_events ORDER BY raw_event_id").fetchall()
        self.assertEqual([(b["raw_event_id"], b["signals_emitted"])
                          for b in book],
                         [(f"{SOURCE}:1", 1), (f"{SOURCE}:2", 0)])

    def test_rerun_same_version_is_incremental(self):
        events = {f"{SOURCE}:1": [account_candidate()]}
        self.run_one(events)
        s2 = self.run_one(events)
        self.assertEqual((s2["events_processed"], s2["signals_new"]), (0, 0))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM signals").fetchone()[0], 1)

    def test_version_bump_reprocesses_but_emits_zero_new(self):
        events = {f"{SOURCE}:1": [account_candidate()]}
        self.run_one(events, version="clf/1.0")
        s2 = self.run_one(events, version="clf/2.0")
        self.assertEqual(s2["events_processed"], 2)     # R3.7: history redone
        self.assertEqual(s2["signals_new"], 0)          # deterministic ids
        self.assertEqual(s2["signals_existing"], 1)
        versions = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT parser_version FROM classified_events")}
        self.assertEqual(versions, {"clf/1.0", "clf/2.0"})

    def test_force_reprocesses_same_version(self):
        events = {f"{SOURCE}:1": [account_candidate()]}
        self.run_one(events)
        s2 = self.run_one(events, force=True)
        self.assertEqual((s2["events_processed"], s2["signals_new"],
                          s2["signals_existing"]), (2, 0, 1))

    def test_review_path_never_fires(self):
        """R6.2: ambiguous name -> review_queue, no signal, event bookkept."""
        s = self.run_one({f"{SOURCE}:1": [account_candidate(
            entity_id=None, entity_name_hint="Zeta")]})
        self.assertEqual((s["signals_new"], s["review_enqueued"]), (0, 1))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM signals").fetchone()[0], 0)
        rq = self.conn.execute(
            "SELECT candidate_entity_id FROM review_queue "
            "ORDER BY candidate_entity_id").fetchall()
        self.assertEqual([r[0] for r in rq], ["EZ1", "EZ2"])

    def test_resolved_name_records_decision(self):
        """R6.4: an auto-match through the framework logs the decision."""
        s = self.run_one({f"{SOURCE}:1": [account_candidate(
            entity_id=None, entity_name_hint="Acme Utilities Inc.",
            confidence=0.8)]})
        self.assertEqual(s["signals_new"], 1)
        dec = self.conn.execute(
            "SELECT * FROM entity_match_decisions").fetchone()
        self.assertEqual(dec["entity_id"], "EA1")
        self.assertEqual(dec["decided_by"], "classification")
        sig = self.conn.execute("SELECT * FROM signals").fetchone()
        self.assertEqual(sig["entity_id"], "EA1")
        self.assertLessEqual(sig["confidence"], 0.8)

    def test_parent_rollup_one_card_per_top_level_account(self):
        """R6.5: child and parent candidates from one event -> one signal
        on the top-level parent."""
        s = self.run_one({f"{SOURCE}:1": [
            account_candidate(entity_id="EC1"),
            account_candidate(entity_id="EP1")]})
        self.assertEqual((s["signals_new"], s["signals_existing"]), (1, 1))
        sig = self.conn.execute("SELECT * FROM signals").fetchone()
        self.assertEqual(sig["entity_id"], "EP1")
        self.assertEqual(sig["signal_id"], f"t_lead:{SOURCE}:1:EP1")

    def test_pre_attributed_disabled_entity_dropped(self):
        """R8.7: explicit entity_id candidates bypass EntityResolver, so the
        classifier boundary must still honor soft-disable."""
        self.conn.execute(
            "UPDATE watchlist_entities SET active = 0 WHERE entity_id = 'EA1'")
        self.conn.commit()
        s = self.run_one({f"{SOURCE}:1": [account_candidate(entity_id="EA1")]})
        self.assertEqual((s["signals_new"], s["dropped_no_entity"]), (0, 1))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM signals").fetchone()[0], 0)
        book = self.conn.execute(
            "SELECT signals_emitted FROM classified_events "
            "WHERE raw_event_id = ?", (f"{SOURCE}:1",)).fetchone()
        self.assertEqual(book["signals_emitted"], 0)

    def test_parent_rollup_skips_disabled_parent(self):
        """R8.7: an active child must not mint a new signal for a disabled
        parent through the R6.5 rollup path."""
        self.conn.execute(
            "UPDATE watchlist_entities SET active = 0 WHERE entity_id = 'EP1'")
        self.conn.commit()
        s = self.run_one({f"{SOURCE}:1": [account_candidate(entity_id="EC1")]})
        self.assertEqual(s["signals_new"], 1)
        sig = self.conn.execute("SELECT * FROM signals").fetchone()
        self.assertEqual(sig["entity_id"], "EC1")
        self.assertEqual(sig["signal_id"], f"t_lead:{SOURCE}:1:EC1")

    def test_sector_scope_has_null_entity(self):
        s = self.run_one({f"{SOURCE}:1": [{
            "trigger_id": "t_reg", "signal_scope": "sector",
            "entity_id": None, "entity_name_hint": None,
            "event_date": "2026-08-01",
            "headline": "FERC directs CIP revisions",
            "evidence": [{"text": "Order on CIP standards",
                          "locator": "abstract"}],
            "confidence": 0.9}]})
        self.assertEqual(s["signals_new"], 1)
        sig = self.conn.execute("SELECT * FROM signals").fetchone()
        self.assertIsNone(sig["entity_id"])
        self.assertEqual(sig["signal_id"], f"t_reg:{SOURCE}:1:sector")

    def test_disallowed_scope_dropped(self):
        """R7.2: t_lead only allows account scope."""
        s = self.run_one({f"{SOURCE}:1": [account_candidate(
            signal_scope="sector")]})
        self.assertEqual((s["signals_new"], s["dropped_scope"]), (0, 1))

    def test_no_evidence_dropped(self):
        """R4.1: nothing surfaces unsourced."""
        s = self.run_one({f"{SOURCE}:1": [account_candidate(evidence=[])]})
        self.assertEqual((s["signals_new"], s["dropped_no_evidence"]), (0, 1))

    def test_classifier_error_contained_and_retried(self):
        """A classifier exception rolls back that event only; the event
        stays unprocessed and is retried on the next run."""
        def flaky(conn, raw):
            if raw["raw_event_id"] == f"{SOURCE}:1":
                raise ValueError("boom")
            return [account_candidate()]
        s = run_classifier(self.conn, "clf_test", SOURCE, flaky, "clf/1.0")
        self.assertEqual((s["status"], s["events_errored"],
                          s["events_processed"], s["signals_new"]),
                         ("error", 1, 1, 1))
        self.assertIn("boom", s["last_error"])
        s2 = self.run_one({f"{SOURCE}:1": [account_candidate()]})
        self.assertEqual((s2["status"], s2["events_processed"]),
                         ("success", 1))    # only the errored event retried
        self.assertEqual(s2["signals_new"], 1)   # event 1's own signal id
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM signals").fetchone()[0], 2)

    def test_unknown_source_raises(self):
        with self.assertRaises(ValueError):
            run_classifier(self.conn, "clf_test", "nope",
                           make_classifier({}), "clf/1.0")


class TestTopLevelEntity(unittest.TestCase):
    def test_rollup_paths(self):
        conn = fixture_conn()
        self.assertEqual(top_level_entity(conn, "EC1"), "EP1")
        self.assertEqual(top_level_entity(conn, "EP1"), "EP1")
        self.assertEqual(top_level_entity(conn, "EA1"), "EA1")
        conn.close()

    def test_rollup_stops_before_disabled_parent(self):
        conn = fixture_conn()
        conn.execute(
            "UPDATE watchlist_entities SET active = 0 WHERE entity_id = 'EP1'")
        conn.commit()
        self.assertEqual(top_level_entity(conn, "EC1"), "EC1")
        conn.close()


class TestTriggerScopes(unittest.TestCase):
    def test_loader_sets_mvp_scopes(self):
        """apply_trigger_scopes covers exactly the 4 MVP triggers (R7.2)."""
        self.assertEqual(
            set(TRIGGER_SCOPES),
            {"leadership_change", "nerc_enforcement", "nerc_cip_revision",
             "tsa_security_directive"})
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_migrations(conn)
        for trigger_id in TRIGGER_SCOPES:
            conn.execute(
                "INSERT INTO triggers (trigger_id, name, base_strength, "
                " decay_half_life_days) VALUES (?, ?, 4, 90)",
                (trigger_id, trigger_id))
        self.assertEqual(apply_trigger_scopes(conn), 4)
        for trigger_id, scopes in TRIGGER_SCOPES.items():
            row = conn.execute(
                "SELECT allowed_scopes FROM triggers WHERE trigger_id = ?",
                (trigger_id,)).fetchone()
            self.assertEqual(json.loads(row["allowed_scopes"]), scopes)
        conn.close()


if __name__ == "__main__":
    unittest.main()
