"""Review Queue / Triage tests for the FastAPI web UI (R8.2, R10.3/G2, R10.7).

The TestClient port of tests/test_ui_review.py: same hermetic fixtures (temp-file
SQLite via apply_migrations, pointed at the app through GRIDSIGNALS_DB, no
network), same trust assertions carried across the framework change. A pending
match renders with candidate name + resolver reason + snippet + source link;
Accept writes entity_match_decisions(decided_by='human', decision='reviewed') +
review_queue disposition='accepted' and swaps the row out; Reject writes
'rejected'/'rejected'; source health classifies all five states with the errored
source's error text verbatim; stale facts list shows the fixture stale fact +
header count while the fresh fact is excluded; an empty review queue renders the
honest positive empty state; hostile upstream HTML is escaped, never markup.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db.migrate import apply_migrations
from app.ui_web import render
from app.ui_web.app import app

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def days_ago_date(n):
    return (NOW.date() - timedelta(days=n)).isoformat()


def _base(conn):
    """Sources, entities, raw events — the FK chain shared by every fixture."""
    for sid, name, enabled, ttl in [
            ("sp_ok", "OK Source", 1, 3600),
            ("sp_err", "Err Source", 1, 3600),
            ("sp_never", "Never Source", 1, 3600),
            ("sp_stale", "Stale Source", 1, 3600),
            ("sp_disabled", "Disabled Source", 0, 3600)]:
        conn.execute(
            "INSERT INTO source_policies (source_id, name, enabled, ttl, "
            "access_method, evidence_rank) VALUES (?,?,?,?, 'rss', 2)",
            (sid, name, enabled, ttl))
    runs = [
        ("r_ok", "sp_ok", iso(NOW - timedelta(minutes=10)), "success", ""),
        ("r_err", "sp_err", iso(NOW - timedelta(minutes=5)), "error",
         "HTTP 503 from upstream"),
        ("r_stale", "sp_stale", iso(NOW - timedelta(days=2)), "success", ""),
    ]
    for rid, sid, started, status, err in runs:
        conn.execute(
            "INSERT INTO source_runs (run_id, source_id, started_at, "
            "finished_at, status, error_state) VALUES (?,?,?,?,?,?)",
            (rid, sid, started, started, status, err))

    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector) "
        "VALUES ('E_DARK','Dark Muni Co','muni_public')")
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, payload, "
        "url) VALUES ('re_rev','sp_ok', ?, "
        "'{\"title\": \"Ambiguous Utility Co filing\"}', 'http://rev/doc')",
        (days_ago_date(3),))


def _facts(conn):
    conn.execute("INSERT INTO products (product_id, name) VALUES "
                 "('p_sentinel','Microsoft Sentinel')")
    # one stale (400d), one fresh (30d)
    conn.execute(
        "INSERT INTO license_facts (fact_id, product_id, segment, "
        "source_quality, source_url, verified_date) VALUES "
        "('f_stale','p_sentinel','commercial','non-primary','http://blog', ?)",
        (days_ago_date(400),))
    conn.execute(
        "INSERT INTO license_facts (fact_id, product_id, segment, "
        "source_quality, source_url, verified_date) VALUES "
        "('f_fresh','p_sentinel','commercial','primary','http://primary', ?)",
        (days_ago_date(30),))


def seed_full(conn):
    """A pending review candidate + all five source states + stale/fresh facts."""
    _base(conn)
    _facts(conn)
    conn.execute(
        "INSERT INTO review_queue (raw_event_id, candidate_entity_id, reason, "
        "confidence, created_at, disposition) VALUES "
        "('re_rev','E_DARK','fuzzy_below_threshold', 0.82, ?, 'pending')",
        (iso(NOW),))


def seed_empty(conn):
    """Sources + facts but no pending review rows — the positive empty state."""
    _base(conn)
    _facts(conn)


def seed_hostile_html(conn):
    """HTML-looking upstream/DB text should render as text, never markup."""
    _base(conn)
    _facts(conn)
    conn.execute(
        "UPDATE watchlist_entities SET name=?, subsector=? "
        "WHERE entity_id='E_DARK'",
        ("<img src=x onerror=alert(1)>", "<b>muni</b>"))
    conn.execute(
        "UPDATE raw_events SET payload=? WHERE raw_event_id='re_rev'",
        ('{"title": "<script>alert(2)</script>"}',))
    conn.execute(
        "UPDATE source_policies SET name=? WHERE source_id='sp_err'",
        ("<b>Err Source</b>",))
    conn.execute(
        "UPDATE source_runs SET error_state=? WHERE run_id='r_err'",
        ("<img src=x onerror=alert(3)>",))
    conn.execute(
        "UPDATE products SET name=? WHERE product_id='p_sentinel'",
        ("<svg onload=alert(4)>",))
    conn.execute(
        "UPDATE license_facts SET segment=?, source_quality=? "
        "WHERE fact_id='f_stale'",
        ("<script>segment</script>", "<i>non-primary</i>"))
    conn.execute(
        "INSERT INTO review_queue (raw_event_id, candidate_entity_id, reason, "
        "confidence, created_at, disposition) VALUES "
        "('re_rev','E_DARK','<b>resolver</b>', 0.82, ?, 'pending')",
        (iso(NOW),))


# A real ransomware.live per-victim permalink shape: the /id/ path segment is
# base64 of "Baxter International, Inc.@shinyhunters" (R4.1).
VICTIM_PERMALINK = ("https://www.ransomware.live/id/"
                    "QmF4dGVyIEludGVybmF0aW9uYWwsIEluYy5Ac2hpbnlodW50ZXJz")
# The victim name the permalink encodes, in the payload title the tracker
# actually ships. Pairing them is the point: a fixture whose title did NOT
# contain the encoded name would let the URL assertions below look like a
# closed identity leak when only one channel is closed.
VICTIM_NAME = "Baxter International, Inc."
VICTIM_TITLE = f"{VICTIM_NAME} listed by shinyhunters"
LONG_DESCRIPTION = "Leak-site listing body text. " * 30


def seed_victim_permalink(conn):
    """A pending row shaped like the real ransomware_live rows: a per-victim
    leak-site permalink, and a multi-field payload whose shown title is a small
    slice of the record (R4.1, R10.5)."""
    seed_full(conn)
    payload = json.dumps({"title": VICTIM_TITLE, "activity": "Healthcare",
                          "group": "shinyhunters",
                          "description": LONG_DESCRIPTION})
    conn.execute("UPDATE raw_events SET url=?, payload=? "
                 "WHERE raw_event_id='re_rev'", (VICTIM_PERMALINK, payload))


def seed_long_title(conn):
    """A pending row whose single payload field overruns the snippet limit."""
    seed_full(conn)
    conn.execute("UPDATE raw_events SET payload=? WHERE raw_event_id='re_rev'",
                 (json.dumps({"title": "Ambiguous Utility Co filing "
                              + ("x" * 300)}),))


def seed_no_candidate(conn):
    """A pending row with candidate_entity_id NULL — the shape a non-resolver
    triage route (e.g. the R10.6 provenance quarantine) inserts."""
    _base(conn)
    _facts(conn)
    conn.execute(
        "INSERT INTO review_queue (raw_event_id, candidate_entity_id, reason, "
        "confidence, created_at, disposition) VALUES "
        "('re_rev', NULL, 'provenance_violation:headline', NULL, ?, 'pending')",
        (iso(NOW),))


def make_db(seed):
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


class ReviewTestBase(unittest.TestCase):
    seed = staticmethod(seed_full)

    def setUp(self):
        self.path = make_db(self.seed)
        os.environ["GRIDSIGNALS_DB"] = self.path
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("GRIDSIGNALS_DB", None)
        try:
            os.remove(self.path)
        except OSError:
            pass

    def page(self):
        resp = self.client.get("/review")
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def _open_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


class TestPendingMatches(ReviewTestBase):
    def test_pending_renders_candidate_reason_snippet_source(self):
        dom = self.page()
        self.assertIn("Dark Muni Co", dom)                 # candidate name
        self.assertIn("fuzzy_below_threshold", dom)        # resolver reason
        self.assertIn("Ambiguous Utility Co filing", dom)  # evidence snippet
        self.assertIn("http://rev/doc", dom)               # source link

    def test_accept_writes_both_tables_and_removes_row(self):
        resp = self.client.post("/review/triage", data={
            "raw_event_id": "re_rev", "candidate_entity_id": "E_DARK",
            "accept": "true"})
        self.assertEqual(resp.status_code, 200)
        # the swap-in confirmation replaces the row; the pending item is gone
        self.assertIn("Decision recorded", resp.text)
        self.assertNotIn("fuzzy_below_threshold", resp.text)

        conn = self._open_conn()
        dec = conn.execute(
            "SELECT decision, decided_by FROM entity_match_decisions "
            "WHERE raw_event_id='re_rev'").fetchone()
        self.assertEqual(dec["decision"], "reviewed")
        self.assertEqual(dec["decided_by"], "human")
        rq = conn.execute(
            "SELECT disposition, disposed_at FROM review_queue "
            "WHERE raw_event_id='re_rev'").fetchone()
        self.assertEqual(rq["disposition"], "accepted")
        self.assertIsNotNone(rq["disposed_at"])
        conn.close()

        # a fresh page load no longer surfaces the pending item -> empty state
        dom = self.page()
        self.assertIn("0 pending", dom)
        self.assertNotIn("fuzzy_below_threshold", dom)

    def test_reject_writes_rejected_disposition(self):
        resp = self.client.post("/review/triage", data={
            "raw_event_id": "re_rev", "candidate_entity_id": "E_DARK",
            "accept": "false"})
        self.assertEqual(resp.status_code, 200)

        conn = self._open_conn()
        dec = conn.execute(
            "SELECT decision FROM entity_match_decisions "
            "WHERE raw_event_id='re_rev'").fetchone()
        self.assertEqual(dec["decision"], "rejected")
        rq = conn.execute(
            "SELECT disposition FROM review_queue "
            "WHERE raw_event_id='re_rev'").fetchone()
        self.assertEqual(rq["disposition"], "rejected")
        conn.close()

    def test_pending_row_carries_the_unconfirmed_badge(self):
        # a queue item is by definition unadjudicated (R10.5)
        dom = self.page()
        self.assertIn("unconfirmed candidate", dom)
        self.assertIn("gs-badge unconfirmed", dom)
        # the tooltip states it once; the section copy does not repeat it
        self.assertIn("no human has adjudicated it yet", dom)
        self.assertEqual(dom.count("unconfirmed candidate"), 1)  # badge text
        self.assertEqual(dom.count("Unconfirmed candidate"), 1)  # its tooltip

    def test_short_snippet_is_not_labelled_truncated(self):
        dom = self.page()
        self.assertIn("Ambiguous Utility Co filing", dom)
        self.assertNotIn("Extract truncated", dom)

    def test_triage_row_dom_id_is_hashed_not_raw(self):
        # the raw ids never appear as a DOM id / selector fragment
        dom = self.page()
        expected = render.review_row_dom_id("re_rev", "E_DARK")
        self.assertIn(f'id="{expected}"', dom)
        self.assertNotIn('id="re_rev"', dom)


class TestSourceHealth(ReviewTestBase):
    def test_five_states_classified_and_error_verbatim(self):
        dom = self.page()
        for label in ("OK", "ERROR", "NEVER RUN", "STALE", "DISABLED"):
            self.assertIn(label, dom)
        for name in ("OK Source", "Err Source", "Never Source",
                     "Stale Source", "Disabled Source"):
            self.assertIn(name, dom)
        # error text shown verbatim for the errored source
        self.assertIn("HTTP 503 from upstream", dom)


class TestStaleFacts(ReviewTestBase):
    def test_stale_listed_with_count_fresh_excluded(self):
        dom = self.page()
        self.assertIn("Stale license facts (1)", dom)  # header count
        self.assertIn("Microsoft Sentinel", dom)       # the stale fact
        # the stale fact's verified_date (400d before NOW) is surfaced; the page
        # uses the real clock for age, so assert the date not the exact day count
        self.assertIn(days_ago_date(400), dom)
        # fresh fact (30d) is not surfaced as stale — its verified date is absent
        self.assertNotIn(days_ago_date(30), dom)


class TestEmptyState(ReviewTestBase):
    seed = staticmethod(seed_empty)

    def test_empty_queue_positive_state(self):
        dom = self.page()
        self.assertIn("0 pending", dom)
        self.assertIn("needed no human help", dom)
        # page still renders the other sections without error
        self.assertIn("Source health", dom)


class TestPendingRowTrust(ReviewTestBase):
    """R4.1 / R10.5: the named candidate must not be shown beside a live
    per-victim permalink, an undisclosed record extract, or no tier at all."""
    seed = staticmethod(seed_victim_permalink)

    def test_victim_permalink_is_replaced_by_the_tracker_index(self):
        dom = self.page()
        self.assertNotIn(VICTIM_PERMALINK, dom)
        self.assertNotIn("QmF4dGVyIEludGVybmF0aW9uYWws", dom)
        self.assertIn('href="https://www.ransomware.live"', dom)

    def test_the_substitution_is_disclosed_not_silent(self):
        # a swapped link must not read as a working citation for the claim
        # beside it (R10.4)
        self.assertIn("per-victim permalink withheld", self.page())

    def test_field_selection_is_disclosed_even_when_nothing_is_cut(self):
        # the title is 48 chars — under the limit — but three more fields of
        # the record are not shown, and the reader is told so
        dom = self.page()
        self.assertIn("Extract: 1 of 4 fields in the raw record", dom)
        self.assertIn("not the whole record", dom)
        self.assertNotIn("Leak-site listing body text.", dom)

    def test_url_channel_only_documented_gap(self):
        """The permalink is withheld; the payload TITLE still names the victim.

        Asserted deliberately so the guarantee is not read as broader than it
        is: this row closes the URL channel, not the snippet channel. If the
        snippet channel is closed later, this test fails and whoever closes it
        must restate the claim rather than inherit a stale one.
        """
        self.assertIn(VICTIM_NAME, self.page())

    def test_unconfirmed_badge_present(self):
        self.assertIn("unconfirmed candidate", self.page())


class TestLongTitleTruncation(ReviewTestBase):
    seed = staticmethod(seed_long_title)

    def test_truncated_snippet_is_labelled(self):
        dom = self.page()
        self.assertIn("Extract truncated at 200 characters", dom)
        self.assertIn("not the whole record", dom)
        # the overrun tail never reaches the DOM
        self.assertNotIn("x" * 300, dom)


class TestNoCandidateRow(ReviewTestBase):
    """review_queue.candidate_entity_id is nullable; a NULL asks a different
    question than a resolver near-match and must not render as a blank name."""
    seed = staticmethod(seed_no_candidate)

    def test_null_candidate_is_labelled_not_blank(self):
        dom = self.page()
        self.assertIn(render.REVIEW_NO_CANDIDATE_LABEL, dom)
        self.assertNotIn("None", dom)
        # the reason still carries why the row is here
        self.assertIn("provenance_violation:headline", dom)

    def test_no_accept_reject_offered_without_a_candidate(self):
        dom = self.page()
        self.assertIn("No entity match to accept or reject", dom)
        self.assertNotIn(">Accept<", dom)
        self.assertNotIn(">Reject<", dom)

    def test_named_candidate_still_gets_the_controls(self):
        # the ordinary resolver row is unchanged
        self.path2 = make_db(seed_full)
        os.environ["GRIDSIGNALS_DB"] = self.path2
        try:
            dom = TestClient(app).get("/review").text
            self.assertIn(">Accept<", dom)
            self.assertNotIn(render.REVIEW_NO_CANDIDATE_LABEL, dom)
        finally:
            os.environ["GRIDSIGNALS_DB"] = self.path
            os.remove(self.path2)


class TestHtmlEscaping(ReviewTestBase):
    seed = staticmethod(seed_hostile_html)

    def test_upstream_text_is_escaped_in_the_dom(self):
        dom = self.page()
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", dom)
        self.assertIn("&lt;script&gt;alert(2)&lt;/script&gt;", dom)
        self.assertIn("&lt;b&gt;resolver&lt;/b&gt;", dom)
        self.assertIn("&lt;img src=x onerror=alert(3)&gt;", dom)
        self.assertIn("&lt;svg onload=alert(4)&gt;", dom)
        self.assertIn("&lt;script&gt;segment&lt;/script&gt;", dom)
        for raw in ("<img", "<script>", "<b>resolver</b>", "<svg"):
            self.assertNotIn(raw, dom)


if __name__ == "__main__":
    unittest.main()
