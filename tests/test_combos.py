"""Combo engine parser/evaluator tests (R9, KTD1, KTD2).

Hermetic: real migrations against in-memory SQLite, FK enforcement on, no
network. Covers the two worked expression shapes from
docs/plans/2026-08-18-001-feat-combo-engine-account-signal-expansion-plan.md
(R10's incident-and-obligation shape, R11's capital-project-and-obligation-
and-no-security-keyword shape) plus the no-eval parse-time-failure contract.
"""
import json
import sqlite3
import unittest

from app.combos import (ComboExpr, ComboExprError, NotKeywordClause,
                        ObligationAnyClause, TriggerAnyClause, evaluate,
                        parse)
from app.db.migrate import apply_migrations

R11_KEYWORDS = ("cybersecurity", "cyber security", "OT security",
                "ICS security", "SCADA security", "intrusion detection",
                "SIEM", "security operations center",
                "industrial control system security")


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


def add_signal(conn, signal_id, entity_id, trigger_id, payload=None,
               status="active"):
    """An account-scoped signal, optionally with a raw_events.payload."""
    raw_event_id = f"fixture:{signal_id}"
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_native_id, "
        " fetched_at, payload) "
        "VALUES (?, ?, '2026-08-15T00:00:00+00:00', ?)",
        (raw_event_id, signal_id,
         json.dumps(payload) if payload is not None else None))
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
        " signal_scope, trigger_id, headline, evidence_snippet, status) "
        "VALUES (?, ?, ?, 'account', ?, 'headline', 'snippet', ?)",
        (signal_id, raw_event_id, entity_id, trigger_id, status))
    conn.commit()


class ParseTest(unittest.TestCase):
    def test_parses_r10_shape(self):
        expr = parse("trigger_any:own_incident AND obligation:any")
        self.assertEqual(
            expr, ComboExpr((TriggerAnyClause(("own_incident",)),
                             ObligationAnyClause())))

    def test_parses_r10_extended_shape_with_multiple_trigger_ids(self):
        expr = parse("trigger_any:own_incident,pipeline_enforcement_action "
                     "AND obligation:any")
        self.assertEqual(
            expr.clauses[0],
            TriggerAnyClause(("own_incident", "pipeline_enforcement_action")))
        self.assertEqual(expr.clauses[1], ObligationAnyClause())

    def test_parses_r11_shape(self):
        expr_str = ("trigger_any:capital_project AND obligation:any AND "
                    "not_keyword:" + ",".join(R11_KEYWORDS))
        expr = parse(expr_str)
        self.assertEqual(len(expr.clauses), 3)
        self.assertEqual(expr.clauses[0], TriggerAnyClause(("capital_project",)))
        self.assertEqual(expr.clauses[1], ObligationAnyClause())
        self.assertEqual(expr.clauses[2], NotKeywordClause(R11_KEYWORDS))

    def test_malformed_expressions_raise_comboexprerror(self):
        malformed = (
            "",
            "   ",
            "trigger_any:",
            "trigger_any:own_incident,,other",
            "trigger_any:own_incident AND",
            "AND obligation:any",
            "trigger_any:own_incident AND AND obligation:any",
            "obligation:all",
            "not_keyword:",
            "unrecognized_clause:foo",
            # lowercase "and" with no comma: without id-shape validation this
            # would silently parse as one bogus trigger_id instead of
            # raising -- must fail closed, not swallow it.
            "trigger_any:own_incident and obligation:any",
            # never reaches eval/exec: an unrecognized clause always raises,
            # it is never executed as code.
            "__import__('os').system('echo pwned')",
            # not_keyword with no preceding trigger_any has no text to check
            # -- rejected at parse time rather than silently trivially true.
            "not_keyword:cybersecurity",
            "obligation:any AND not_keyword:cybersecurity",
            "not_keyword:cybersecurity AND trigger_any:capital_project",
            # a second trigger_any would silently narrow not_keyword's text
            # to only the last clause's signals -- rejected, not silently
            # accepted with one clause's signals unchecked.
            ("trigger_any:own_incident AND "
             "trigger_any:pipeline_enforcement_action AND obligation:any"),
        )
        for expr in malformed:
            with self.subTest(expr=expr):
                with self.assertRaises(ComboExprError):
                    parse(expr)


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()
        self.addCleanup(self.conn.close)

    def test_true_when_trigger_and_obligation_both_present(self):
        add_entity(self.conn, "E1", "midstream")
        add_signal(self.conn, "s1", "E1", "own_incident")
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        self.assertTrue(
            evaluate(self.conn, "E1",
                    "trigger_any:own_incident AND obligation:any"))

    def test_false_when_only_trigger_present(self):
        add_entity(self.conn, "E1", "midstream")
        add_signal(self.conn, "s1", "E1", "own_incident")
        self.assertFalse(
            evaluate(self.conn, "E1",
                    "trigger_any:own_incident AND obligation:any"))

    def test_false_when_only_obligation_present(self):
        add_entity(self.conn, "E1", "midstream")
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        self.assertFalse(
            evaluate(self.conn, "E1",
                    "trigger_any:own_incident AND obligation:any"))

    def test_true_with_two_applicable_obligation_rows_exists_semantics(self):
        add_entity(self.conn, "E1", "midstream")
        add_signal(self.conn, "s1", "E1", "own_incident")
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        add_obligation(self.conn, "o2", "subsector_in:midstream;lng")
        self.assertTrue(
            evaluate(self.conn, "E1",
                    "trigger_any:own_incident AND obligation:any"))

    def test_trigger_any_matches_any_id_in_the_set(self):
        add_entity(self.conn, "E1", "midstream")
        add_signal(self.conn, "s1", "E1", "pipeline_enforcement_action")
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        self.assertTrue(
            evaluate(self.conn, "E1",
                    "trigger_any:own_incident,pipeline_enforcement_action "
                    "AND obligation:any"))

    def test_not_keyword_true_when_payload_lacks_listed_terms(self):
        add_entity(self.conn, "E1", "midstream")
        add_signal(self.conn, "s1", "E1", "capital_project",
                   payload={"description": "New LNG terminal construction."})
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        expr = ("trigger_any:capital_project AND obligation:any AND "
               "not_keyword:" + ",".join(R11_KEYWORDS))
        self.assertTrue(evaluate(self.conn, "E1", expr))

    def test_not_keyword_false_when_payload_contains_a_listed_term(self):
        add_entity(self.conn, "E1", "midstream")
        add_signal(
            self.conn, "s1", "E1", "capital_project",
            payload={"description": "SCADA security upgrade for the plant."})
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        expr = ("trigger_any:capital_project AND obligation:any AND "
               "not_keyword:" + ",".join(R11_KEYWORDS))
        self.assertFalse(evaluate(self.conn, "E1", expr))

    def test_evaluate_accepts_a_pre_parsed_expr(self):
        add_entity(self.conn, "E1", "midstream")
        add_signal(self.conn, "s1", "E1", "own_incident")
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        parsed = parse("trigger_any:own_incident AND obligation:any")
        self.assertTrue(evaluate(self.conn, "E1", parsed))

    def test_evaluate_rejects_a_hand_built_expr_with_bad_clause_shape(self):
        # _validate_clause_shape must be enforced by evaluate() itself, not
        # only by parse(), so a ComboExpr built directly (bypassing parse())
        # can never resurrect the trivially-true not_keyword trap.
        add_entity(self.conn, "E1", "midstream")
        bad = ComboExpr((NotKeywordClause(("cybersecurity",)),))
        with self.assertRaises(ComboExprError):
            evaluate(self.conn, "E1", bad)

    def test_inactive_signal_does_not_satisfy_trigger_any(self):
        add_entity(self.conn, "E1", "midstream")
        add_signal(self.conn, "s1", "E1", "own_incident", status="retracted")
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        self.assertFalse(
            evaluate(self.conn, "E1",
                    "trigger_any:own_incident AND obligation:any"))

    def test_not_keyword_match_is_case_insensitive(self):
        add_entity(self.conn, "E1", "midstream")
        add_signal(
            self.conn, "s1", "E1", "capital_project",
            payload={"description": "scada SECURITY retrofit planned."})
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        expr = ("trigger_any:capital_project AND obligation:any AND "
               "not_keyword:" + ",".join(R11_KEYWORDS))
        self.assertFalse(evaluate(self.conn, "E1", expr))

    def test_not_keyword_checks_headline_not_just_payload(self):
        add_entity(self.conn, "E1", "midstream")
        conn = self.conn
        conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_native_id, "
            " fetched_at) VALUES ('fixture:s1', 's1', "
            " '2026-08-15T00:00:00+00:00')")
        conn.execute(
            "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
            " signal_scope, trigger_id, headline, evidence_snippet, status) "
            "VALUES ('s1', 'fixture:s1', 'E1', 'account', 'capital_project', "
            " 'SIEM deployment announced', 'snippet', 'active')")
        conn.commit()
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        expr = ("trigger_any:capital_project AND obligation:any AND "
               "not_keyword:" + ",".join(R11_KEYWORDS))
        self.assertFalse(evaluate(self.conn, "E1", expr))

    def test_not_keyword_checks_every_matching_signal_not_just_one(self):
        add_entity(self.conn, "E1", "midstream")
        add_signal(self.conn, "s1", "E1", "capital_project",
                   payload={"description": "New LNG terminal construction."})
        add_signal(
            self.conn, "s2", "E1", "capital_project",
            payload={"description": "SCADA security upgrade for the plant."})
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        expr = ("trigger_any:capital_project AND obligation:any AND "
               "not_keyword:" + ",".join(R11_KEYWORDS))
        self.assertFalse(evaluate(self.conn, "E1", expr))

    def test_not_keyword_true_when_payload_json_is_malformed(self):
        add_entity(self.conn, "E1", "midstream")
        conn = self.conn
        conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_native_id, "
            " fetched_at, payload) VALUES ('fixture:s1', 's1', "
            " '2026-08-15T00:00:00+00:00', '{not valid json')")
        conn.execute(
            "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
            " signal_scope, trigger_id, headline, evidence_snippet, status) "
            "VALUES ('s1', 'fixture:s1', 'E1', 'account', 'capital_project', "
            " 'headline', 'snippet', 'active')")
        conn.commit()
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        expr = ("trigger_any:capital_project AND obligation:any AND "
               "not_keyword:" + ",".join(R11_KEYWORDS))
        self.assertTrue(evaluate(self.conn, "E1", expr))

    def test_not_keyword_true_when_payload_json_is_not_a_dict(self):
        add_entity(self.conn, "E1", "midstream")
        add_signal(self.conn, "s1", "E1", "capital_project",
                   payload=["not", "a", "dict"])
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        expr = ("trigger_any:capital_project AND obligation:any AND "
               "not_keyword:" + ",".join(R11_KEYWORDS))
        self.assertTrue(evaluate(self.conn, "E1", expr))


if __name__ == "__main__":
    unittest.main()
