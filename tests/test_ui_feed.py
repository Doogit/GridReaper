"""Signal Feed + card component AppTest suite (R8.1, R7.2, R6.6, R9.1, R4.3).

Hermetic: a temp-file SQLite DB (not :memory: — AppTest opens its own
connection) built by apply_migrations + fixture rows, pointed at the page via
the GRIDSIGNALS_DB env override. AppTest runs no server and no network.

Covers: cards render from fixture signals (account + sector, scope-separated);
feedback insert + reason-code-required on not_useful; superseded/decayed hidden
by default and reachable via the status filter; outreach shown only when
customer_facing_allowed=1 and only from outreach_safe_text; non-primary fact
chips badged with NO price text anywhere in the DOM.
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from streamlit.testing.v1 import AppTest

from app.db.migrate import apply_migrations
from app.ui import data

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
HOME = "app/ui/Home.py"
# A non-primary price string that must NEVER reach the DOM (R4.3/R7.11).
FORBIDDEN_PRICE = "$9/GB rumored"


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def days_ago(n):
    return (NOW.date() - timedelta(days=n)).isoformat()


def _add_signal(conn, sid, entity_id, scope, trigger_id, event_date, headline,
                cfa=0, status="active", score=None):
    raw = "re_acc" if scope in ("account", "parent") else None
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, signal_scope, "
        "trigger_id, event_date, headline, evidence_snippet, source_url, "
        "confidence, evidence_quality, customer_facing_allowed, score, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, raw, entity_id, scope, trigger_id, event_date, headline, headline,
         "http://src/doc", 0.9, "IR", cfa, score, status))


def seed(conn):
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_lead','Leadership',4,90)")
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_reg','Regulatory',5,600)")

    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector, "
        "richness, coverage_flag, gov_cloud_likelihood, tenant_cloud_environment) "
        "VALUES ('E_ACME','Acme Energy','iou_electric','high','edgar-visible',"
        "'possible','commercial')")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector, "
        "richness, coverage_flag, gov_cloud_likelihood, tenant_cloud_environment) "
        "VALUES ('E_DARK','Dark Muni Co','muni_public','low','dark','likely',"
        "'gcc_high')")

    conn.execute("INSERT INTO products (product_id, name) VALUES "
                 "('p_sentinel','Microsoft Sentinel')")
    conn.execute(
        "INSERT INTO license_play_candidates (play_id, trigger_id, product_id, "
        "recommended_path, discovery_question) VALUES "
        "('play1','t_lead','p_sentinel','Adopt E5 grant','What is your SIEM?')")
    # one primary + one non-primary fact carrying a price that must not leak
    conn.execute(
        "INSERT INTO license_facts (fact_id, product_id, segment, price_note, "
        "source_quality, source_url, verified_date) VALUES "
        "('f_primary','p_sentinel','commercial','$2/GB','primary',"
        "'http://primary', ?)", (days_ago(30),))
    conn.execute(
        "INSERT INTO license_facts (fact_id, product_id, segment, price_note, "
        "source_quality, source_url, verified_date) VALUES "
        "('f_nonprimary','p_sentinel','commercial',?,'non-primary',"
        "'http://blog', ?)", (FORBIDDEN_PRICE, days_ago(400)))

    conn.execute(
        "INSERT INTO source_policies (source_id, name, enabled, ttl, "
        "access_method, evidence_rank) VALUES ('sp_ok','OK',1,3600,'rss',2)")
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, payload, "
        "url) VALUES ('re_acc','sp_ok', ?, "
        "'{\"title\": \"Acme names new CISO\"}', 'http://acme/8k')",
        (days_ago(5),))

    # account (cfa=1, has snapshot), account (cfa=0, no outreach), sector,
    # regulatory, plus a decayed + a superseded to test the default hide.
    _add_signal(conn, "S_ACC1", "E_ACME", "account", "t_lead", days_ago(5),
                "Acme names new CISO", cfa=1, score=4.2)
    _add_signal(conn, "S_ACC2", "E_ACME", "account", "t_lead", days_ago(9),
                "Acme restricted outreach event", cfa=0, score=3.0)
    _add_signal(conn, "S_SEC", None, "sector", "t_reg", days_ago(2),
                "FERC final rule CIP revision", cfa=1, score=2.75)
    _add_signal(conn, "S_REG", None, "regulatory_calendar", "t_reg", days_ago(1),
                "FERC proposed rule virtualization", cfa=1, score=2.6)
    _add_signal(conn, "S_DEAD", "E_ACME", "account", "t_lead", days_ago(400),
                "Old decayed exec note", cfa=1, score=0.8, status="decayed")
    _add_signal(conn, "S_SUP", None, "regulatory_calendar", "t_reg", days_ago(20),
                "Superseded NOPR", cfa=1, score=2.0, status="superseded")

    conn.execute(
        "INSERT INTO signal_evidence (signal_id, raw_event_id, evidence_text, "
        "evidence_locator, evidence_rank) VALUES ('S_ACC1','re_acc',"
        "'Acme Energy appointed a new Chief Information Security Officer.',"
        "'para 2', 1)")
    # snapshot for the cfa=1 account signal: outreach text + a non-primary fact
    conn.execute(
        "INSERT INTO license_play_snapshots (signal_id, play_id, fact_ids, "
        "generated_at, generation_version, display_text, outreach_safe_text) "
        "VALUES ('S_ACC1','play1', '[\"f_primary\",\"f_nonprimary\"]', ?, "
        "'plays/1.0', 'Recommended path: Adopt E5 grant', "
        "'Given the recent leadership change, a licensing review may be timely.')",
        (iso(NOW),))
    # snapshot for the cfa=0 signal: outreach text present but must be withheld
    conn.execute(
        "INSERT INTO license_play_snapshots (signal_id, play_id, fact_ids, "
        "generated_at, generation_version, display_text, outreach_safe_text) "
        "VALUES ('S_ACC2','play1', '[]', ?, 'plays/1.0', "
        "'Recommended path: Adopt E5 grant', "
        "'SECRET_OUTREACH_SHOULD_NOT_APPEAR for restricted account.')",
        (iso(NOW),))

    conn.executemany(
        "INSERT INTO badge_legend (badge_kind, code, label, description) "
        "VALUES (?,?,?,?)",
        [("evidence_quality", "IR", "Investor Report", "From an investor filing"),
         ("source_quality", "non-primary", "Non-primary",
          "Not an authoritative source (R4.3)")])


def make_db():
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


def _all_markdown(at):
    """Every rendered markdown/caption/error body, concatenated."""
    chunks = []
    for el in at.markdown:
        chunks.append(el.value)
    for el in at.caption:
        chunks.append(el.value)
    return "\n".join(chunks)


class FeedTestBase(unittest.TestCase):
    def setUp(self):
        self.path = make_db()
        os.environ["GRIDSIGNALS_DB"] = self.path

    def tearDown(self):
        os.environ.pop("GRIDSIGNALS_DB", None)
        try:
            os.remove(self.path)
        except OSError:
            pass

    def run_app(self):
        at = AppTest.from_file(HOME, default_timeout=30).run()
        self.assertEqual(list(at.exception), [], msg=self._exc_msg(at))
        return at

    @staticmethod
    def _exc_msg(at):
        return "; ".join(f"{e.type}: {e.message}" for e in at.exception)


class TestCardsRender(FeedTestBase):
    def test_account_and_sector_cards_present(self):
        at = self.run_app()
        dom = _all_markdown(at)
        self.assertIn("Acme names new CISO", dom)          # account card
        self.assertIn("FERC final rule CIP revision", dom)  # sector card
        # divider label is html-escaped in the DOM
        self.assertIn("Sector &amp; regulatory", dom)

    def test_scope_divider_present(self):
        at = self.run_app()
        dom = _all_markdown(at)
        self.assertIn("gs-divider", dom)
        self.assertIn("Sector", dom)

    def test_non_primary_chip_badged_and_no_price_in_dom(self):
        at = self.run_app()
        dom = _all_markdown(at)
        self.assertIn("gs-badge nonprimary", dom)   # R4.3 chip rendered
        self.assertIn("Non-primary", dom)            # legend label used
        # R4.3/R7.11: the non-primary price string must never reach the DOM
        self.assertNotIn(FORBIDDEN_PRICE, dom)
        self.assertNotIn("$9", dom)


class TestStatusFilter(FeedTestBase):
    def test_decayed_and_superseded_hidden_by_default(self):
        at = self.run_app()
        dom = _all_markdown(at)
        self.assertNotIn("Old decayed exec note", dom)
        self.assertNotIn("Superseded NOPR", dom)

    def test_widen_status_to_all_reveals_them(self):
        at = self.run_app()
        at.selectbox[0].select("all").run()
        self.assertEqual(list(at.exception), [], msg=self._exc_msg(at))
        dom = _all_markdown(at)
        self.assertIn("Old decayed exec note", dom)
        self.assertIn("Superseded NOPR", dom)


class TestOutreachGate(FeedTestBase):
    def test_outreach_shown_only_when_customer_facing_allowed(self):
        at = self.run_app()
        dom = _all_markdown(at)
        # cfa=1 signal: its outreach_safe_text appears
        self.assertIn("a licensing review may be timely", dom)
        # cfa=0 signal: its outreach_safe_text must NOT appear anywhere
        self.assertNotIn("SECRET_OUTREACH_SHOULD_NOT_APPEAR", dom)


class TestFeedback(FeedTestBase):
    def _feedback_rows(self):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(
                "SELECT signal_id, verdict, reason_code FROM feedback").fetchall()
        finally:
            conn.close()

    def test_useful_button_inserts_row(self):
        at = self.run_app()
        # find the Useful button for S_ACC1 and click it
        btn = at.button(key="card_S_ACC1_useful")
        btn.click().run()
        self.assertEqual(list(at.exception), [], msg=self._exc_msg(at))
        rows = self._feedback_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "useful")

    def test_not_useful_requires_reason_then_submits(self):
        at = self.run_app()
        # clicking Not useful reveals the reason select; no row yet
        at.button(key="card_S_ACC1_notuseful_btn").click().run()
        self.assertEqual(list(at.exception), [], msg=self._exc_msg(at))
        self.assertEqual(len(self._feedback_rows()), 0)
        # select a reason and submit -> a not_useful row with that reason
        at.selectbox(key="card_S_ACC1_reason").select("wrong_entity").run()
        at.button(key="card_S_ACC1_submit").click().run()
        self.assertEqual(list(at.exception), [], msg=self._exc_msg(at))
        rows = self._feedback_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "not_useful")
        self.assertEqual(rows[0][2], "wrong_entity")

    def test_record_feedback_enforces_reason_required(self):
        """Direct guard on the wiring: not_useful without a reason raises and
        writes nothing (R9.1)."""
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with self.assertRaises(ValueError):
                data.record_feedback(conn, "S_ACC1", "not_useful", now=NOW)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
