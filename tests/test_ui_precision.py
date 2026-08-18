"""Feedback / Precision page + data-layer tests (R8.6, R9.2-R9.5, R9.12).

Two layers, both hermetic (real migrations, FK on, no network):

1. Data layer — an in-memory DB seeded with signals across dimensions plus
   feedback + audit rows; asserts the four precision_* helpers return dicts
   with every key the precision view consumes and correct joins (source_id
   resolved via raw_events, trigger_name present, entity_id None for a sector
   signal, half-life picks the latest feedback verdict per signal). These
   data.py-direct reads are framework-agnostic and stay UNCHANGED from the
   Streamlit era.

2. Route layer — a temp-file DB (GRIDSIGNALS_DB) fetched through a FastAPI
   TestClient (replacing the old Streamlit AppTest). (a) EMPTY: 0 feedback /
   0 audit — GET /precision must be 200 and render the honest low-n / empty copy
   with no fake gauge. (b) POPULATED: enough feedback + audit to exercise the
   headline gauge, G1 account-only slicing, tables, reason codes, disagreement,
   half-life, and run history.
"""
import html
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.audit import precision
from app.db.migrate import apply_migrations
from app.ui import data
from app.ui_web import render
from app.ui_web.app import app

# Real current time, not a fixed past date: data.precision_report now windows
# its headline useful=/auto= rate, its G2 recommendation, and its per-dimension
# tables to the UTC calendar month containing `now` (R9.3/R9.5), and the
# `/precision` and `/admin` routes call it with no `now` override (real
# wall-clock time). A fixed historical NOW would drift out of that window as
# real time moves past it, silently breaking every fixture below that relies
# on its feedback/audit rows landing in the "current" reporting month.
NOW = datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def days_ago_date(n):
    return (NOW.date() - timedelta(days=n)).isoformat()


def _schema(conn):
    """Sources, triggers, entities, raw events — the FK chain signals need."""
    for sid, name in [("sp_edgar", "EDGAR"), ("sp_ferc", "FERC RSS")]:
        conn.execute(
            "INSERT INTO source_policies (source_id, name, enabled, ttl, "
            "access_method, evidence_rank) VALUES (?,?,1,3600,'rss',2)",
            (sid, name))
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES "
                 "('leadership_change','Leadership change',4,90)")
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES "
                 "('nerc_enforcement','NERC enforcement',5,180)")
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES "
                 "('reg_calendar','Regulatory calendar',3,600)")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector) "
        "VALUES ('E_ACME','Acme Energy','iou_electric')")
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, url) "
        "VALUES ('re_edgar','sp_edgar', ?, 'http://edgar/8k')",
        (days_ago_date(5),))


def _signal(conn, sid, entity_id, scope, trigger_id, raw_event_id,
            incident=None, score_decay=None, status="active"):
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, signal_scope, "
        "trigger_id, event_date, headline, incident_evidence_level, "
        "score, score_decay, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sid, raw_event_id, entity_id, scope, trigger_id, days_ago_date(5),
         f"headline {sid}", incident, 3.0, score_decay, status))


def _feedback(conn, sid, verdict, reason=None, ts=None):
    conn.execute(
        "INSERT INTO feedback (signal_id, verdict, reason_code, ts, note) "
        "VALUES (?,?,?,?, '')",
        (sid, verdict, reason, ts or iso(NOW)))


def _audit(conn, sid, check_type, result, ts=None):
    conn.execute(
        "INSERT INTO audit (signal_id, check_type, result, model_id, "
        "prompt_version, ts, parser_version) VALUES (?,?,?, 'claude-haiku', "
        "'p/1', ?, 'parse/1')",
        (sid, check_type, result, ts or iso(NOW)))


def seed_dims(conn):
    """A handful of signals spanning account vs sector scope + both sources, one
    account signal with two feedback rows (latest-verdict-wins probe)."""
    _schema(conn)
    # account signal on EDGAR, incident tier confirmed
    _signal(conn, "S_ACC", "E_ACME", "account", "leadership_change",
            "re_edgar", incident="confirmed", score_decay=0.8)
    # sector signal, no raw_event -> source_id None, entity_id None
    _signal(conn, "S_SEC", None, "sector", "reg_calendar", None)
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, url) "
        "VALUES ('re_ferc','sp_ferc', ?, 'http://ferc/rule')",
        (days_ago_date(3),))
    conn.execute(
        "UPDATE signals SET raw_event_id='re_ferc' WHERE signal_id='S_SEC'")

    # S_ACC: two feedback rows; latest (newer ts) is not_useful
    _feedback(conn, "S_ACC", "useful", ts=iso(NOW - timedelta(days=2)))
    _feedback(conn, "S_ACC", "not_useful", reason="weak_evidence", ts=iso(NOW))
    _feedback(conn, "S_SEC", "useful", ts=iso(NOW))

    _audit(conn, "S_ACC", "entity_match", "pass")
    _audit(conn, "S_ACC", "evidence_support", "fail")
    _audit(conn, "S_SEC", "entity_match", "pass")


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


# ---------------------------------------------------------------------------
# Data-layer tests (UNCHANGED — framework-agnostic data.py reads)
# ---------------------------------------------------------------------------

class TestPrecisionReads(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        apply_migrations(self.conn)
        seed_dims(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_feedback_rows_keys_and_joins(self):
        rows = data.precision_feedback_rows(self.conn)
        self.assertEqual(len(rows), 3)
        required = {"signal_id", "verdict", "reason_code", "ts", "trigger_id",
                    "trigger_name", "source_id", "signal_scope",
                    "incident_evidence_level", "entity_id"}
        for r in rows:
            self.assertTrue(required.issubset(r.keys()))
        by_sig = {}
        for r in rows:
            by_sig.setdefault(r["signal_id"], []).append(r)
        acc = by_sig["S_ACC"][0]
        self.assertEqual(acc["source_id"], "sp_edgar")       # via raw_events
        self.assertEqual(acc["trigger_name"], "Leadership change")
        self.assertEqual(acc["entity_id"], "E_ACME")
        sec = by_sig["S_SEC"][0]
        self.assertEqual(sec["source_id"], "sp_ferc")
        self.assertIsNone(sec["entity_id"])                  # sector scope

    def test_audit_rows_keys_and_joins(self):
        rows = data.precision_audit_rows(self.conn)
        self.assertEqual(len(rows), 3)
        required = {"signal_id", "check_type", "result", "model_id",
                    "prompt_version", "ts", "trigger_id", "trigger_name",
                    "source_id", "signal_scope", "incident_evidence_level",
                    "entity_id"}
        for r in rows:
            self.assertTrue(required.issubset(r.keys()))
        acc = [r for r in rows if r["signal_id"] == "S_ACC"]
        self.assertEqual({r["result"] for r in acc}, {"pass", "fail"})
        self.assertEqual(acc[0]["source_id"], "sp_edgar")

    def test_halflife_latest_verdict_per_signal(self):
        rows = data.precision_halflife_rows(self.conn)
        # one row per signal (2 signals), not per feedback row
        self.assertEqual(len(rows), 2)
        by_sig = {r["signal_id"]: r for r in rows}
        self.assertIn("decay_half_life_days", by_sig["S_ACC"].keys())
        # S_ACC has two feedback rows; the LATEST (by ts) is not_useful
        self.assertEqual(by_sig["S_ACC"]["verdict"], "not_useful")
        self.assertEqual(by_sig["S_ACC"]["trigger_name"], "Leadership change")
        self.assertEqual(by_sig["S_ACC"]["score_decay"], 0.8)

    def test_audit_run_rows_newest_first(self):
        for i, started in enumerate([iso(NOW - timedelta(hours=2)), iso(NOW)]):
            self.conn.execute(
                "INSERT INTO audit_runs (run_id, started_at, status, "
                "verdicts_written, budget_spent, skipped_reason) "
                "VALUES (?,?,?,?,?,?)",
                (f"run{i}", started, "success", 3, 0.01, ""))
        self.conn.commit()
        rows = data.audit_run_rows(self.conn)
        self.assertEqual([r["run_id"] for r in rows], ["run1", "run0"])
        self.assertIn("skipped_reason", rows[0].keys())


# ---------------------------------------------------------------------------
# Route tests (FastAPI TestClient — replaces the Streamlit AppTest)
# ---------------------------------------------------------------------------

def seed_empty(conn):
    """Signals only — 0 feedback, 0 audit, 0 runs (the current DB state)."""
    _schema(conn)
    _signal(conn, "S_ACC", "E_ACME", "account", "leadership_change",
            "re_edgar", incident="confirmed")


def seed_populated(conn):
    """Enough account feedback (n>=20) + audit to draw the gauge and tables,
    plus a below-threshold source, a comparable disagreement, and a run row."""
    _schema(conn)
    # 25 account cards on leadership_change / EDGAR: 20 useful, 5 not_useful
    for i in range(25):
        sid = f"S_ACC{i:02d}"
        conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id, event_date, url) "
            "VALUES (?, 'sp_edgar', ?, 'http://edgar/x')",
            (f"re_{i:02d}", days_ago_date(40)))
        _signal(conn, sid, "E_ACME", "account", "leadership_change",
                f"re_{i:02d}", incident="confirmed", score_decay=0.7)
        # Feedback ts is left at the _feedback default (iso(NOW)) rather than
        # backdated: data.precision_report now windows useful_overall/g2/the
        # per-dimension tables to the calendar month containing `now` (R9.3/
        # R9.5), and this route calls it with no `now` override (real
        # wall-clock time) - a backdated ts would fall out of that window and
        # this fixture's 25 account cards would silently stop counting toward
        # the headline. No assertion below needs G1's days_span (G1 stays on
        # the full, unwindowed history), so there is nothing to preserve by
        # backdating.
        if i < 20:
            _feedback(conn, sid, "useful")
            _audit(conn, sid, "entity_match", "pass")
            _audit(conn, sid, "evidence_support", "pass")
        else:
            _feedback(conn, sid, "not_useful", reason="weak_evidence")
            _audit(conn, sid, "entity_match", "pass")
            _audit(conn, sid, "evidence_support", "fail")
    # a sector card reported separately
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, url) "
        "VALUES ('re_ferc','sp_ferc', ?, 'http://ferc/rule')",
        (days_ago_date(3),))
    _signal(conn, "S_SEC", None, "sector", "reg_calendar", "re_ferc")
    _feedback(conn, "S_SEC", "useful", ts=iso(NOW))
    _audit(conn, "S_SEC", "entity_match", "pass")
    # a run row for the history panel
    conn.execute(
        "INSERT INTO audit_runs (run_id, started_at, finished_at, model_id, "
        "status, signals_sampled, verdicts_written, budget_spent, "
        "skipped_reason) VALUES ('run0', ?, ?, 'claude-haiku', 'success', "
        "26, 51, 0.12, '')", (iso(NOW), iso(NOW)))


def seed_gated_source(conn):
    """One source (sp_edgar) that WOULD be demoted by base G2 (low precision,
    n>=20) but is GATED OFF by R9.11: 30 account signals, 4 useful / 26
    not_useful (13% precision < 40%), each judged 'pass' (judge positive) so the
    26 not_useful humans DISAGREE with the judge -> 87% disagreement over 30
    comparable (>20%, >= floor). g2_gated must withhold the demote recommendation
    on BOTH surfaces. sp_ferc is left with no rated feedback (n/a control)."""
    _schema(conn)
    for i in range(30):
        sid = f"S_G{i:02d}"
        conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id, event_date, url) "
            "VALUES (?, 'sp_edgar', ?, 'http://edgar/x')",
            (f"reg_{i:02d}", days_ago_date(20)))
        _signal(conn, sid, "E_ACME", "account", "leadership_change",
                f"reg_{i:02d}", incident="confirmed")
        # judge always 'pass' (judge positive)
        _audit(conn, sid, "entity_match", "pass")
        if i < 4:
            _feedback(conn, sid, "useful")               # agrees with judge
        else:
            _feedback(conn, sid, "not_useful",
                      reason="weak_evidence")             # disagrees with judge


class PrecisionRouteCase(unittest.TestCase):
    def setUp(self):
        self.path = None
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("GRIDSIGNALS_DB", None)
        if self.path and os.path.exists(self.path):
            try:
                os.remove(self.path)
            except PermissionError:
                pass

    def _get(self, seed):
        self.path = make_db(seed)
        os.environ["GRIDSIGNALS_DB"] = self.path
        resp = self.client.get("/precision")
        self.assertEqual(resp.status_code, 200)
        return resp.text


class TestEmptyState(PrecisionRouteCase):
    def test_empty_renders_honest_lown_no_fake_gauge(self):
        text = self._get(seed_empty)
        # framing / trust line present
        self.assertIn("NOT validated sales lift", text)
        # low-n headline, not a fake percentage
        self.assertIn("not enough rated cards yet", text)
        self.assertIn("0 audit verdicts yet", text)
        # empty section copy
        self.assertIn("0 not-useful ratings yet", text)
        self.assertIn("0 audit runs yet", text)
        # the headline useful-rate label is present but renders "n/a", never a
        # fabricated gauge percentage, on zero data
        self.assertIn("Human useful-rate", text)
        self.assertNotIn("useful-rate: 0%", text)


class TestPopulatedState(PrecisionRouteCase):
    def test_populated_gauge_and_tables_render(self):
        text = self._get(seed_populated)
        # headline gauge shows the overall useful-rate with its n (26 rated:
        # 25 account + 1 sector) — never a bare percentage
        self.assertIn("n=26 rated cards", text)
        # G1 account trigger shows its own n=25 account-only slice
        self.assertIn("useful-rate: 80% (n=25)", text)
        # G1 primary trigger surfaced
        self.assertIn("leadership_change", text)
        # separated reporting block present (R9.4)
        self.assertIn("reported separately", text)
        # reason code distribution populated
        self.assertIn("weak_evidence", text)
        # run history populated
        self.assertIn("audit run history", text)
        # framing caption still present
        self.assertIn("NOT validated sales lift", text)


class TestG1Waiver(PrecisionRouteCase):
    """The R9.4 Gate G1 operator waiver as the page renders it (KTD1).

    The waiver banner is a disclosure, so the page must say "waived AND not
    passed" while leaving the gate's own unmet conditions on screen unchanged —
    and it must never say ELIGIBLE.
    """
    def test_waiver_banner_renders_with_its_reason(self):
        text = html.unescape(self._get(seed_empty))
        # The frozen waiver-FACT sentence, verbatim, in the DOM.
        self.assertIn(precision.G1_WAIVER_FACT, text)
        self.assertIn("WAIVED by operator ruling 2026-08-16", text)
        self.assertIn("not passed", text)
        # The live-computed conditions clause: seed_empty has 0 rated cards,
        # so the gate's own conditions are genuinely still unmet.
        self.assertIn("gate's own conditions above are unmet", text)
        # The recorded lift condition travels with it.
        self.assertIn(precision.G1_WAIVER_LIFT_CONDITION, text)

    def test_waived_page_keeps_blocked_badges_and_never_says_eligible(self):
        text = html.unescape(self._get(seed_empty))
        # Per-trigger evidence untouched: both primary triggers still BLOCKED
        # with their blocked: reason lines.
        self.assertIn("BLOCKED", text)
        self.assertIn("blocked:", text)
        for trigger in precision.DEFAULT_PRIMARY_TRIGGERS:
            self.assertIn(trigger, text)
        # A waived gate is never a passed gate.
        self.assertNotIn("ELIGIBLE", text)

    def test_waived_page_renders_no_zero_denominator_percentage(self):
        # Standing R8.6 rule: an empty denominator reads n/a with its n, never
        # a fabricated 0%.
        text = html.unescape(self._get(seed_empty))
        self.assertIn("n/a (0 rated)", text)
        self.assertNotIn("0% (n=0)", text)
        self.assertNotIn("useful-rate: 0%", text)
        self.assertNotIn("auto-accuracy: 0%", text)


class TestR911Gate(PrecisionRouteCase):
    def _get_both(self, seed):
        self.path = make_db(seed)
        os.environ["GRIDSIGNALS_DB"] = self.path
        prec = self.client.get("/precision")
        adm = self.client.get("/admin")
        self.assertEqual(prec.status_code, 200)
        self.assertEqual(adm.status_code, 200)
        return prec.text, adm.text

    def test_gated_source_withheld_on_both_surfaces(self):
        # The two-surface regression test: a source that base G2 would demote is
        # withheld by R9.11, and Precision + Admin must AGREE on that.
        prec, adm = self._get_both(seed_gated_source)
        # Precision page flags the source as withheld and NOT DEMOTE?
        self.assertIn("judge verdicts withheld from demotion", prec)
        self.assertIn("gate: withheld", prec)
        # Admin source table shows the SAME gated recommendation for sp_edgar:
        # the withheld note is present and it is not recommending demotion.
        self.assertIn("judge verdicts withheld from demotion", adm)
        self.assertIn("gate: withheld", adm)
        # The gate suppressed the recommendation, so the base demote guidance
        # ("consider store-only/disable") must NOT appear on either surface —
        # the source is not being recommended for demotion anymore.
        self.assertNotIn("consider store-only/disable", prec)
        self.assertNotIn("consider store-only/disable", adm)

    def test_precision_spotcheck_tracker_present(self):
        text, _ = self._get_both(seed_gated_source)
        self.assertIn("audit spot-check coverage", text)
        self.assertIn("Spot-check:", text)

    def test_no_rated_feedback_reads_na(self):
        # sp_ferc has no rated feedback in this seed -> its G2 gate is absent
        # (no g2 cell) on Admin, i.e. the honest "no rated feedback" copy.
        _, adm = self._get_both(seed_gated_source)
        self.assertIn("no rated feedback for this source yet", adm)


class TestG1WaiverViewBranches(unittest.TestCase):
    """``render.precision_g1_view`` over all three overall-line states (KTD1).

    The route tests above can only reach the ``waived`` branch: the shipped
    ``G1_WAIVER_ACTIVE`` is True and ``data.precision_report`` calls
    ``g1_status`` with no override, so every seeded route case now renders the
    waiver. That left the ``eligible`` and blocked branches — and the
    ``waiver is None`` fallback this PR added — with no test execution path at
    the view layer, which is exactly the coverage the waiver's documented
    rollback depends on. ``precision_g1_view`` takes a plain dict, so the
    branches are pinned here directly with no DB.
    """

    @staticmethod
    def _g1(state, waiver, eligible):
        return {
            "triggers": {"leadership_change": {
                "meets": False, "useful_rate": None, "useful_n": 0,
                "auto_accuracy": None, "auto_n": 0, "days_span": 0.0,
                "reasons": ["n<20 rated account cards (have 0)"]}},
            "reported_separately": {"feedback": {"rate": None, "total": 0},
                                    "auto": {"accuracy": None, "scored": 0}},
            "eligible": eligible,
            "state": state,
            "waiver": waiver,
            "conditions_still_unmet": not eligible,
        }

    def test_blocked_state_renders_no_waiver(self):
        out = render.precision_g1_view(self._g1("blocked", None, False))
        self.assertFalse(out["waived"])
        self.assertIsNone(out["waiver_reason"])
        self.assertIsNone(out["waiver_lift"])
        self.assertFalse(out["eligible"])

    def test_eligible_state_renders_no_waiver(self):
        out = render.precision_g1_view(self._g1("eligible", None, True))
        self.assertFalse(out["waived"])
        self.assertIsNone(out["waiver_reason"])
        self.assertIsNone(out["waiver_lift"])
        # The pre-waiver pass path is untouched by this PR.
        self.assertTrue(out["eligible"])

    def test_waived_state_forwards_reason_and_lift(self):
        # eligible=False here -> the live "conditions still unmet" clause
        # (U16) is appended to the frozen waiver-fact half.
        waiver = {"date": precision.G1_WAIVER_DATE,
                  "reason": precision.G1_WAIVER_FACT,
                  "lift_condition": precision.G1_WAIVER_LIFT_CONDITION}
        out = render.precision_g1_view(self._g1("waived", waiver, False))
        self.assertTrue(out["waived"])
        self.assertIn(precision.G1_WAIVER_FACT, out["waiver_reason"])
        self.assertIn("gate's own conditions above are unmet",
                      out["waiver_reason"])
        self.assertEqual(out["waiver_lift"], precision.G1_WAIVER_LIFT_CONDITION)
        # A waiver is a disclosure, never a pass: the computed value passes
        # through untouched (KTD1).
        self.assertFalse(out["eligible"])

    def test_waived_state_with_conditions_now_met_changes_the_wording(self):
        # U16, PR #78 finding #2: the banner's "unmet" claim must be LIVE, not
        # frozen — if a primary trigger ever crosses the gate while the
        # waiver stays active (recorded, not enforced), the wording flips.
        waiver = {"date": precision.G1_WAIVER_DATE,
                  "reason": precision.G1_WAIVER_FACT,
                  "lift_condition": precision.G1_WAIVER_LIFT_CONDITION}
        out = render.precision_g1_view(self._g1("waived", waiver, True))
        self.assertTrue(out["waived"])
        self.assertIn(precision.G1_WAIVER_FACT, out["waiver_reason"])
        self.assertIn("gate's own conditions above are now met",
                      out["waiver_reason"])
        self.assertNotIn("are unmet", out["waiver_reason"])
        # The waiver is still recorded as active — a met gate does not lift
        # it on its own (Open Question 7 / KTD1: recorded, not enforced).
        self.assertTrue(out["waived"])

    def test_blocked_reasons_survive_every_state(self):
        # The per-trigger BLOCKED badge and its reason line must be identical
        # whether or not the waiver is active — the waiver discloses the failed
        # gate, it never hides the conditions it failed.
        for state, waiver in (("blocked", None),
                              ("waived", {"date": "2026-08-16", "reason": "r",
                                          "lift_condition": "l"})):
            with self.subTest(state=state):
                out = render.precision_g1_view(self._g1(state, waiver, False))
                self.assertEqual(out["triggers"][0]["badge"], "BLOCKED")
                self.assertIn("n<20 rated account cards",
                              out["triggers"][0]["blocked"])


if __name__ == "__main__":
    unittest.main()
