"""Pure unit tests for precision computation (R9.3, R9.4, R9.5).

Hand-built row lists only — no DB, no network. Asserts the trust invariant
throughout: every rate is returned with its denominator, and an empty
denominator yields ``None`` (never a fake 0.0 pass).
"""
import unittest

from app.audit import precision as p


def fb(signal_id, verdict, **kw):
    """Build a feedback_row with sane defaults for unset keys."""
    row = {
        "signal_id": signal_id,
        "verdict": verdict,
        "reason_code": None,
        "ts": "2026-01-01T00:00:00Z",
        "trigger_id": "leadership_change",
        "trigger_name": "Leadership change",
        "source_id": "src_a",
        "signal_scope": "account",
        "incident_evidence_level": None,
        "entity_id": "e1",
    }
    row.update(kw)
    return row


def au(signal_id, check_type, result, **kw):
    """Build an audit_row with sane defaults for unset keys."""
    row = {
        "signal_id": signal_id,
        "check_type": check_type,
        "result": result,
        "model_id": "m",
        "prompt_version": "judge/1.0",
        "ts": "2026-01-01T00:00:00Z",
        "trigger_id": "leadership_change",
        "trigger_name": "Leadership change",
        "source_id": "src_a",
        "signal_scope": "account",
        "incident_evidence_level": None,
        "entity_id": "e1",
    }
    row.update(kw)
    return row


class UsefulRateTests(unittest.TestCase):
    def test_positives_include_useful_and_converted(self):
        rows = [
            fb("s1", "useful"),
            fb("s2", "converted"),
            fb("s3", "not_useful", reason_code="wrong_entity"),
        ]
        overall = p.useful_rate_overall(rows)
        self.assertEqual(overall, {"useful": 2, "total": 3, "rate": 2 / 3})

    def test_empty_dimension_value_gives_none_rate_with_n(self):
        result = p.useful_rate([], "trigger")
        self.assertIsNone(result["overall"]["rate"])
        # Trust invariant: denominator still present.
        self.assertEqual(result["overall"]["total"], 0)

    def test_per_dimension_split(self):
        rows = [
            fb("s1", "useful", source_id="src_a"),
            fb("s2", "not_useful", source_id="src_a", reason_code="duplicate"),
            fb("s3", "useful", source_id="src_b"),
        ]
        result = p.useful_rate(rows, "source")
        self.assertEqual(result["src_a"], {"useful": 1, "total": 2, "rate": 0.5})
        self.assertEqual(result["src_b"], {"useful": 1, "total": 1, "rate": 1.0})
        self.assertEqual(result["overall"]["total"], 3)

    def test_rate_never_without_denominator(self):
        rows = [fb("s1", "useful"), fb("s2", "not_useful", reason_code="other")]
        for value, cell in p.useful_rate(rows, "trigger").items():
            self.assertIn("total", cell)  # denominator always carried


class AutoAccuracyTests(unittest.TestCase):
    def test_only_entity_match_and_evidence_support_counted(self):
        rows = [
            au("s1", "entity_match", "pass"),
            au("s1", "evidence_support", "fail"),
            au("s1", "classification", "fail"),        # ignored dimension check
            au("s1", "license_play_support", "fail"),  # ignored
        ]
        overall = p.auto_accuracy(rows, "trigger")["overall"]
        self.assertEqual(overall, {"correct": 1, "scored": 2, "accuracy": 0.5})

    def test_unclear_and_na_excluded_from_denominator(self):
        rows = [
            au("s1", "entity_match", "pass"),
            au("s2", "entity_match", "unclear"),
            au("s3", "evidence_support", "not_applicable"),
        ]
        overall = p.auto_accuracy(rows, "trigger")["overall"]
        self.assertEqual(overall, {"correct": 1, "scored": 1, "accuracy": 1.0})

    def test_none_accuracy_when_nothing_scored(self):
        rows = [au("s1", "entity_match", "unclear")]
        overall = p.auto_accuracy(rows, "trigger")["overall"]
        self.assertIsNone(overall["accuracy"])
        self.assertEqual(overall["scored"], 0)  # denominator present

    def test_no_rows_at_all(self):
        overall = p.auto_accuracy([], "source")["overall"]
        self.assertEqual(overall, {"correct": 0, "scored": 0, "accuracy": None})


class ReasonCodeDistributionTests(unittest.TestCase):
    def test_only_not_useful_rows_ordered_desc(self):
        rows = [
            fb("s1", "not_useful", reason_code="wrong_entity"),
            fb("s2", "not_useful", reason_code="wrong_entity"),
            fb("s3", "not_useful", reason_code="duplicate"),
            fb("s4", "useful"),  # positive: no reason code, ignored
            fb("s5", "converted"),
        ]
        dist = p.reason_code_distribution(rows)
        self.assertEqual(dist, [
            {"reason_code": "wrong_entity", "count": 2},
            {"reason_code": "duplicate", "count": 1},
        ])

    def test_tie_broken_by_code_asc(self):
        rows = [
            fb("s1", "not_useful", reason_code="stale_event"),
            fb("s2", "not_useful", reason_code="already_known"),
        ]
        dist = p.reason_code_distribution(rows)
        self.assertEqual(
            [d["reason_code"] for d in dist], ["already_known", "stale_event"])

    def test_empty(self):
        self.assertEqual(p.reason_code_distribution([]), [])


class JudgeHumanDisagreementTests(unittest.TestCase):
    def test_agree_case(self):
        audit = [au("s1", "entity_match", "fail")]           # judge negative
        feedback = [fb("s1", "not_useful", reason_code="wrong_entity")]  # human neg
        out = p.judge_human_disagreement(audit, feedback)
        self.assertEqual(out["comparable"], 1)
        self.assertEqual(out["agree"], 1)
        self.assertEqual(out["disagree"], 0)
        self.assertEqual(out["disagreement_rate"], 0.0)
        self.assertEqual(
            out["items"][0], {"signal_id": "s1", "judge": "negative",
                              "human": "negative"})

    def test_disagree_case(self):
        audit = [au("s2", "entity_match", "pass")]  # judge positive
        feedback = [fb("s2", "not_useful", reason_code="not_my_account")]  # human neg
        out = p.judge_human_disagreement(audit, feedback)
        self.assertEqual(out["comparable"], 1)
        self.assertEqual(out["disagree"], 1)
        self.assertEqual(out["disagreement_rate"], 1.0)
        self.assertEqual(out["items"][0]["judge"], "positive")
        self.assertEqual(out["items"][0]["human"], "negative")

    def test_comparable_excludes_signals_missing_one_side(self):
        audit = [
            au("s1", "entity_match", "pass"),
            au("s3", "entity_match", "pass"),   # no human feedback -> excluded
        ]
        feedback = [
            fb("s1", "useful"),
            fb("s4", "useful"),                 # no judge row -> excluded
        ]
        out = p.judge_human_disagreement(audit, feedback)
        self.assertEqual(out["comparable"], 1)
        self.assertEqual([i["signal_id"] for i in out["items"]], ["s1"])

    def test_none_rate_when_no_comparable(self):
        out = p.judge_human_disagreement([], [])
        self.assertIsNone(out["disagreement_rate"])
        self.assertEqual(out["comparable"], 0)


class HalfLifeEffectivenessTests(unittest.TestCase):
    def test_per_trigger_with_sample_sizes(self):
        rows = [
            {"signal_id": "s1", "trigger_id": "t1", "trigger_name": "T1",
             "decay_half_life_days": 30, "score": 5.0, "score_decay": 0.8,
             "status": "active", "event_date": "2026-01-01", "verdict": "useful"},
            {"signal_id": "s2", "trigger_id": "t1", "trigger_name": "T1",
             "decay_half_life_days": 30, "score": 4.0, "score_decay": 0.6,
             "status": "active", "event_date": "2026-01-02",
             "verdict": "not_useful"},
            {"signal_id": "s3", "trigger_id": "t1", "trigger_name": "T1",
             "decay_half_life_days": 30, "score": 3.0, "score_decay": None,
             "status": "active", "event_date": "2026-01-03", "verdict": None},
        ]
        out = p.half_life_effectiveness(rows)
        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertEqual(row["trigger_id"], "t1")
        self.assertEqual(row["cards"], 3)
        self.assertEqual(row["rated"], 2)
        self.assertEqual(row["useful"], 1)
        self.assertEqual(row["useful_rate"], 0.5)
        self.assertAlmostEqual(row["mean_score_decay"], 0.7)
        self.assertEqual(row["decay_samples"], 2)

    def test_none_useful_rate_when_no_feedback(self):
        rows = [{"signal_id": "s1", "trigger_id": "t2", "trigger_name": "T2",
                 "decay_half_life_days": 14, "score": 1.0, "score_decay": None,
                 "status": "active", "event_date": "2026-01-01",
                 "verdict": None}]
        out = p.half_life_effectiveness(rows)
        self.assertIsNone(out[0]["useful_rate"])
        self.assertEqual(out[0]["rated"], 0)          # denominator present
        self.assertIsNone(out[0]["mean_score_decay"])


class G1StatusTests(unittest.TestCase):
    NOW = "2026-03-01T00:00:00Z"  # ~59 days after 2026-01-01

    def _passing_feedback(self, trigger):
        # 25 account cards, 20 useful (80%) — >=60% and >=20.
        rows = []
        for i in range(20):
            rows.append(fb(f"{trigger}:u{i}", "useful", trigger_id=trigger,
                           entity_id="e1", signal_scope="account"))
        for i in range(5):
            rows.append(fb(f"{trigger}:n{i}", "not_useful", trigger_id=trigger,
                           entity_id="e1", signal_scope="account",
                           reason_code="other"))
        return rows

    def _passing_audit(self, trigger):
        # 90% pass on entity_match+evidence_support.
        rows = []
        for i in range(9):
            rows.append(au(f"{trigger}:a{i}", "entity_match", "pass",
                           trigger_id=trigger, entity_id="e1",
                           signal_scope="account"))
        rows.append(au(f"{trigger}:a9", "entity_match", "fail",
                       trigger_id=trigger, entity_id="e1",
                       signal_scope="account"))
        return rows

    def test_passing_scenario_meets_true(self):
        triggers = ("leadership_change", "nerc_enforcement")
        feedback, audit = [], []
        for t in triggers:
            feedback += self._passing_feedback(t)
            audit += self._passing_audit(t)
        out = p.g1_status(feedback, audit, primary_triggers=triggers,
                          now=self.NOW)
        for t in triggers:
            cell = out["triggers"][t]
            self.assertTrue(cell["meets"], f"{t} should meet: {cell['reasons']}")
            self.assertEqual(cell["useful_n"], 25)
            self.assertGreaterEqual(cell["days_span"], 30)
        self.assertTrue(out["eligible"])
        self.assertEqual(out["blocked_reasons"], [])

    def test_blocked_small_sample_meets_false_with_reason(self):
        # Only 5 account cards -> n<20.
        feedback = [fb(f"u{i}", "useful", trigger_id="leadership_change")
                    for i in range(5)]
        audit = [au(f"a{i}", "entity_match", "pass",
                    trigger_id="leadership_change") for i in range(3)]
        out = p.g1_status(feedback, audit,
                          primary_triggers=("leadership_change",), now=self.NOW)
        cell = out["triggers"]["leadership_change"]
        self.assertFalse(cell["meets"])
        self.assertTrue(any("n<20" in r for r in cell["reasons"]))
        self.assertFalse(out["eligible"])
        # Never a fake pass: rate is real, n is present.
        self.assertEqual(cell["useful_n"], 5)

    def test_sector_rows_do_not_count_toward_account_trigger(self):
        # Account trigger has a strong sample; a pile of sector rows on the same
        # trigger must NOT feed it — they go to reported_separately.
        feedback = self._passing_feedback("leadership_change")
        # Add sector rows (no entity_id, sector scope) that are all not_useful.
        for i in range(50):
            feedback.append(fb(f"sec{i}", "not_useful",
                               trigger_id="leadership_change",
                               signal_scope="sector", entity_id=None,
                               reason_code="other"))
        audit = self._passing_audit("leadership_change")
        out = p.g1_status(feedback, audit,
                          primary_triggers=("leadership_change",), now=self.NOW)
        cell = out["triggers"]["leadership_change"]
        # Account trigger still 25 rated, unaffected by the 50 sector rows.
        self.assertEqual(cell["useful_n"], 25)
        self.assertTrue(cell["meets"])
        # The sector rows appear under reported_separately.
        self.assertEqual(out["reported_separately"]["feedback"]["total"], 50)

    def test_unconfirmed_account_incidents_reported_separately(self):
        # R8.6/R9.4: unconfirmed early-warning cards are account-scoped WITH an
        # entity, but being unverified they must not feed account precision -
        # they belong under reported_separately, like sector rows.
        feedback = self._passing_feedback("leadership_change")
        for i in range(30):
            feedback.append(fb(f"unconf{i}", "not_useful",
                               trigger_id="leadership_change",
                               signal_scope="account", entity_id="e1",
                               incident_evidence_level="unconfirmed_early_warning",
                               reason_code="other"))
        audit = self._passing_audit("leadership_change")
        out = p.g1_status(feedback, audit,
                          primary_triggers=("leadership_change",), now=self.NOW)
        cell = out["triggers"]["leadership_change"]
        # Account trigger still 25 rated, unaffected by the 30 unconfirmed rows.
        self.assertEqual(cell["useful_n"], 25)
        self.assertTrue(cell["meets"])
        self.assertEqual(out["reported_separately"]["feedback"]["total"], 30)

    def test_none_rate_reason_when_no_account_cards(self):
        out = p.g1_status([], [], primary_triggers=("leadership_change",),
                          now=self.NOW)
        cell = out["triggers"]["leadership_change"]
        self.assertIsNone(cell["useful_rate"])
        self.assertIsNone(cell["auto_accuracy"])
        self.assertFalse(cell["meets"])
        self.assertEqual(cell["useful_n"], 0)  # denominator present


class G2StatusTests(unittest.TestCase):
    def test_below_threshold_with_enough_n_recommends_demote(self):
        # 30% precision over n=20 (6 useful, 14 not_useful).
        rows = [fb(f"u{i}", "useful", source_id="bad") for i in range(6)]
        rows += [fb(f"n{i}", "not_useful", source_id="bad",
                    reason_code="weak_evidence") for i in range(14)]
        out = p.g2_status(rows)
        cell = out["bad"]
        self.assertEqual(cell["n"], 20)
        self.assertAlmostEqual(cell["precision"], 0.3)
        self.assertTrue(cell["below_threshold"])
        self.assertTrue(cell["demote_recommended"])
        self.assertEqual(cell["reason_codes"], {"weak_evidence": 14})

    def test_below_threshold_tiny_n_does_not_recommend(self):
        # 0% precision but only n=3 -> sample too small.
        rows = [fb(f"n{i}", "not_useful", source_id="tiny",
                   reason_code="other") for i in range(3)]
        out = p.g2_status(rows)
        cell = out["tiny"]
        self.assertEqual(cell["n"], 3)
        self.assertTrue(cell["below_threshold"])
        self.assertFalse(cell["demote_recommended"])
        self.assertIn("sample too small", cell["note"])

    def test_healthy_source_not_recommended(self):
        rows = [fb(f"u{i}", "useful", source_id="good") for i in range(20)]
        out = p.g2_status(rows)
        cell = out["good"]
        self.assertEqual(cell["precision"], 1.0)
        self.assertFalse(cell["below_threshold"])
        self.assertFalse(cell["demote_recommended"])

    def test_precision_carries_n(self):
        rows = [fb("u1", "useful", source_id="s")]
        cell = p.g2_status(rows)["s"]
        self.assertIn("n", cell)  # trust invariant: rate never bare


if __name__ == "__main__":
    unittest.main()
