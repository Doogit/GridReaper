"""Pure unit tests for precision computation (R9.3, R9.4, R9.5).

Hand-built row lists only — no DB, no network. Asserts the trust invariant
throughout: every rate is returned with its denominator, and an empty
denominator yields ``None`` (never a fake 0.0 pass).
"""
import unittest
from unittest import mock

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


class G1WaiverTests(unittest.TestCase):
    """The R9.4 Gate G1 operator waiver (KTD1).

    A waiver is a DISCLOSURE, never a pass: it adds a ``waived`` state no
    computation can produce, while every underlying number keeps computing with
    its real denominator. With the waiver inactive the pre-existing behavior is
    unchanged, so the waiver is provably additive.
    """
    NOW = "2026-03-01T00:00:00Z"  # ~59 days after the fb/au default ts

    def _rated(self):
        """25 account cards on leadership_change, 20 useful -> 80% over n=25."""
        rows = [fb(f"u{i}", "useful") for i in range(20)]
        return rows + [fb(f"n{i}", "not_useful", reason_code="other")
                       for i in range(5)]

    def _judged(self):
        """10 scored judge checks, 9 pass -> 90% auto-accuracy over n=10."""
        rows = [au(f"a{i}", "entity_match", "pass") for i in range(9)]
        return rows + [au("a9", "entity_match", "fail")]

    def test_waived_state_carries_ruling_date_and_is_not_pass(self):
        out = p.g1_status([], [], primary_triggers=("leadership_change",),
                          now=self.NOW, waiver_active=True)
        self.assertEqual(out["state"], "waived")
        self.assertEqual(out["waiver"]["date"], p.G1_WAIVER_DATE)
        self.assertEqual(out["waiver"]["date"], "2026-08-16")
        # The most trust-loaded string on the most trust-loaded page — pinned
        # verbatim so it can never drift into sounding like a pass.
        self.assertEqual(out["waiver"]["reason"], (
            "Overall: WAIVED by operator ruling 2026-08-16 — not passed. The "
            "gate's own conditions below are unmet and are shown unchanged."))
        # Never coerced to a pass: the computed gate is still not eligible,
        # and the state stays inside its closed three-value vocabulary.
        self.assertIn(out["state"], ("waived", "eligible", "blocked"))
        self.assertFalse(out["eligible"])

    def test_shipped_default_waives_without_an_explicit_argument(self):
        # Every other test in this class passes waiver_active explicitly, so
        # none of them would notice G1_WAIVER_ACTIVE being flipped. This one
        # pins the SHIPPED default — the value production actually runs under,
        # since data.precision_report calls g1_status with no override.
        self.assertTrue(p.G1_WAIVER_ACTIVE)
        out = p.g1_status([], [], primary_triggers=("leadership_change",),
                          now=self.NOW)
        self.assertEqual(out["state"], "waived")

    def test_patching_the_waiver_constant_takes_effect(self):
        # The ruling is read at CALL time, not bound once into the default
        # argument. Its three sibling constants (date / reason / lift condition)
        # are read from module globals in the body, so a G1_WAIVER_ACTIVE bound
        # at import would leave one of the four frozen and three live — and a
        # mock.patch.object of it, this suite's idiom, would silently prove
        # nothing while still passing.
        with mock.patch.object(p, "G1_WAIVER_ACTIVE", False):
            out = p.g1_status([], [], primary_triggers=("leadership_change",),
                              now=self.NOW)
        self.assertEqual(out["state"], "blocked")
        self.assertIsNone(out["waiver"])
        # ...and nothing about the patch is sticky: the shipped ruling governs
        # again the moment the patch is dropped.
        out = p.g1_status([], [], primary_triggers=("leadership_change",),
                          now=self.NOW)
        self.assertEqual(out["state"], "waived")

    def test_explicit_waiver_argument_still_overrides_the_constant(self):
        # Late binding must not cost the caller its override: an explicit
        # argument wins over the module constant in BOTH directions.
        with mock.patch.object(p, "G1_WAIVER_ACTIVE", False):
            out = p.g1_status([], [], primary_triggers=("leadership_change",),
                              now=self.NOW, waiver_active=True)
        self.assertEqual(out["state"], "waived")
        with mock.patch.object(p, "G1_WAIVER_ACTIVE", True):
            out = p.g1_status([], [], primary_triggers=("leadership_change",),
                              now=self.NOW, waiver_active=False)
        self.assertEqual(out["state"], "blocked")

    def test_waiver_records_lift_condition(self):
        # The condition that ENDS the waiver must be readable off the state, so
        # a reader can see what would bring the gate back.
        out = p.g1_status([], [], primary_triggers=("leadership_change",),
                          now=self.NOW, waiver_active=True)
        lift = out["waiver"]["lift_condition"]
        self.assertEqual(lift, p.G1_WAIVER_LIFT_CONDITION)
        self.assertIn(str(p.G1_MIN_RATED), lift)
        self.assertIn("rated", lift)

    def test_waiver_is_recorded_not_enforced(self):
        # The lift condition is RECORDED, not ENFORCED (Open Question 7): a
        # trigger that has since reached the rated-card floor does NOT make
        # g1_status fall back to the computed state on its own.
        out = p.g1_status(self._rated(), self._judged(),
                          primary_triggers=("leadership_change",),
                          now=self.NOW, waiver_active=True)
        self.assertGreaterEqual(out["triggers"]["leadership_change"]["useful_n"],
                                p.G1_MIN_RATED)
        self.assertEqual(out["state"], "waived")

    def test_underlying_numbers_keep_their_real_sample_sizes(self):
        # The computation keeps running underneath the waiver: real rates, real
        # denominators — nothing suppressed, nothing fabricated.
        out = p.g1_status(self._rated(), self._judged(),
                          primary_triggers=("leadership_change",),
                          now=self.NOW, waiver_active=True)
        cell = out["triggers"]["leadership_change"]
        self.assertEqual(cell["useful_rate"], 0.8)
        self.assertEqual(cell["useful_n"], 25)
        self.assertEqual(cell["auto_accuracy"], 0.9)
        self.assertEqual(cell["auto_n"], 10)

    def test_waiver_does_not_hide_blocked_reasons(self):
        # The gate's unmet conditions stay visible under the waiver.
        out = p.g1_status([], [], primary_triggers=("leadership_change",),
                          now=self.NOW, waiver_active=True)
        cell = out["triggers"]["leadership_change"]
        self.assertFalse(cell["meets"])
        self.assertTrue(any("n<20" in r for r in cell["reasons"]))
        self.assertTrue(out["blocked_reasons"])

    def test_inactive_waiver_is_byte_identical_to_prior_behavior(self):
        # Characterization test: with the waiver inactive the whole returned
        # structure equals the pre-waiver output, plus the two additive keys
        # reporting the un-waived computed state.
        out = p.g1_status([], [], primary_triggers=("leadership_change",),
                          now=self.NOW, waiver_active=False)
        self.assertEqual(out, {
            "triggers": {"leadership_change": {
                "useful_rate": None,
                "useful_n": 0,
                "auto_accuracy": None,
                "auto_n": 0,
                "days_span": 0,
                "meets": False,
                "reasons": [
                    "n<20 rated account cards (have 0)",
                    "span<30d (have 0.0d)",
                    "no rated account cards for useful-rate",
                    "no scored judge checks for auto-accuracy",
                ],
            }},
            "reported_separately": {
                "feedback": {"useful": 0, "total": 0, "rate": None},
                "auto": {"correct": 0, "scored": 0, "accuracy": None},
            },
            "eligible": False,
            "blocked_reasons": [
                "leadership_change: n<20 rated account cards (have 0)",
                "leadership_change: span<30d (have 0.0d)",
                "leadership_change: no rated account cards for useful-rate",
                "leadership_change: no scored judge checks for auto-accuracy",
            ],
            "state": "blocked",
            "waiver": None,
        })

    def test_waived_state_unreachable_by_computation(self):
        # No data can compute a "waived" state — only the recorded waiver.
        for rows in ([[], []], [self._rated(), self._judged()]):
            out = p.g1_status(rows[0], rows[1],
                              primary_triggers=("leadership_change",),
                              now=self.NOW, waiver_active=False)
            self.assertIn(out["state"], ("eligible", "blocked"))
            self.assertIsNone(out["waiver"])

    def test_g2_and_disagreement_gate_unaffected(self):
        # G2 (R9.5) and the R9.11 disagreement overlay carry no waiver: G1's
        # waiver is G1's alone.
        rows = [fb(f"u{i}", "useful", source_id="good") for i in range(20)]
        g2 = p.g2_status(rows)
        self.assertEqual(set(g2["good"]), {
            "precision", "n", "reason_codes", "below_threshold",
            "demote_recommended", "note"})
        gated = p.g2_gated(g2, p.judge_human_disagreement_by_source([], rows))
        self.assertEqual(gated["good"]["gate"], "na")
        self.assertNotIn("waiver", gated["good"])
        self.assertNotIn("state", gated["good"])


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


class DisagreementBySourceTests(unittest.TestCase):
    def test_buckets_by_audit_source(self):
        # s1 (src_a): judge positive, human negative -> disagree.
        # s2 (src_b): judge negative, human negative -> agree.
        audit = [
            au("s1", "entity_match", "pass", source_id="src_a"),
            au("s2", "entity_match", "fail", source_id="src_b"),
        ]
        feedback = [
            fb("s1", "not_useful", reason_code="other", source_id="src_a"),
            fb("s2", "not_useful", reason_code="other", source_id="src_b"),
        ]
        out = p.judge_human_disagreement_by_source(audit, feedback)
        self.assertEqual(out["src_a"]["disagreement_rate"], 1.0)
        self.assertEqual(out["src_a"]["comparable"], 1)
        self.assertEqual(out["src_b"]["disagreement_rate"], 0.0)

    def test_global_signature_unchanged(self):
        # The old global function still returns its original shape.
        out = p.judge_human_disagreement([au("s1", "entity_match", "fail")],
                                         [fb("s1", "not_useful",
                                             reason_code="other")])
        self.assertEqual(out["comparable"], 1)
        self.assertIn("items", out)


class G2GatedTests(unittest.TestCase):
    def _g2(self, source, useful, not_useful):
        rows = [fb(f"{source}:u{i}", "useful", source_id=source)
                for i in range(useful)]
        rows += [fb(f"{source}:n{i}", "not_useful", source_id=source,
                    reason_code="weak_evidence") for i in range(not_useful)]
        return p.g2_status(rows), rows

    def test_high_disagreement_over_floor_withholds_demote(self):
        # A source that WOULD be demoted (low precision, big n)...
        g2, _ = self._g2("bad", 4, 26)          # ~13% precision, n=30
        self.assertTrue(g2["bad"]["demote_recommended"])
        # ...with 30% disagreement over >= floor comparable is gated off.
        dis = {"bad": {"comparable": p.G2_MIN_N, "agree": 14, "disagree": 6,
                       "disagreement_rate": 0.30}}
        gated = p.g2_gated(g2, dis)
        self.assertEqual(gated["bad"]["gate"], "withheld")
        self.assertFalse(gated["bad"]["demote_recommended"])
        self.assertIn("judge verdicts withheld from demotion",
                      gated["bad"]["note"])
        # Base result untouched (overlay is non-mutating).
        self.assertTrue(g2["bad"]["demote_recommended"])

    def test_high_disagreement_without_demote_recommendation_stays_ok(self):
        # The disagreement gate withholds demotion guidance; it must not invent
        # a withheld demotion state for a source whose base G2 is healthy.
        g2, _ = self._g2("good", 26, 4)
        self.assertFalse(g2["good"]["demote_recommended"])
        dis = {"good": {"comparable": p.G2_MIN_N, "agree": 14, "disagree": 6,
                        "disagreement_rate": 0.30}}
        gated = p.g2_gated(g2, dis)
        self.assertEqual(gated["good"]["gate"], "ok")
        self.assertFalse(gated["good"]["demote_recommended"])
        self.assertNotIn("<40%", gated["good"]["note"])

    def test_low_disagreement_leaves_g2_unchanged(self):
        g2, _ = self._g2("bad", 4, 26)
        dis = {"bad": {"comparable": p.G2_MIN_N, "agree": 18, "disagree": 2,
                       "disagreement_rate": 0.10}}
        gated = p.g2_gated(g2, dis)
        self.assertEqual(gated["bad"]["gate"], "ok")
        self.assertTrue(gated["bad"]["demote_recommended"])

    def test_high_disagreement_below_floor_never_blocks(self):
        g2, _ = self._g2("bad", 4, 26)
        dis = {"bad": {"comparable": 3, "agree": 2, "disagree": 1,
                       "disagreement_rate": 0.30}}
        gated = p.g2_gated(g2, dis)
        self.assertEqual(gated["bad"]["gate"], "below_floor")
        # No false block: the recommendation stands.
        self.assertTrue(gated["bad"]["demote_recommended"])

    def test_no_comparable_reads_na(self):
        g2, _ = self._g2("bad", 4, 26)
        gated = p.g2_gated(g2, {})   # no disagreement data for any source
        self.assertEqual(gated["bad"]["gate"], "na")
        self.assertEqual(gated["bad"]["comparable"], 0)
        self.assertIsNone(gated["bad"]["disagreement_rate"])
        self.assertTrue(gated["bad"]["demote_recommended"])  # unchanged


class SpotcheckCoverageTests(unittest.TestCase):
    NOW = "2026-08-15T00:00:00Z"

    def test_met_when_reviewed_at_or_above_target(self):
        # 5 signals audited this month, all human-reviewed -> reviewed=5.
        audit, feedback = [], []
        for i in range(5):
            audit.append(au(f"s{i}", "entity_match", "pass",
                            ts="2026-08-10T00:00:00Z"))
            feedback.append(fb(f"s{i}", "useful",
                               ts="2026-08-11T00:00:00Z"))
        sc = p.spotcheck_coverage(audit, feedback, now=self.NOW)
        self.assertEqual(sc["reviewed"], 5)
        self.assertEqual(sc["audited"], 5)
        self.assertEqual(sc["target"], 5)   # floor
        self.assertTrue(sc["met"])
        self.assertEqual(sc["window"], "2026-08")

    def test_below_floor_when_too_few_reviewed(self):
        # 5 audited, only 2 reviewed -> below the floor of 5.
        audit, feedback = [], []
        for i in range(5):
            audit.append(au(f"s{i}", "entity_match", "pass",
                            ts="2026-08-10T00:00:00Z"))
        feedback = [
            fb("s0", "useful", ts="2026-08-11T00:00:00Z"),
            fb("s1", "not_useful", reason_code="other",
               ts="2026-08-11T00:00:00Z"),
        ]
        sc = p.spotcheck_coverage(audit, feedback, now=self.NOW)
        self.assertEqual(sc["reviewed"], 2)
        self.assertFalse(sc["met"])

    def test_prior_month_audit_excluded(self):
        audit = [au("s1", "entity_match", "pass", ts="2026-07-31T00:00:00Z")]
        feedback = [fb("s1", "useful")]
        sc = p.spotcheck_coverage(audit, feedback, now=self.NOW)
        self.assertEqual(sc["audited"], 0)
        self.assertEqual(sc["reviewed"], 0)

    def test_prior_month_feedback_does_not_count_current_month_review(self):
        audit = [au("s1", "entity_match", "pass", ts="2026-08-10T00:00:00Z")]
        feedback = [fb("s1", "useful", ts="2026-07-31T00:00:00Z")]
        sc = p.spotcheck_coverage(audit, feedback, now=self.NOW)
        self.assertEqual(sc["audited"], 1)
        self.assertEqual(sc["reviewed"], 0)
        self.assertFalse(sc["met"])


class LateBoundConstantDefaultsTests(unittest.TestCase):
    """U25: nine more module-constant defaults were bound at import time,
    the same bug U17 already fixed for ``waiver_active``. Each function must
    resolve its constant from the live module global at CALL time, so a
    ``mock.patch.object`` of the constant is honored by a call that omits the
    corresponding argument."""

    NOW = "2026-03-01T00:00:00Z"  # ~59 days after the fb/au default ts

    def _rated_feedback(self):
        rows = [fb(f"u{i}", "useful", entity_id="e1", signal_scope="account")
                for i in range(20)]
        rows += [fb(f"n{i}", "not_useful", entity_id="e1",
                    signal_scope="account", reason_code="other")
                 for i in range(5)]
        return rows

    def _rated_audit(self):
        rows = [au(f"a{i}", "entity_match", "pass", entity_id="e1",
                   signal_scope="account") for i in range(9)]
        rows.append(au("a9", "entity_match", "fail", entity_id="e1",
                       signal_scope="account"))
        return rows

    def test_g1_min_days_patch_is_honored(self):
        feedback, audit = self._rated_feedback(), self._rated_audit()
        out = p.g1_status(feedback, audit,
                          primary_triggers=("leadership_change",),
                          now=self.NOW, waiver_active=False)
        self.assertTrue(out["triggers"]["leadership_change"]["meets"])
        with mock.patch.object(p, "G1_MIN_DAYS", 9999):
            out = p.g1_status(feedback, audit,
                              primary_triggers=("leadership_change",),
                              now=self.NOW, waiver_active=False)
        cell = out["triggers"]["leadership_change"]
        self.assertFalse(cell["meets"])
        self.assertTrue(any("span<9999d" in r for r in cell["reasons"]))

    def test_g1_min_rated_patch_is_honored(self):
        feedback, audit = self._rated_feedback(), self._rated_audit()
        with mock.patch.object(p, "G1_MIN_RATED", 9999):
            out = p.g1_status(feedback, audit,
                              primary_triggers=("leadership_change",),
                              now=self.NOW, waiver_active=False)
        cell = out["triggers"]["leadership_change"]
        self.assertFalse(cell["meets"])
        self.assertTrue(any("n<9999" in r for r in cell["reasons"]))

    def test_g2_threshold_patch_is_honored(self):
        # 75% precision over n=20 — above the default 40% floor.
        rows = [fb(f"u{i}", "useful", source_id="s") for i in range(15)]
        rows += [fb(f"n{i}", "not_useful", source_id="s", reason_code="other")
                 for i in range(5)]
        out = p.g2_status(rows)
        self.assertFalse(out["s"]["below_threshold"])
        with mock.patch.object(p, "G2_THRESHOLD", 0.99):
            out = p.g2_status(rows)
        self.assertTrue(out["s"]["below_threshold"])
        self.assertTrue(out["s"]["demote_recommended"])

    def test_g2_min_n_patch_is_honored(self):
        # 0% precision over n=20 — enough sample to recommend demotion by
        # default.
        rows = [fb(f"n{i}", "not_useful", source_id="s", reason_code="other")
                for i in range(20)]
        out = p.g2_status(rows)
        self.assertTrue(out["s"]["demote_recommended"])
        with mock.patch.object(p, "G2_MIN_N", 9999):
            out = p.g2_status(rows)
        self.assertFalse(out["s"]["demote_recommended"])
        self.assertIn("sample too small", out["s"]["note"])

    def _many_reviewed(self, n):
        audit = [au(f"s{i}", "entity_match", "pass",
                    ts="2026-08-10T00:00:00Z") for i in range(n)]
        feedback = [fb(f"s{i}", "useful", ts="2026-08-11T00:00:00Z")
                    for i in range(n)]
        return audit, feedback

    def test_spotcheck_abs_target_patch_is_honored(self):
        # audited=100 -> frac_target=20, so the default abs_target=10 is the
        # binding cap; raising it changes the target.
        audit, feedback = self._many_reviewed(100)
        sc = p.spotcheck_coverage(audit, feedback, now="2026-08-15T00:00:00Z")
        self.assertEqual(sc["target"], 10)
        with mock.patch.object(p, "SPOTCHECK_ABS_TARGET", 15):
            sc = p.spotcheck_coverage(
                audit, feedback, now="2026-08-15T00:00:00Z")
        self.assertEqual(sc["target"], 15)

    def test_spotcheck_fraction_patch_is_honored(self):
        # audited=30 -> frac_target=6 is the binding cap under abs_target=10;
        # raising the fraction pushes frac_target past abs_target.
        audit, feedback = self._many_reviewed(30)
        sc = p.spotcheck_coverage(audit, feedback, now="2026-08-15T00:00:00Z")
        self.assertEqual(sc["target"], 6)
        with mock.patch.object(p, "SPOTCHECK_FRACTION", 0.5):
            sc = p.spotcheck_coverage(
                audit, feedback, now="2026-08-15T00:00:00Z")
        self.assertEqual(sc["target"], 10)

    def test_spotcheck_min_floor_patch_is_honored(self):
        # audited=1 -> the hard floor of 5 dominates; raising it changes the
        # target even though abs_target/fraction are unaffected.
        audit, feedback = self._many_reviewed(1)
        sc = p.spotcheck_coverage(audit, feedback, now="2026-08-15T00:00:00Z")
        self.assertEqual(sc["target"], 5)
        with mock.patch.object(p, "SPOTCHECK_MIN_FLOOR", 50):
            sc = p.spotcheck_coverage(
                audit, feedback, now="2026-08-15T00:00:00Z")
        self.assertEqual(sc["target"], 50)

    def _withheld_scenario(self, comparable, disagreement_rate):
        g2 = {"bad": {"precision": 0.1, "n": 30, "reason_codes": {},
                      "below_threshold": True, "demote_recommended": True,
                      "note": "n/a"}}
        dis = {"bad": {"comparable": comparable,
                       "agree": comparable - round(comparable
                                                    * disagreement_rate),
                       "disagree": round(comparable * disagreement_rate),
                       "disagreement_rate": disagreement_rate}}
        return g2, dis

    def test_disagreement_gate_threshold_patch_is_honored(self):
        g2, dis = self._withheld_scenario(p.G2_MIN_N, 0.25)
        gated = p.g2_gated(g2, dis)
        self.assertEqual(gated["bad"]["gate"], "withheld")
        with mock.patch.object(p, "DISAGREEMENT_GATE_THRESHOLD", 0.99):
            gated = p.g2_gated(g2, dis)
        self.assertEqual(gated["bad"]["gate"], "ok")
        self.assertTrue(gated["bad"]["demote_recommended"])

    def test_disagreement_min_comparable_patch_is_honored(self):
        g2, dis = self._withheld_scenario(10, 0.30)
        gated = p.g2_gated(g2, dis)
        self.assertEqual(gated["bad"]["gate"], "below_floor")
        with mock.patch.object(p, "DISAGREEMENT_MIN_COMPARABLE", 5):
            gated = p.g2_gated(g2, dis)
        self.assertEqual(gated["bad"]["gate"], "withheld")


if __name__ == "__main__":
    unittest.main()
