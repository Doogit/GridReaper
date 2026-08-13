"""Tests for app.first_load — the runtime first-load ingest gate (deploy path).

Hermetic: in-memory SQLite via apply_migrations, FK on, no network. Mirrors the
entrypoint contract — an empty signals table means feed_is_empty is True (the
entrypoint runs the background ingest), a single signal means False (skip).
"""
import sqlite3
import unittest

from app.db.migrate import apply_migrations
from app.first_load import feed_is_empty


class FirstLoadTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        apply_migrations(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_empty_store_is_empty(self):
        self.assertTrue(feed_is_empty(self.conn))

    def test_store_with_signal_is_not_empty(self):
        self.conn.execute(
            "INSERT INTO triggers (trigger_id, name, base_strength, "
            "decay_half_life_days) VALUES ('t_x','X',4,90)")
        self.conn.execute(
            "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
            "signal_scope, trigger_id, event_date, headline, evidence_snippet, "
            "source_url, confidence, evidence_quality, incident_evidence_level, "
            "customer_facing_allowed, score, status) "
            "VALUES ('S1',NULL,NULL,'sector','t_x','2026-08-01','h','h',"
            "'http://s',0.9,'IR',NULL,1,2.0,'active')")
        self.assertFalse(feed_is_empty(self.conn))


if __name__ == "__main__":
    unittest.main()
