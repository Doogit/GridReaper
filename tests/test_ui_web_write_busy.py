"""A busy writer lock must read as an inline warning, never a 500 (R3.2).

``data._commit_with_backoff`` already turns an unwinnable SQLite writer lock
into a ``WriteBusyError`` whose message is written for the operator ("nothing
was saved; try again in a moment") — tests/test_ui_config_writes.py covers that
seam. Nothing caught it at the HTTP surface, so both sanctioned UI writes that
do NOT hold the ingestion lock — POST /feedback (R9.1) and POST /review/triage
(R8.2) — turned a transient lock into an unhandled 500.

These tests pin the surface: 200 + the operator-grade message in the body, an
HX-Reswap that APPENDS rather than replaces (so the reason form and the pending
row's buttons survive and the click can be retried), and — for triage — the
review_queue row still pending afterwards. The busy condition is injected at the
data function rather than by holding a real lock, because the real lock would
cost the connection's 5s busy_timeout per attempt; what is under test here is
the route's handling, not the backoff.

Hermetic: temp-file SQLite via apply_migrations, pointed at the app through
GRIDSIGNALS_DB, no network. UTC ISO-8601 (R10.2).
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from fastapi.testclient import TestClient

from app.db.migrate import apply_migrations
from app.ui import data
from app.ui_web.app import app

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
BUSY = data.WriteBusyError(
    "could not save this thing: the database stayed busy across 4 attempts "
    "over 0.65s. An ingestion or scoring run is probably writing - nothing "
    "was saved; try again in a moment.")


def _seed(conn):
    """One pending review row and its FK chain."""
    conn.execute(
        "INSERT INTO source_policies (source_id, name, enabled, ttl, "
        "access_method, evidence_rank) VALUES ('sp_ok','OK Source',1,3600,"
        "'rss',2)")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector) "
        "VALUES ('E_DARK','Dark Muni Co','muni_public')")
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, payload, "
        "url) VALUES ('re_rev','sp_ok','2026-07-29',"
        "'{\"title\": \"Ambiguous Utility Co filing\"}','http://rev/doc')")
    conn.execute(
        "INSERT INTO review_queue (raw_event_id, candidate_entity_id, reason, "
        "confidence, created_at, disposition) VALUES "
        "('re_rev','E_DARK','fuzzy_below_threshold',0.82,?,'pending')",
        (NOW.isoformat(),))


class WriteBusyTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys=ON")
        apply_migrations(conn)
        _seed(conn)
        conn.commit()
        conn.close()
        os.environ["GRIDSIGNALS_DB"] = self.path
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("GRIDSIGNALS_DB", None)
        try:
            os.remove(self.path)
        except OSError:
            pass

    def assert_inline_warning(self, resp):
        """200, the operator-grade message verbatim, and a swap that appends."""
        self.assertEqual(resp.status_code, 200)
        self.assertIn("nothing was saved; try again in a moment", resp.text)
        self.assertIn("gs-fb-error", resp.text)
        self.assertEqual(resp.headers.get("HX-Reswap"), "beforeend")


class TestFeedbackWriteBusy(WriteBusyTestBase):
    def test_useful_verdict_busy_is_a_warning_not_a_500(self):
        with mock.patch.object(data, "record_feedback", side_effect=BUSY):
            resp = self.client.post(
                "/feedback", data={"signal_id": "S1", "verdict": "useful"})
        self.assert_inline_warning(resp)

    def test_not_useful_busy_keeps_the_reason_form(self):
        # beforeend appends the warning INTO the feedback container, so the
        # reason form the operator filled in (note included) is still there.
        with mock.patch.object(data, "record_feedback", side_effect=BUSY):
            resp = self.client.post("/feedback", data={
                "signal_id": "S1", "verdict": "not_useful",
                "reason_code": "duplicate", "note": "seen this one"})
        self.assert_inline_warning(resp)
        self.assertNotIn("<form", resp.text)      # nothing is replaced
        self.assertNotIn("Feedback recorded", resp.text)

    def test_a_validation_error_still_returns_the_reason_form(self):
        # the R9.1 ValueError path is unchanged: a real reason form comes back
        resp = self.client.post(
            "/feedback", data={"signal_id": "S1", "verdict": "not_useful"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("reason_code is required", resp.text)
        self.assertIn("<form", resp.text)
        self.assertIsNone(resp.headers.get("HX-Reswap"))


class TestTriageWriteBusy(WriteBusyTestBase):
    def test_busy_is_a_warning_not_a_500(self):
        with mock.patch.object(data, "triage_decision", side_effect=BUSY):
            resp = self.client.post("/review/triage", data={
                "raw_event_id": "re_rev", "candidate_entity_id": "E_DARK",
                "accept": "true"})
        self.assert_inline_warning(resp)
        self.assertNotIn("Decision recorded", resp.text)

    def test_busy_does_not_consume_the_pending_row(self):
        with mock.patch.object(data, "triage_decision", side_effect=BUSY):
            self.client.post("/review/triage", data={
                "raw_event_id": "re_rev", "candidate_entity_id": "E_DARK",
                "accept": "true"})
        conn = sqlite3.connect(self.path)
        row = conn.execute("SELECT disposition FROM review_queue "
                           "WHERE raw_event_id='re_rev'").fetchone()
        conn.close()
        self.assertEqual(row[0], "pending")
        # and the queue still offers the retry
        dom = self.client.get("/review").text
        self.assertIn("fuzzy_below_threshold", dom)
        self.assertIn('hx-post="/review/triage"', dom)


if __name__ == "__main__":
    unittest.main()
