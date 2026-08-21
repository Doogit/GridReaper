"""Combo 2 (combo_no_ot_tooling) scenario tests (R11, KTD8).

Hermetic: real migrations against in-memory SQLite, FK enforcement on,
no network. Reads the live logic_expr from seeds/combo_rules.csv.

CRITICAL fixture shape: these tests mirror U4's REAL capital_project signal
shape -- evidence_snippet carries the award description text (as runner.py
writes evidence[0]["text"] = "Award {Award ID}: {Description}" to it), and
raw_events.payload uses CAPITALIZED keys (USAspending's format: "Description",
"Award ID", etc.). This means payload.get("description") (lowercase) is None
live. The combo engine's not_keyword clause must read through evidence_snippet
to catch OT-security keywords, not through the (None) lowercase payload key.
Any regression where the check relied only on payload["description"] would
make the SCADA-security test below fire incorrectly (it would not fire,
because the check would see None and pass) -- that is the regression these
fixtures are designed to catch.
"""
import csv
import json
import os
import sqlite3
import unittest

from app.combos import NotKeywordClause, evaluate, parse
from app.db.migrate import apply_migrations

# KTD8: the exact keyword list Combo 2 must screen. Matches the seeded CSV;
# binding assertion below verifies they cannot drift.
KTD8_KEYWORDS = (
    "cybersecurity",
    "cyber security",
    "OT security",
    "ICS security",
    "SCADA security",
    "intrusion detection",
    "SIEM",
    "security operations center",
    "industrial control system security",
)


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
    for trigger_id in ("capital_project", "own_incident", "nerc_cip_revision"):
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


def add_capital_signal(conn, signal_id, entity_id, evidence_snippet,
                       capitalized_payload, status="active"):
    """Insert a capital_project signal shaped like U4's classifier output.

    evidence_snippet carries the award description text (the path the combo
    engine must read to find keyword text). capitalized_payload uses
    USAspending's CAPITALIZED keys ("Description", "Award ID", ...) so that
    payload.get("description") (lowercase) returns None -- the only way the
    not_keyword check can see the award text is through evidence_snippet.
    headline is intentionally empty to isolate the evidence_snippet path.
    """
    raw_event_id = f"fixture:{signal_id}"
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_native_id, "
        " fetched_at, payload) VALUES (?, ?, '2026-08-15T00:00:00+00:00', ?)",
        (raw_event_id, signal_id, json.dumps(capitalized_payload)))
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
        " signal_scope, trigger_id, headline, evidence_snippet, status) "
        "VALUES (?, ?, ?, 'account', 'capital_project', '', ?, ?)",
        (signal_id, raw_event_id, entity_id, evidence_snippet, status))
    conn.commit()


class Combo2NoOtToolingTest(unittest.TestCase):
    """U11: Combo 2 fires on capital_project + obligation when no OT-security keyword."""

    def setUp(self):
        self.conn = fixture_conn()
        self.addCleanup(self.conn.close)
        self._expr = _read_combo_rules_csv()["combo_no_ot_tooling"]

    def _capitalized_payload(self, award_id, description):
        """USAspending-shaped payload with CAPITALIZED keys (matches U4 output)."""
        return {
            "Award ID": award_id,
            "Description": description,
            "Recipient Name": "Acme Midstream LLC",
            "Awarding Agency": "Department of Energy",
        }

    def test_fires_lng_construction_no_keyword(self):
        """Fires: award description is clean infrastructure work (no listed keyword)."""
        add_entity(self.conn, "E1", "midstream")
        add_capital_signal(
            self.conn, "s1", "E1",
            evidence_snippet=(
                "Award A1: New LNG terminal construction and compressor "
                "station upgrade."),
            capitalized_payload=self._capitalized_payload(
                "A1",
                "New LNG terminal construction and compressor station upgrade."))
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        self.assertTrue(evaluate(self.conn, "E1", self._expr))

    def test_does_not_fire_scada_security_keyword_in_evidence_snippet(self):
        """SCADA security keyword in evidence_snippet suppresses the combo.

        This test PROVES the combo engine reads award text through
        evidence_snippet, not through payload.get("description") (lowercase).
        payload uses the CAPITALIZED key "Description" -- payload.get("description")
        is None. If the check read only from the (None) payload key, it would
        see no keyword and incorrectly fire. The test only passes when the
        engine reads evidence_snippet correctly.
        """
        add_entity(self.conn, "E1", "midstream")
        add_capital_signal(
            self.conn, "s1", "E1",
            evidence_snippet="Award A2: SCADA security upgrade for the plant.",
            capitalized_payload=self._capitalized_payload(
                "A2", "SCADA security upgrade for the plant."))
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        self.assertFalse(evaluate(self.conn, "E1", self._expr))

    def test_methane_monitoring_fires_non_false_negative(self):
        """KTD8: methane monitoring is NOT a listed OT-security keyword.

        Bare "monitoring" must not trigger the absence filter -- only the
        specific compound terms in the list apply. This guards against
        over-broadening the keyword list to accidental false negatives.
        """
        add_entity(self.conn, "E1", "midstream")
        add_capital_signal(
            self.conn, "s1", "E1",
            evidence_snippet=(
                "Award A3: methane monitoring system installation across "
                "the pipeline network."),
            capitalized_payload=self._capitalized_payload(
                "A3",
                "methane monitoring system installation across the pipeline "
                "network."))
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        self.assertTrue(evaluate(self.conn, "E1", self._expr))

    def test_does_not_fire_without_applicable_obligation(self):
        add_entity(self.conn, "E1", "midstream")
        add_capital_signal(
            self.conn, "s1", "E1",
            evidence_snippet="Award A4: Pipeline integrity inspection.",
            capitalized_payload=self._capitalized_payload(
                "A4", "Pipeline integrity inspection."))
        # No obligation inserted
        self.assertFalse(evaluate(self.conn, "E1", self._expr))

    def test_does_not_fire_without_capital_project_signal(self):
        add_entity(self.conn, "E1", "midstream")
        add_obligation(self.conn, "o1", "subsector_in:midstream")
        # No signal inserted
        self.assertFalse(evaluate(self.conn, "E1", self._expr))

    def test_seeded_logic_expr_parses_cleanly(self):
        from app.combos import ComboExprError
        try:
            parse(self._expr)
        except ComboExprError as exc:
            self.fail(
                f"combo_no_ot_tooling logic_expr failed to parse: {exc}")

    def test_not_keyword_terms_match_ktd8_list(self):
        """KTD8 binding: the seeded not_keyword terms must equal the KTD8 list exactly."""
        parsed = parse(self._expr)
        nk_clauses = [c for c in parsed.clauses if isinstance(c, NotKeywordClause)]
        self.assertEqual(len(nk_clauses), 1,
                         "Combo 2 must have exactly one not_keyword clause")
        self.assertEqual(
            nk_clauses[0].terms, KTD8_KEYWORDS,
            "Combo 2's not_keyword terms must match the KTD8 list exactly")


if __name__ == "__main__":
    unittest.main()
