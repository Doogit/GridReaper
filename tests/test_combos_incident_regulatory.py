"""Combo 1 (combo_incident_regulatory) scenario tests (R10, KTD4).

Hermetic: real migrations against in-memory SQLite, FK enforcement on,
no network. Reads the live logic_expr from seeds/combo_rules.csv so the
tests bind to the real seeded rule and cannot diverge from it.
"""
import csv
import json
import os
import sqlite3
import unittest

import app.combos as combos
from app.combos import ComboExprError, TriggerAnyClause, evaluate, parse
from app.db.load_seeds import validate_combo_rules
from app.db.migrate import apply_migrations


def _read_combo_rules_csv():
    """Read seeds/combo_rules.csv; return dict rule_id -> logic_expr."""
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, ".."))
    csv_path = os.path.join(repo_root, "seeds", "combo_rules.csv")
    rules = {}
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rules[row["rule_id"]] = row["logic_expr"]
    return rules


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    for trigger_id in ("own_incident", "pipeline_enforcement_action",
                       "capital_project", "nerc_cip_revision"):
        conn.execute(
            "INSERT INTO triggers (trigger_id, name, base_strength, "
            " decay_half_life_days, mvp_flag, evidence_quality, "
            " allowed_scopes) VALUES (?, ?, 5, 270, 1, 'PC', ?)",
            (trigger_id, trigger_id, json.dumps(["account"])))
    conn.commit()
    return conn


def add_entity(conn, entity_id, subsector):
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector) "
        "VALUES (?, ?, ?)", (entity_id, entity_id, subsector))
    conn.commit()


def add_obligation(conn, obligation_id, applicability_rule):
    conn.execute(
        "INSERT INTO regulatory_obligations (obligation_id, "
        " applicability_rule, effective_date) VALUES (?, ?, '2026-01-01')",
        (obligation_id, applicability_rule))
    conn.commit()


def add_signal(conn, signal_id, entity_id, trigger_id, status="active"):
    raw_event_id = f"fixture:{signal_id}"
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_native_id, "
        " fetched_at) VALUES (?, ?, '2026-08-15T00:00:00+00:00')",
        (raw_event_id, signal_id))
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
        " signal_scope, trigger_id, headline, evidence_snippet, status) "
        "VALUES (?, ?, ?, 'account', ?, 'headline', 'snippet', ?)",
        (signal_id, raw_event_id, entity_id, trigger_id, status))
    conn.commit()


class Combo1IncidentRegulatoryTest(unittest.TestCase):
    """U10: Combo 1 fires when own_incident signal + applicable obligation."""

    def setUp(self):
        self.conn = fixture_conn()
        self.addCleanup(self.conn.close)
        self._rules = _read_combo_rules_csv()
        self._expr = self._rules["combo_incident_regulatory"]

    def test_fires_when_incident_signal_and_obligation_both_present(self):
        add_entity(self.conn, "E1", "midstream")
        add_signal(self.conn, "s1", "E1", "own_incident")
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        self.assertTrue(evaluate(self.conn, "E1", self._expr))

    def test_fires_on_pipeline_enforcement_action_signal_and_obligation(self):
        """KTD4: the allowlist includes U5's PHMSA trigger (PR #112, merged),
        so a pipeline_enforcement_action signal + applicable obligation also
        fires Combo 1 -- trigger_any matches any id in the seeded set."""
        add_entity(self.conn, "E1", "midstream")
        add_signal(self.conn, "s1", "E1", "pipeline_enforcement_action")
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        self.assertTrue(evaluate(self.conn, "E1", self._expr))

    def test_does_not_fire_when_only_incident_signal_no_obligation(self):
        add_entity(self.conn, "E1", "midstream")
        add_signal(self.conn, "s1", "E1", "own_incident")
        self.assertFalse(evaluate(self.conn, "E1", self._expr))

    def test_does_not_fire_when_only_obligation_no_incident_signal(self):
        add_entity(self.conn, "E1", "midstream")
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        self.assertFalse(evaluate(self.conn, "E1", self._expr))

    def test_does_not_fire_when_no_obligation_rows_match_entity_subsector(self):
        add_entity(self.conn, "E1", "midstream")
        add_signal(self.conn, "s1", "E1", "own_incident")
        # Obligation applies only to "lng", not "midstream"
        add_obligation(self.conn, "o1", "subsector_in:lng")
        self.assertFalse(evaluate(self.conn, "E1", self._expr))

    def test_fires_with_two_applicable_obligation_rows_exists_semantics(self):
        """KTD2: obligation:any is EXISTS -- fires on at least one match."""
        add_entity(self.conn, "E1", "midstream")
        add_signal(self.conn, "s1", "E1", "own_incident")
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        add_obligation(self.conn, "o2", "subsector_in:midstream;lng")
        self.assertTrue(evaluate(self.conn, "E1", self._expr))

    def test_seeded_logic_expr_parses_cleanly(self):
        """The real CSV expression must parse without ComboExprError."""
        try:
            parse(self._expr)
        except ComboExprError as exc:
            self.fail(
                f"combo_incident_regulatory logic_expr failed to parse: {exc}")

    def test_trigger_ids_match_incident_trigger_allowlist(self):
        """KTD4: Combo 1's trigger_any clause must equal INCIDENT_TRIGGER_IDS.

        Binding assertion: seeds/combo_rules.csv and combos.INCIDENT_TRIGGER_IDS
        must stay in sync -- any change to one must be reflected in the other.
        A drift between the two would mean the seeded rule fires on triggers
        not in the maintained allowlist, or vice versa.
        """
        parsed = parse(self._expr)
        trigger_clauses = [c for c in parsed.clauses
                           if isinstance(c, TriggerAnyClause)]
        self.assertEqual(len(trigger_clauses), 1,
                         "Combo 1 must have exactly one trigger_any clause")
        self.assertEqual(
            set(trigger_clauses[0].trigger_ids),
            set(combos.INCIDENT_TRIGGER_IDS),
            "Combo 1's trigger_ids must equal combos.INCIDENT_TRIGGER_IDS")


class ValidateComboRulesTest(unittest.TestCase):
    """R4.5: validate_combo_rules raises loudly on bad rows."""

    def _make_conn(self, trigger_ids=()):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(conn)
        for trigger_id in trigger_ids:
            conn.execute(
                "INSERT INTO triggers (trigger_id, name, base_strength, "
                " decay_half_life_days, mvp_flag, evidence_quality, "
                " allowed_scopes) VALUES (?, ?, 5, 270, 1, 'PC', ?)",
                (trigger_id, trigger_id, json.dumps(["account"])))
        conn.commit()
        return conn

    def test_raises_on_malformed_logic_expr(self):
        conn = self._make_conn(("own_incident",))
        conn.execute(
            "INSERT INTO combo_rules (rule_id, logic_expr, multiplier, "
            " enabled_stage) VALUES ('bad_rule', 'trigger_any:', 1.0, 2)")
        conn.commit()
        with self.assertRaises(ValueError) as ctx:
            validate_combo_rules(conn)
        self.assertIn("bad_rule", str(ctx.exception))
        conn.close()

    def test_raises_on_unknown_trigger_id(self):
        conn = self._make_conn(("own_incident",))
        conn.execute(
            "INSERT INTO combo_rules (rule_id, logic_expr, multiplier, "
            " enabled_stage) VALUES ('bad_rule', "
            " 'trigger_any:nonexistent_trigger AND obligation:any', 1.0, 2)")
        conn.commit()
        with self.assertRaises(ValueError) as ctx:
            validate_combo_rules(conn)
        self.assertIn("bad_rule", str(ctx.exception))
        self.assertIn("nonexistent_trigger", str(ctx.exception))
        conn.close()

    def test_passes_when_all_trigger_ids_exist(self):
        conn = self._make_conn(("own_incident",))
        conn.execute(
            "INSERT INTO combo_rules (rule_id, logic_expr, multiplier, "
            " enabled_stage) VALUES ('good_rule', "
            " 'trigger_any:own_incident AND obligation:any', 1.5, 2)")
        conn.commit()
        # Should not raise
        validate_combo_rules(conn)
        conn.close()


if __name__ == "__main__":
    unittest.main()
