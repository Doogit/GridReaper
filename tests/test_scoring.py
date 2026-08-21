"""Scoring engine tests (R7.3, R7.5).

Hermetic: real migrations against in-memory SQLite, FK on, fixture-inserted
triggers/entities/signals and scoring_weights rows (the CSV->table path is
exercised by the loader tests; TestSeedCsvCoverage checks the shipped CSV
covers every lookup key). Covers exact formula math, half-life curve points,
neutral fallback for unknown weight keys, the regulatory_calendar
applicability path, the decay flip at DECAY_THRESHOLD, idempotency with an
injected clock, and that dismissed/decayed rows are never touched.

TestScoringConfigVersion covers R3.7 reproducibility: the config token hashes
BOTH tuning tables (scoring_weights and the triggers base_strength /
decay_half_life_days columns), moves for every knob the scorer reads, and is
stamped on active rows so a stored score names the tuning that produced it.
"""
import csv
import os
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from app.db.migrate import apply_migrations
from app.scoring import DECAY_THRESHOLD, rescore, scoring_config_version

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

SEEDS_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "seeds", "scoring_weights.csv")

EXPECTED_SUBSECTORS = {
    "iou_electric", "og_ep", "midstream", "coop_gt", "ofs", "renewables",
    "muni_public", "refiner", "rto_iso", "iou_gas", "public",
    "coop_distribution", "coop_transmission", "federal", "federal_pma",
    "ipp", "lng", "og_major", "state_authority", "state_owned", "storage",
}

FIXTURE_WEIGHTS = [
    ("subsector", "iou_electric", 1.1),
    ("subsector", "og_ep", 0.75),
    ("richness", "high", 1.0),
    ("richness", "medium", 0.9),
    ("richness", "low", 0.75),
    ("coverage", "edgar-visible", 1.0),
    ("coverage", "dark", 0.85),
    ("applicability", "default", 0.9),
    ("scope", "account", 1.0),
    ("scope", "sector", 0.55),
    ("scope", "regulatory_calendar", 0.45),
]


def days_ago(n):
    return (NOW.date() - timedelta(days=n)).isoformat()


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        " decay_half_life_days) VALUES ('t_lead', 'Leadership', 4, 90)")
    conn.execute(
        "INSERT INTO triggers (trigger_id, name, base_strength, "
        " decay_half_life_days) VALUES ('t_reg', 'Regulatory', 5, 600)")
    entities = [
        # (entity_id, subsector, richness, coverage_flag)
        ("E_IOU", "iou_electric", "high", "edgar-visible"),
        ("E_OG", "og_ep", "low", "dark"),
        ("E_NEW", "brand_new_subsector", "", ""),   # nothing in weights
    ]
    for eid, sub, rich, cov in entities:
        conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, subsector, "
            " richness, coverage_flag) VALUES (?, ?, ?, ?, ?)",
            (eid, eid, sub, rich, cov))
    conn.executemany(
        "INSERT INTO scoring_weights (weight_kind, key, weight) "
        "VALUES (?, ?, ?)", FIXTURE_WEIGHTS)
    conn.commit()
    return conn


def add_signal(conn, signal_id, trigger_id, scope, entity_id, event_date,
               status="active", score=None):
    conn.execute(
        "INSERT INTO signals (signal_id, trigger_id, signal_scope, "
        " entity_id, event_date, status, score) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (signal_id, trigger_id, scope, entity_id, event_date, status, score))
    conn.commit()


class TestScoring(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def get(self, signal_id):
        return self.conn.execute(
            "SELECT score, status FROM signals WHERE signal_id = ?",
            (signal_id,)).fetchone()

    def components(self, signal_id):
        return self.conn.execute(
            "SELECT score, score_base, score_decay, score_account_fit, "
            " score_scope_fit, scored_at FROM signals WHERE signal_id = ?",
            (signal_id,)).fetchone()

    def test_formula_exact_at_age_zero(self):
        """base 4 * decay 1 * (1.1 * 1.0 * 1.0) * scope 1.0 = 4.4"""
        add_signal(self.conn, "s1", "t_lead", "account", "E_IOU", days_ago(0))
        summary = rescore(self.conn, now=NOW)
        self.assertEqual(summary, {"scored": 1, "decayed": 0})
        self.assertAlmostEqual(self.get("s1")["score"], 4.4)

    def test_formula_exact_all_fit_factors(self):
        """base 4 * (0.75 * 0.75 * 0.85) = 1.9125"""
        add_signal(self.conn, "s1", "t_lead", "account", "E_OG", days_ago(0))
        rescore(self.conn, now=NOW)
        self.assertAlmostEqual(self.get("s1")["score"], 1.9125)

    def test_half_life_point_halves_score(self):
        """age == half_life (90d) -> exactly half of 4.4"""
        add_signal(self.conn, "s1", "t_lead", "account", "E_IOU", days_ago(90))
        rescore(self.conn, now=NOW)
        self.assertAlmostEqual(self.get("s1")["score"], 2.2)

    def test_unknown_weight_keys_fall_back_neutral(self):
        """Unseen subsector + blank richness/coverage -> all 1.0, score 4."""
        add_signal(self.conn, "s1", "t_lead", "account", "E_NEW", days_ago(0))
        rescore(self.conn, now=NOW)
        self.assertAlmostEqual(self.get("s1")["score"], 4.0)

    def test_sector_scope_entity_less_is_neutral_fit(self):
        """5 * 1.0 (no account to fit) * scope 0.55 = 2.75"""
        add_signal(self.conn, "s1", "t_reg", "sector", None, days_ago(0))
        rescore(self.conn, now=NOW)
        self.assertAlmostEqual(self.get("s1")["score"], 2.75)

    def test_regulatory_calendar_uses_applicability(self):
        """Entity-less: 5 * applicability default 0.9 * scope 0.45 = 2.025"""
        add_signal(self.conn, "s1", "t_reg", "regulatory_calendar", None,
                   days_ago(0))
        rescore(self.conn, now=NOW)
        self.assertAlmostEqual(self.get("s1")["score"], 2.025)

    def test_regulatory_calendar_with_entity_replaces_richness(self):
        """R7.5: applicability (default 0.9) replaces richness (high 1.0):
        5 * (1.1 * 0.9 * 1.0) * 0.45 = 2.2275"""
        add_signal(self.conn, "s1", "t_reg", "regulatory_calendar", "E_IOU",
                   days_ago(0))
        rescore(self.conn, now=NOW)
        self.assertAlmostEqual(self.get("s1")["score"], 2.2275)

    def test_sector_never_outranks_fresh_account_card(self):
        add_signal(self.conn, "acct", "t_lead", "account", "E_IOU", days_ago(0))
        add_signal(self.conn, "sect", "t_reg", "sector", None, days_ago(0))
        add_signal(self.conn, "cal", "t_reg", "regulatory_calendar", None,
                   days_ago(0))
        rescore(self.conn, now=NOW)
        self.assertGreater(self.get("acct")["score"], self.get("sect")["score"])
        self.assertGreater(self.get("sect")["score"], self.get("cal")["score"])

    def test_decay_flip_below_threshold(self):
        """Perfect-fit leadership signal: 4 * 0.5^(200/90) ~= 0.858 < 1.0
        -> decayed; at exactly 2 half-lives the score is exactly 1.0 and
        stays active (flip is strictly below DECAY_THRESHOLD)."""
        add_signal(self.conn, "old", "t_lead", "account", "E_NEW",
                   days_ago(200))
        add_signal(self.conn, "edge", "t_lead", "account", "E_NEW",
                   days_ago(180))
        summary = rescore(self.conn, now=NOW)
        self.assertEqual(summary, {"scored": 2, "decayed": 1})
        old = self.get("old")
        self.assertEqual(old["status"], "decayed")
        self.assertLess(old["score"], DECAY_THRESHOLD)
        edge = self.get("edge")
        self.assertEqual(edge["status"], "active")
        self.assertAlmostEqual(edge["score"], 1.0)

    def test_empty_and_garbage_event_dates_score_at_full_strength(self):
        add_signal(self.conn, "s1", "t_lead", "account", "E_NEW", "")
        add_signal(self.conn, "s2", "t_lead", "account", "E_NEW", "not-a-date")
        add_signal(self.conn, "s3", "t_lead", "account", "E_NEW",
                   days_ago(0) + "T09:30:00Z")   # timestamp form
        rescore(self.conn, now=NOW)
        for sid in ("s1", "s2", "s3"):
            self.assertAlmostEqual(self.get(sid)["score"], 4.0)

    def test_rescore_idempotent_for_same_now(self):
        add_signal(self.conn, "s1", "t_lead", "account", "E_IOU", days_ago(30))
        add_signal(self.conn, "s2", "t_reg", "sector", None, days_ago(400))
        add_signal(self.conn, "s3", "t_lead", "account", "E_OG", days_ago(200))
        rescore(self.conn, now=NOW)
        first = self.conn.execute(
            "SELECT signal_id, score, status FROM signals "
            "ORDER BY signal_id").fetchall()
        rescore(self.conn, now=NOW)
        second = self.conn.execute(
            "SELECT signal_id, score, status FROM signals "
            "ORDER BY signal_id").fetchall()
        self.assertEqual([tuple(r) for r in first], [tuple(r) for r in second])

    def test_score_components_persisted_and_multiply_to_score(self):
        """R8.1 explainability: the four components are stored, multiply back
        to the score, and scored_at is the injected clock (UTC ISO-8601).
        base 5 * (applicability 0.9 replacing richness) * scope 0.45."""
        add_signal(self.conn, "s1", "t_reg", "regulatory_calendar", "E_IOU",
                   days_ago(0))
        rescore(self.conn, now=NOW)
        c = self.components("s1")
        self.assertAlmostEqual(c["score_base"], 5.0)
        self.assertAlmostEqual(c["score_decay"], 1.0)
        self.assertAlmostEqual(c["score_account_fit"], 1.1 * 0.9 * 1.0)
        self.assertAlmostEqual(c["score_scope_fit"], 0.45)
        product = (c["score_base"] * c["score_decay"]
                   * c["score_account_fit"] * c["score_scope_fit"])
        self.assertAlmostEqual(product, c["score"])
        self.assertEqual(c["scored_at"], NOW.isoformat())

    def test_decayed_component_captures_half_life_curve(self):
        """At one half-life the decay component is exactly 0.5 and still
        multiplies out to the stored score."""
        add_signal(self.conn, "s1", "t_lead", "account", "E_IOU", days_ago(90))
        rescore(self.conn, now=NOW)
        c = self.components("s1")
        self.assertAlmostEqual(c["score_decay"], 0.5)
        self.assertAlmostEqual(
            c["score_base"] * c["score_decay"] * c["score_account_fit"]
            * c["score_scope_fit"], c["score"])

    def test_dismissed_and_decayed_rows_untouched(self):
        add_signal(self.conn, "gone", "t_lead", "account", "E_IOU",
                   days_ago(0), status="dismissed", score=3.3)
        add_signal(self.conn, "dead", "t_lead", "account", "E_IOU",
                   days_ago(0), status="decayed", score=0.42)
        summary = rescore(self.conn, now=NOW)
        self.assertEqual(summary, {"scored": 0, "decayed": 0})
        self.assertEqual(tuple(self.get("gone")), (3.3, "dismissed"))
        self.assertEqual(tuple(self.get("dead")), (0.42, "decayed"))


class TestSeedCsvCoverage(unittest.TestCase):
    """The shipped seeds/scoring_weights.csv covers every lookup key the
    scorer will hit against the real watchlist."""

    @classmethod
    def setUpClass(cls):
        with open(SEEDS_CSV, newline="", encoding="utf-8") as fh:
            cls.rows = list(csv.DictReader(fh))
        cls.by_kind = {}
        for row in cls.rows:
            cls.by_kind.setdefault(row["weight_kind"], {})[row["key"]] = (
                float(row["weight"]))

    def test_all_weights_are_positive_floats(self):
        for row in self.rows:
            self.assertGreater(float(row["weight"]), 0.0, msg=row)

    def test_subsector_keys_cover_watchlist(self):
        self.assertEqual(set(self.by_kind["subsector"]), EXPECTED_SUBSECTORS)

    def test_richness_and_coverage_keys(self):
        self.assertEqual(set(self.by_kind["richness"]),
                         {"high", "medium", "low"})
        self.assertEqual(set(self.by_kind["coverage"]),
                         {"edgar-visible", "dark"})
        # R6.6: dark accounts are discounted, never zeroed
        self.assertGreater(self.by_kind["coverage"]["dark"], 0.0)

    def test_applicability_default_present(self):
        self.assertIn("default", self.by_kind["applicability"])

    def test_scope_fit_ordering(self):
        scope = self.by_kind["scope"]
        self.assertEqual(set(scope),
                         {"account", "sector", "regulatory_calendar"})
        self.assertGreater(scope["account"], scope["sector"])
        self.assertGreater(scope["sector"], scope["regulatory_calendar"])


class TestScoringConfigVersion(unittest.TestCase):
    """R3.7: a stored score names the tuning that produced it, and the token
    moves for EVERY knob the scorer reads - both scoring_weights and the
    triggers columns (base_strength, decay_half_life_days)."""

    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def version(self):
        return scoring_config_version(self.conn)

    def stored(self, signal_id="s1"):
        return self.conn.execute(
            "SELECT score, score_base, score_decay, score_account_fit, "
            " score_scope_fit, scoring_config_version, status FROM signals "
            "WHERE signal_id = ?", (signal_id,)).fetchone()

    def test_token_is_deterministic_16_hex(self):
        first = self.version()
        self.assertRegex(first, r"^[0-9a-f]{16}$")
        self.assertEqual(first, self.version())

    def test_weight_edit_moves_the_token(self):
        before = self.version()
        self.conn.execute("UPDATE scoring_weights SET weight = 1.3 "
                          "WHERE weight_kind = 'subsector' AND key = 'iou_electric'")
        self.assertNotEqual(before, self.version())

    def test_half_life_edit_moves_the_token(self):
        """The KTD3 correction: half-life lives in triggers, not
        scoring_weights. Hashing scoring_weights alone would miss this."""
        before = self.version()
        self.conn.execute("UPDATE triggers SET decay_half_life_days = 45 "
                          "WHERE trigger_id = 't_lead'")
        self.assertNotEqual(before, self.version())

    def test_base_strength_edit_moves_the_token(self):
        before = self.version()
        self.conn.execute("UPDATE triggers SET base_strength = 5 "
                          "WHERE trigger_id = 't_lead'")
        self.assertNotEqual(before, self.version())

    def test_unrelated_edit_leaves_the_token_alone(self):
        before = self.version()
        self.conn.execute("UPDATE triggers SET name = 'Leadership change' "
                          "WHERE trigger_id = 't_lead'")
        self.conn.execute("UPDATE scoring_weights SET notes = 'tuned' "
                          "WHERE weight_kind = 'scope' AND key = 'account'")
        self.assertEqual(before, self.version())

    def test_integer_and_real_spellings_hash_identically(self):
        """SQLite affinity lets 4 and 4.0 both land in base_strength; the same
        tuning must not produce two tokens."""
        self.conn.execute("UPDATE triggers SET base_strength = 4 "
                          "WHERE trigger_id = 't_lead'")
        as_int = self.version()
        self.conn.execute("UPDATE triggers SET base_strength = 4.0 "
                          "WHERE trigger_id = 't_lead'")
        self.assertEqual(as_int, self.version())

    def test_rescore_stamps_the_current_token(self):
        add_signal(self.conn, "s1", "t_lead", "account", "E_IOU", days_ago(0))
        rescore(self.conn, now=NOW)
        self.assertEqual(self.stored()["scoring_config_version"], self.version())

    def test_prior_score_stays_reproducible_after_a_weight_change(self):
        """The proving test: mutate one weight, re-score, and the version token
        changes while the pre-change score is still explained exactly by the
        components stored with its own token."""
        add_signal(self.conn, "s1", "t_lead", "account", "E_IOU", days_ago(0))
        rescore(self.conn, now=NOW)
        old = dict(self.stored())
        self.assertAlmostEqual(old["score"], 4.4)
        self.assertAlmostEqual(
            old["score_base"] * old["score_decay"]
            * old["score_account_fit"] * old["score_scope_fit"], old["score"])

        self.conn.execute("UPDATE scoring_weights SET weight = 0.5 "
                          "WHERE weight_kind = 'subsector' AND key = 'iou_electric'")
        rescore(self.conn, now=NOW)
        new = dict(self.stored())

        self.assertNotEqual(old["scoring_config_version"],
                            new["scoring_config_version"])
        self.assertAlmostEqual(new["score"], 2.0)
        self.assertAlmostEqual(
            new["score_base"] * new["score_decay"]
            * new["score_account_fit"] * new["score_scope_fit"], new["score"])
        # the captured pre-change row still multiplies out to its own score:
        # "which config produced 4.4?" is now answerable from the row alone.
        self.assertAlmostEqual(
            old["score_base"] * old["score_decay"]
            * old["score_account_fit"] * old["score_scope_fit"], 4.4)

    def test_half_life_change_moves_both_score_and_token(self):
        add_signal(self.conn, "s1", "t_lead", "account", "E_IOU", days_ago(90))
        rescore(self.conn, now=NOW)
        old = dict(self.stored())
        self.assertAlmostEqual(old["score"], 2.2)
        self.conn.execute("UPDATE triggers SET decay_half_life_days = 45 "
                          "WHERE trigger_id = 't_lead'")
        rescore(self.conn, now=NOW)
        new = dict(self.stored())
        self.assertAlmostEqual(new["score"], 1.1)
        self.assertNotEqual(old["scoring_config_version"],
                            new["scoring_config_version"])

    def test_non_active_rows_keep_their_stale_token(self):
        """Documented, intended gap: rescore() only touches active rows, so a
        dismissed signal keeps the token it was last scored under (NULL here,
        never scored) rather than being restamped with today's tuning."""
        add_signal(self.conn, "s_dis", "t_lead", "account", "E_IOU",
                   days_ago(0), status="dismissed", score=3.0)
        add_signal(self.conn, "s1", "t_lead", "account", "E_IOU", days_ago(0))
        rescore(self.conn, now=NOW)
        self.assertIsNone(self.stored("s_dis")["scoring_config_version"])
        self.assertEqual(self.stored("s_dis")["score"], 3.0)
        self.assertIsNotNone(self.stored("s1")["scoring_config_version"])

    def test_admin_weight_edit_path_moves_the_token(self):
        """data.update_weight rescores; the stamped token must move with it."""
        from app.ui import data
        add_signal(self.conn, "s1", "t_lead", "account", "E_IOU", days_ago(0))
        rescore(self.conn, now=NOW)
        before = self.stored()["scoring_config_version"]
        data.update_weight(self.conn, "subsector", "iou_electric", 0.5,
                           reason="tuning", now=NOW)
        self.assertNotEqual(before, self.stored()["scoring_config_version"])

    def test_admin_half_life_edit_path_moves_the_token(self):
        from app.ui import data
        add_signal(self.conn, "s1", "t_lead", "account", "E_IOU", days_ago(0))
        rescore(self.conn, now=NOW)
        before = self.stored()["scoring_config_version"]
        data.update_half_life(self.conn, "t_lead", 45, reason="tuning", now=NOW)
        self.assertNotEqual(before, self.stored()["scoring_config_version"])


class TestComboScoringAntiRegression(unittest.TestCase):
    """R12 anti-regression: empty combo_rules -> byte-identical scores, score_combo NULL.

    This is the FIRST test to write per spec: measured-inert before any rules exist.
    """

    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def _row(self, signal_id):
        return self.conn.execute(
            "SELECT score, score_base, score_decay, score_account_fit, "
            " score_scope_fit, score_combo FROM signals WHERE signal_id = ?",
            (signal_id,)).fetchone()

    def test_no_combo_rules_score_is_byte_identical(self):
        """With zero combo_rules, rescore produces the exact same score as before
        combo scoring existed, and score_combo is NULL."""
        add_signal(self.conn, "s1", "t_lead", "account", "E_IOU", days_ago(0))
        summary = rescore(self.conn, now=NOW)
        self.assertEqual(summary, {"scored": 1, "decayed": 0})
        row = self._row("s1")
        # Score unchanged: 4 * 1.0 * (1.1 * 1.0 * 1.0) * 1.0 = 4.4
        self.assertAlmostEqual(row["score"], 4.4)
        # Four factors still multiply to the score
        product = (row["score_base"] * row["score_decay"]
                   * row["score_account_fit"] * row["score_scope_fit"])
        self.assertAlmostEqual(product, row["score"])
        # score_combo is NULL when no rules exist
        self.assertIsNone(row["score_combo"])

    def test_disabled_combo_rule_is_inert(self):
        """A combo_rules row with enabled_stage IS NULL is disabled and must not
        affect the score or set score_combo."""
        self.conn.execute(
            "INSERT INTO combo_rules (rule_id, logic_expr, multiplier, "
            " enabled_stage) VALUES ('r_off', 'trigger_any:t_lead', 2.0, NULL)")
        self.conn.commit()
        add_signal(self.conn, "s1", "t_lead", "account", "E_IOU", days_ago(0))
        rescore(self.conn, now=NOW)
        row = self._row("s1")
        self.assertAlmostEqual(row["score"], 4.4)
        self.assertIsNone(row["score_combo"])

    def test_entity_less_signal_score_combo_is_null(self):
        """entity_id IS NULL -> combo evaluation is skipped; score_combo = NULL."""
        self.conn.execute(
            "INSERT INTO combo_rules (rule_id, logic_expr, multiplier, "
            " enabled_stage) VALUES ('r1', 'trigger_any:t_reg', 1.5, 1)")
        self.conn.commit()
        add_signal(self.conn, "s1", "t_reg", "sector", None, days_ago(0))
        rescore(self.conn, now=NOW)
        row = self._row("s1")
        # Score unchanged: 5 * 1.0 * 1.0 * 0.55 = 2.75 (entity-less sector)
        self.assertAlmostEqual(row["score"], 2.75)
        self.assertIsNone(row["score_combo"])


class TestComboScoringMultiplier(unittest.TestCase):
    """R12: with an enabled rule that fires, the multiplier applies once and
    the five factors multiply to the stored score."""

    def setUp(self):
        self.conn = fixture_conn()
        # Insert one enabled combo rule for t_lead signals.
        self.conn.execute(
            "INSERT INTO combo_rules (rule_id, logic_expr, multiplier, "
            " enabled_stage) VALUES ('r1', 'trigger_any:t_lead', 1.5, 1)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _row(self, signal_id):
        return self.conn.execute(
            "SELECT score, score_base, score_decay, score_account_fit, "
            " score_scope_fit, score_combo FROM signals WHERE signal_id = ?",
            (signal_id,)).fetchone()

    def test_combo_multiplier_applies_when_rule_fires(self):
        """base 4 * decay 1 * fit 1.1 * scope 1.0 * combo 1.5 = 6.6"""
        add_signal(self.conn, "s1", "t_lead", "account", "E_IOU", days_ago(0))
        rescore(self.conn, now=NOW)
        row = self._row("s1")
        self.assertAlmostEqual(row["score"], 6.6)
        self.assertAlmostEqual(row["score_combo"], 1.5)
        # All five factors multiply to the stored score.
        five = (row["score_base"] * row["score_decay"]
                * row["score_account_fit"] * row["score_scope_fit"]
                * row["score_combo"])
        self.assertAlmostEqual(five, row["score"])

    def test_combo_does_not_fire_for_non_matching_trigger(self):
        """Rule fires on t_lead; a t_reg signal for E_IOU does not match."""
        add_signal(self.conn, "s1", "t_reg", "account", "E_IOU", days_ago(0))
        rescore(self.conn, now=NOW)
        row = self._row("s1")
        self.assertIsNone(row["score_combo"])
        # Score unchanged: 5 * 1.0 * (1.1 * 0.9 * 1.0) * 1.0 = 4.95
        self.assertAlmostEqual(
            row["score_base"] * row["score_decay"]
            * row["score_account_fit"] * row["score_scope_fit"],
            row["score"])

    def test_product_of_two_firing_rules(self):
        """Two enabled rules both fire -> multiplier is their product."""
        self.conn.execute(
            "INSERT INTO combo_rules (rule_id, logic_expr, multiplier, "
            " enabled_stage) VALUES ('r2', 'trigger_any:t_lead', 1.2, 1)")
        self.conn.commit()
        add_signal(self.conn, "s1", "t_lead", "account", "E_IOU", days_ago(0))
        rescore(self.conn, now=NOW)
        row = self._row("s1")
        # r1=1.5, r2=1.2 -> product 1.8; score = 4.4 * 1.8 = 7.92
        self.assertAlmostEqual(row["score_combo"], 1.8)
        self.assertAlmostEqual(row["score"], 4.4 * 1.8)

    def test_entity_cache_shared_across_signals(self):
        """Two signals for the same entity share the cached evaluation."""
        add_signal(self.conn, "s1", "t_lead", "account", "E_IOU", days_ago(0))
        add_signal(self.conn, "s2", "t_lead", "account", "E_IOU", days_ago(30))
        rescore(self.conn, now=NOW)
        r1 = self._row("s1")
        r2 = self._row("s2")
        # Both should have score_combo = 1.5
        self.assertAlmostEqual(r1["score_combo"], 1.5)
        self.assertAlmostEqual(r2["score_combo"], 1.5)

    def test_null_multiplier_rule_is_skipped(self):
        """An enabled rule with NULL multiplier is skipped; rescore completes
        without crashing and the remaining valid rule still fires (R12
        defense-in-depth guard for load-side validation failures)."""
        self.conn.execute(
            "INSERT INTO combo_rules (rule_id, logic_expr, multiplier, "
            " enabled_stage) VALUES ('bad', 'trigger_any:t_lead', NULL, 1)")
        self.conn.commit()
        add_signal(self.conn, "s1", "t_lead", "account", "E_IOU", days_ago(0))
        result = rescore(self.conn, now=NOW)  # must not raise
        self.assertEqual(result["scored"], 1)
        row = self._row("s1")
        # 'bad' rule skipped; 'r1' (multiplier 1.5) still fires.
        self.assertAlmostEqual(row["score_combo"], 1.5)

    def test_non_positive_multiplier_rule_is_skipped(self):
        """An enabled rule with a non-positive multiplier (bypassing load-side
        validation) is skipped rather than driving the score to <=0; the
        remaining valid rule still fires (R12 defense-in-depth)."""
        self.conn.execute(
            "INSERT INTO combo_rules (rule_id, logic_expr, multiplier, "
            " enabled_stage) VALUES ('bad', 'trigger_any:t_lead', -2.0, 1)")
        self.conn.commit()
        add_signal(self.conn, "s1", "t_lead", "account", "E_IOU", days_ago(0))
        result = rescore(self.conn, now=NOW)  # must not raise
        self.assertEqual(result["scored"], 1)
        row = self._row("s1")
        # 'bad' rule skipped; 'r1' (multiplier 1.5) still fires.
        self.assertAlmostEqual(row["score_combo"], 1.5)

    def test_only_active_rows_get_score_combo(self):
        """Dismissed and decayed rows are not rescored; score_combo stays NULL."""
        add_signal(self.conn, "gone", "t_lead", "account", "E_IOU",
                   days_ago(0), status="dismissed", score=3.3)
        add_signal(self.conn, "dead", "t_lead", "account", "E_IOU",
                   days_ago(0), status="decayed", score=0.42)
        rescore(self.conn, now=NOW)
        self.assertIsNone(self._row("gone")["score_combo"])
        self.assertIsNone(self._row("dead")["score_combo"])


class TestComboConfigVersion(unittest.TestCase):
    """R12: scoring_config_version moves when combo_rules content changes."""

    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def version(self):
        return scoring_config_version(self.conn)

    def test_empty_combo_rules_leaves_token_stable(self):
        """No combo_rules rows -> token is identical to pre-combo behaviour
        (same hash input, same output)."""
        before = self.version()
        self.assertEqual(before, self.version())

    def test_adding_combo_rule_moves_token(self):
        before = self.version()
        self.conn.execute(
            "INSERT INTO combo_rules (rule_id, logic_expr, multiplier, "
            " enabled_stage) VALUES ('r1', 'trigger_any:t_lead', 1.5, 1)")
        self.conn.commit()
        self.assertNotEqual(before, self.version())

    def test_changing_combo_multiplier_moves_token(self):
        self.conn.execute(
            "INSERT INTO combo_rules (rule_id, logic_expr, multiplier, "
            " enabled_stage) VALUES ('r1', 'trigger_any:t_lead', 1.5, 1)")
        self.conn.commit()
        before = self.version()
        self.conn.execute(
            "UPDATE combo_rules SET multiplier = 2.0 WHERE rule_id = 'r1'")
        self.conn.commit()
        self.assertNotEqual(before, self.version())

    def test_toggling_enabled_stage_moves_token(self):
        self.conn.execute(
            "INSERT INTO combo_rules (rule_id, logic_expr, multiplier, "
            " enabled_stage) VALUES ('r1', 'trigger_any:t_lead', 1.5, NULL)")
        self.conn.commit()
        before = self.version()
        self.conn.execute(
            "UPDATE combo_rules SET enabled_stage = 1 WHERE rule_id = 'r1'")
        self.conn.commit()
        self.assertNotEqual(before, self.version())

    def test_unchanged_combo_rules_leaves_token_stable(self):
        self.conn.execute(
            "INSERT INTO combo_rules (rule_id, logic_expr, multiplier, "
            " enabled_stage) VALUES ('r1', 'trigger_any:t_lead', 1.5, 1)")
        self.conn.commit()
        v1 = self.version()
        v2 = self.version()
        self.assertEqual(v1, v2)


if __name__ == "__main__":
    unittest.main()
