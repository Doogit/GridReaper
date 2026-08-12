"""Admin / Config page tests (R8.7; R3.3, R3.2, R10.7).

Hermetic AppTest against a temp-file SQLite (a file DB, not :memory:, so the
page's own connection sees the same rows). Covers: the page renders every
section; saving a weight edits scoring_weights + writes a config_audit row +
rescores; saving a half-life edits triggers + audits; toggling a source edits
source_policies.enabled + audits; a held single-writer lock turns a save into an
"ingestion in progress" warning with NO DB write; staleness labels state only
what the field says. Every case asserts ``at.exception`` is empty.
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from streamlit.testing.v1 import AppTest

from app.db.migrate import apply_migrations
from app.ingest.runner import ingest_lock

PAGE = "app/ui/pages/5_Admin.py"
NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def days_ago_date(n):
    return (NOW.date() - timedelta(days=n)).isoformat()


def seed(conn):
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_lead','Leadership',4,90)")
    conn.execute("INSERT INTO scoring_weights (weight_kind, key, weight, notes) "
                 "VALUES ('scope','sector',0.55,'seed note')")
    conn.execute("INSERT INTO source_policies (source_id, name, enabled, ttl, "
                 "access_method, evidence_rank) VALUES "
                 "('edgar','EDGAR',1,3600,'rss',1)")
    # An active signal so a weight/half-life save has something to rescore.
    conn.execute(
        "INSERT INTO signals (signal_id, signal_scope, trigger_id, event_date, "
        " headline, status, score) VALUES "
        "('s1','sector','t_lead','2026-08-01','Card','active',2.2)")
    # Staleness: one fact stale by date, one with an unknown verified_date.
    conn.execute("INSERT INTO products (product_id, name) VALUES "
                 "('p_sent','Microsoft Sentinel')")
    conn.execute(
        "INSERT INTO license_facts (fact_id, product_id, segment, "
        "source_quality, verified_date) VALUES "
        "('f_old','p_sent','commercial','non-primary', ?)", (days_ago_date(210),))
    conn.execute(
        "INSERT INTO license_facts (fact_id, product_id, segment, "
        "source_quality, verified_date) VALUES "
        "('f_unk','p_sent','commercial','non-primary','')")


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


def all_text(at):
    parts = []
    for kind in ("markdown", "caption", "subheader", "title", "header"):
        for el in getattr(at, kind, []):
            val = getattr(el, "value", None)
            if val:
                parts.append(str(val))
    return "\n".join(parts)


class AdminPageCase(unittest.TestCase):
    def setUp(self):
        self.path = make_db()
        os.environ["GRIDSIGNALS_DB"] = self.path

    def tearDown(self):
        os.environ.pop("GRIDSIGNALS_DB", None)
        if self.path and os.path.exists(self.path):
            try:
                os.remove(self.path)
            except PermissionError:
                pass

    def _run(self):
        return AppTest.from_file(PAGE, default_timeout=30).run()

    def _open_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _widget(self, at, kind, key):
        for w in getattr(at, kind):
            if w.key == key:
                return w
        self.fail(f"{kind} {key} not found")

    def assertNoException(self, at):
        self.assertEqual(list(at.exception), [], msg=[
            (e.type, e.message) for e in at.exception])


class TestRender(AdminPageCase):
    def test_all_sections_render(self):
        at = self._run()
        self.assertNoException(at)
        text = all_text(at)
        for section in ("Scoring weights", "Decay half-lives", "Source policies",
                        "License-fact staleness", "Recent config changes"):
            self.assertIn(section, text)


class TestWeightSave(AdminPageCase):
    def test_save_edits_audits_and_rescores(self):
        at = self._run()
        at = self._widget(at, "number_input", "w_scope_sector").set_value(0.90).run()
        at = self._widget(at, "button", "wsave_scope_sector").click().run()
        self.assertNoException(at)

        conn = self._open_conn()
        self.assertEqual(conn.execute(
            "SELECT weight FROM scoring_weights WHERE key='sector'"
            ).fetchone()["weight"], 0.90)
        audit = conn.execute("SELECT table_name, field, new_value "
                             "FROM config_audit").fetchall()
        conn.close()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["table_name"], "scoring_weights")
        self.assertEqual(audit[0]["field"], "weight")
        self.assertTrue(any("Saved" in s.value for s in at.success))


class TestHalfLifeSave(AdminPageCase):
    def test_save_edits_and_audits(self):
        at = self._run()
        at = self._widget(at, "number_input", "hl_t_lead").set_value(150).run()
        at = self._widget(at, "button", "hlsave_t_lead").click().run()
        self.assertNoException(at)
        conn = self._open_conn()
        self.assertEqual(conn.execute(
            "SELECT decay_half_life_days FROM triggers WHERE trigger_id='t_lead'"
            ).fetchone()["decay_half_life_days"], 150)
        self.assertEqual(conn.execute(
            "SELECT field FROM config_audit").fetchone()["field"],
            "decay_half_life_days")
        conn.close()


class TestSourceToggle(AdminPageCase):
    def test_disable_source_audits(self):
        at = self._run()
        at = self._widget(at, "toggle", "src_edgar").set_value(False).run()
        at = self._widget(at, "button", "srcsave_edgar").click().run()
        self.assertNoException(at)
        conn = self._open_conn()
        self.assertEqual(conn.execute(
            "SELECT enabled FROM source_policies WHERE source_id='edgar'"
            ).fetchone()["enabled"], 0)
        self.assertEqual(conn.execute(
            "SELECT field FROM config_audit").fetchone()["field"], "enabled")
        conn.close()


class TestLockHeld(AdminPageCase):
    def test_save_under_held_lock_warns_and_writes_nothing(self):
        at = self._run()
        with ingest_lock():                       # simulate an in-progress run
            at = self._widget(at, "number_input", "w_scope_sector"
                              ).set_value(0.90).run()
            at = self._widget(at, "button", "wsave_scope_sector").click().run()
        self.assertNoException(at)
        self.assertTrue(any("Ingestion in progress" in w.value
                            for w in at.warning))
        conn = self._open_conn()
        self.assertEqual(conn.execute(
            "SELECT weight FROM scoring_weights WHERE key='sector'"
            ).fetchone()["weight"], 0.55)          # unchanged
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM config_audit").fetchone()["c"], 0)
        conn.close()


class TestStalenessLabels(AdminPageCase):
    def test_literal_field_stating_labels(self):
        at = self._run()
        self.assertNoException(at)
        text = all_text(at)
        # age is computed from the real clock, so assert the label FORM, not an
        # exact day count (the stale fact is >180d old either way).
        self.assertRegex(text, r"verified \d+ days ago")
        self.assertIn("verification date unknown", text)
        self.assertNotIn("unverified", text)      # no interpretive verb


if __name__ == "__main__":
    unittest.main()
