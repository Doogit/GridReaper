"""Review Queue / Triage page tests (R8.2, R10.3/G2, R10.7).

Hermetic AppTest against a temp-file SQLite built by real migrations + fixture
rows (a file DB, not :memory:, so the page's own connection sees the same data).
Covers: a pending match renders with candidate name + resolver reason + snippet
+ source link; Accept writes entity_match_decisions(decided_by='human',
decision='reviewed') + review_queue disposition='accepted' and drops the item
from the pending list on rerun; Reject writes decision='rejected'/'rejected';
source health classifies all five states with the errored source's error_state
shown; stale facts list shows the fixture stale fact + header count while a
fresh fact is excluded; an empty review queue renders the positive empty state.
Every case asserts ``at.exception is None``.
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from streamlit.testing.v1 import AppTest

from app.db.migrate import apply_migrations

PAGE = "app/ui/pages/2_Review_Queue.py"
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


def all_text(at):
    """Concatenate markdown/caption/subheader/title text for substring asserts."""
    parts = []
    for kind in ("markdown", "caption", "subheader", "title", "header"):
        for el in getattr(at, kind, []):
            val = getattr(el, "value", None)
            if val:
                parts.append(str(val))
    return "\n".join(parts)


class ReviewPageCase(unittest.TestCase):
    def setUp(self):
        self.path = None

    def tearDown(self):
        os.environ.pop("GRIDSIGNALS_DB", None)
        if self.path and os.path.exists(self.path):
            try:
                os.remove(self.path)
            except PermissionError:
                # Windows: Streamlit's AppTest script cache may still hold the
                # file open; a leftover temp DB is harmless for a hermetic test.
                pass

    def _run(self, seed):
        self.path = make_db(seed)
        os.environ["GRIDSIGNALS_DB"] = self.path
        return AppTest.from_file(PAGE, default_timeout=30).run()

    def _open_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def assertNoException(self, at):
        # AppTest.exception is an (empty) ElementList when the run is clean, not
        # None; assert it carries no exception element.
        self.assertEqual(list(at.exception), [], msg=[
            (e.type, e.message) for e in at.exception])


class TestPendingMatches(ReviewPageCase):
    def test_pending_renders_candidate_reason_snippet_source(self):
        at = self._run(seed_full)
        self.assertNoException(at)
        text = all_text(at)
        self.assertIn("Dark Muni Co", text)                 # candidate name
        self.assertIn("fuzzy_below_threshold", text)        # resolver reason
        self.assertIn("Ambiguous Utility Co filing", text)  # evidence snippet
        self.assertIn("http://rev/doc", text)               # source link

    def _button(self, at, key):
        for b in at.button:
            if b.key == key:
                return b
        self.fail(f"button {key} not found")

    def test_accept_writes_both_tables_and_leaves_pending(self):
        at = self._run(seed_full)
        self.assertNoException(at)
        at = self._button(at, "accept_1").click().run()
        self.assertNoException(at)

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

        # item is gone from the pending list -> positive empty state shows
        self.assertIn("0 pending", all_text(at))
        self.assertNotIn("fuzzy_below_threshold", all_text(at))

    def test_reject_writes_rejected_disposition(self):
        at = self._run(seed_full)
        self.assertNoException(at)
        at = self._button(at, "reject_1").click().run()
        self.assertNoException(at)

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


class TestSourceHealth(ReviewPageCase):
    def test_five_states_classified_and_error_verbatim(self):
        at = self._run(seed_full)
        self.assertNoException(at)
        text = all_text(at)
        for label in ("OK", "ERROR", "NEVER RUN", "STALE", "DISABLED"):
            self.assertIn(label, text)
        # each source is named
        for name in ("OK Source", "Err Source", "Never Source",
                     "Stale Source", "Disabled Source"):
            self.assertIn(name, text)
        # error text shown verbatim for the errored source
        self.assertIn("HTTP 503 from upstream", text)


class TestStaleFacts(ReviewPageCase):
    def test_stale_listed_with_count_fresh_excluded(self):
        at = self._run(seed_full)
        self.assertNoException(at)
        text = all_text(at)
        self.assertIn("Stale license facts (1)", text)  # header count
        self.assertIn("Microsoft Sentinel", text)       # the stale fact
        # the stale fact's verified_date (400d before NOW) is surfaced; the page
        # uses the real clock for age, so assert the date not the exact day count
        self.assertIn(days_ago_date(400), text)
        # fresh fact (30d) is not surfaced as stale — its verified date is absent
        self.assertNotIn(days_ago_date(30), text)


class TestEmptyState(ReviewPageCase):
    def test_empty_queue_positive_state(self):
        at = self._run(seed_empty)
        self.assertNoException(at)
        text = all_text(at)
        self.assertIn("0 pending", text)
        self.assertIn("needed no human help", text)
        # page still renders the other sections without error
        self.assertIn("Source health", text)


class TestHtmlEscaping(ReviewPageCase):
    def test_upstream_text_is_escaped_inside_html_blocks(self):
        at = self._run(seed_hostile_html)
        self.assertNoException(at)
        text = all_text(at)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", text)
        self.assertIn("&lt;script&gt;alert(2)&lt;/script&gt;", text)
        self.assertIn("&lt;b&gt;resolver&lt;/b&gt;", text)
        self.assertIn("&lt;img src=x onerror=alert(3)&gt;", text)
        self.assertIn("&lt;svg onload=alert(4)&gt;", text)
        self.assertIn("&lt;script&gt;segment&lt;/script&gt;", text)
        for raw in ("<img", "<script>", "<b>resolver</b>", "<svg"):
            self.assertNotIn(raw, text)


if __name__ == "__main__":
    unittest.main()
