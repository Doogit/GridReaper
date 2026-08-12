"""Docket-based supersession tests (R8.1 'superseded' status).

Hermetic: real migrations against in-memory SQLite, FK on, fixture-inserted
triggers / raw_events (with Federal-Register-shaped payloads) / signals. The
two NOPR->final pairs mirror verified ground truth in the real DB
(data/gridsignals.db): Virtualization NOPR 2025-18395 (docket RM24-8-000) is
superseded by Order 919 final 2026-05716 (RM24-8-000); CIP-003 NOPR 2025-18396
(RM25-8-000) by Order 918 final 2026-05711 (RM25-8-000); the Order 912 final
2025-18394 is not a proposal and stays active. All share trigger
nerc_cip_revision. Covers docket-match flip, no-docket-overlap no-op, a
proposal with no later final, idempotency, score frozen on flip, and that
final rules are never flipped.
"""
import json
import sqlite3
import unittest

from app.db.migrate import apply_migrations
from app.supersede import supersede


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        " decay_half_life_days) VALUES ('nerc_cip_revision', 'CIP', 5, 600)")
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        " decay_half_life_days) VALUES ('other', 'Other', 4, 90)")
    conn.commit()
    return conn


def add(conn, signal_id, trigger_id, scope, event_date, doc_type, dockets,
        status="active", score=2.5, entity_id=None):
    """Insert a raw_event (payload carries type + docket_ids) and its signal."""
    rid = f"re:{signal_id}"
    payload = json.dumps({"type": doc_type, "docket_ids": dockets})
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, event_date, payload) "
        "VALUES (?, ?, ?)", (rid, event_date, payload))
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
        " signal_scope, trigger_id, event_date, status, score) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (signal_id, rid, entity_id, scope, trigger_id, event_date, status,
         score))
    conn.commit()


# The two verified NOPR->final pairs plus the standalone Order 912 final.
def seed_real_shape(conn):
    add(conn, "nopr_virt", "nerc_cip_revision", "regulatory_calendar",
        "2025-09-23", "Proposed Rule", ["Docket No. RM24-8-000"])
    add(conn, "nopr_cip003", "nerc_cip_revision", "regulatory_calendar",
        "2025-09-23", "Proposed Rule", ["Docket No. RM25-8-000"])
    add(conn, "order912", "nerc_cip_revision", "sector", "2025-09-23", "Rule",
        ["Docket Nos. RM24-4-000 and RM20-19-000", "Order No. 912"])
    add(conn, "order918", "nerc_cip_revision", "sector", "2026-03-24", "Rule",
        ["Docket No. RM25-8-000"])
    add(conn, "order919", "nerc_cip_revision", "sector", "2026-03-24", "Rule",
        ["Docket No. RM24-8-000"])


class TestSupersede(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def status(self, signal_id):
        return self.conn.execute(
            "SELECT status FROM signals WHERE signal_id = ?",
            (signal_id,)).fetchone()["status"]

    def score(self, signal_id):
        return self.conn.execute(
            "SELECT score FROM signals WHERE signal_id = ?",
            (signal_id,)).fetchone()["score"]

    def test_two_nopr_pairs_flip(self):
        """The verified pairs: both NOPRs -> superseded; Order 912 (a final,
        not a proposal) stays active; the finals stay active."""
        seed_real_shape(self.conn)
        summary = supersede(self.conn)
        self.assertEqual(summary, {"flipped": 2, "proposals_seen": 2})
        self.assertEqual(self.status("nopr_virt"), "superseded")
        self.assertEqual(self.status("nopr_cip003"), "superseded")
        self.assertEqual(self.status("order912"), "active")
        self.assertEqual(self.status("order918"), "active")
        self.assertEqual(self.status("order919"), "active")

    def test_no_docket_overlap_is_noop(self):
        """A later final of the same trigger with a disjoint docket set does
        not supersede the proposal."""
        add(self.conn, "prop", "nerc_cip_revision", "regulatory_calendar",
            "2025-01-01", "Proposed Rule", ["Docket No. RM24-8-000"])
        add(self.conn, "final", "nerc_cip_revision", "sector", "2025-06-01",
            "Rule", ["Docket No. RM99-1-000"])
        summary = supersede(self.conn)
        self.assertEqual(summary, {"flipped": 0, "proposals_seen": 1})
        self.assertEqual(self.status("prop"), "active")

    def test_proposal_with_no_later_final_is_noop(self):
        """Docket matches but the only final predates the proposal -> no flip
        (final must be strictly later)."""
        add(self.conn, "final_old", "nerc_cip_revision", "sector",
            "2025-01-01", "Rule", ["Docket No. RM24-8-000"])
        add(self.conn, "prop", "nerc_cip_revision", "regulatory_calendar",
            "2025-06-01", "Proposed Rule", ["Docket No. RM24-8-000"])
        summary = supersede(self.conn)
        self.assertEqual(summary, {"flipped": 0, "proposals_seen": 1})
        self.assertEqual(self.status("prop"), "active")

    def test_different_trigger_does_not_supersede(self):
        """A final sharing the docket but of a different trigger is ignored."""
        add(self.conn, "prop", "nerc_cip_revision", "regulatory_calendar",
            "2025-01-01", "Proposed Rule", ["Docket No. RM24-8-000"])
        add(self.conn, "final_other", "other", "sector", "2025-06-01", "Rule",
            ["Docket No. RM24-8-000"])
        summary = supersede(self.conn)
        self.assertEqual(summary, {"flipped": 0, "proposals_seen": 1})
        self.assertEqual(self.status("prop"), "active")

    def test_bundled_docket_string_parses_multiple(self):
        """A final whose single docket_ids string bundles several dockets
        supersedes a proposal that shares any one of them."""
        add(self.conn, "prop", "nerc_cip_revision", "regulatory_calendar",
            "2025-01-01", "Proposed Rule", ["Docket No. RM20-19-000"])
        add(self.conn, "final", "nerc_cip_revision", "sector", "2025-06-01",
            "Rule", ["Docket Nos. RM24-4-000 and RM20-19-000"])
        summary = supersede(self.conn)
        self.assertEqual(summary["flipped"], 1)
        self.assertEqual(self.status("prop"), "superseded")

    def test_idempotent_second_run_flips_zero(self):
        seed_real_shape(self.conn)
        first = supersede(self.conn)
        self.assertEqual(first["flipped"], 2)
        second = supersede(self.conn)
        self.assertEqual(second["flipped"], 0)
        self.assertEqual(self.status("nopr_virt"), "superseded")
        self.assertEqual(self.status("nopr_cip003"), "superseded")

    def test_score_frozen_on_flip(self):
        """Supersession is status-only: the score is untouched."""
        add(self.conn, "prop", "nerc_cip_revision", "regulatory_calendar",
            "2025-01-01", "Proposed Rule", ["Docket No. RM24-8-000"],
            score=2.025)
        add(self.conn, "final", "nerc_cip_revision", "sector", "2025-06-01",
            "Rule", ["Docket No. RM24-8-000"])
        supersede(self.conn)
        self.assertEqual(self.status("prop"), "superseded")
        self.assertAlmostEqual(self.score("prop"), 2.025)

    def test_finals_never_flipped(self):
        """Two finals sharing a docket never supersede each other (only
        proposals are candidates)."""
        add(self.conn, "final_a", "nerc_cip_revision", "sector", "2025-01-01",
            "Rule", ["Docket No. RM24-8-000"])
        add(self.conn, "final_b", "nerc_cip_revision", "sector", "2025-06-01",
            "Rule", ["Docket No. RM24-8-000"])
        summary = supersede(self.conn)
        self.assertEqual(summary, {"flipped": 0, "proposals_seen": 0})
        self.assertEqual(self.status("final_a"), "active")
        self.assertEqual(self.status("final_b"), "active")


if __name__ == "__main__":
    unittest.main()
