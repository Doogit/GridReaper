"""Combo engine: parses and evaluates ``combo_rules.logic_expr`` (R9).

``combo_rules`` has existed since ``app/db/migrations/0001_initial.sql`` with
zero rows and zero readers. This module is the first code in the repo to read
``logic_expr`` at all -- the grammar and evaluator here, not any seeded rule,
is what U10/U11 build their two combos against.

Grammar (hand-written parser, NEVER ``eval``/``exec`` -- non-negotiable per
this project's stdlib-only, no-dynamic-code rule)::

    combo_expr := clause (" AND " clause)*
    clause      := "trigger_any:" trigger_id ("," trigger_id)*
                 | "obligation:any"
                 | "not_keyword:" term ("," term)*

``trigger_any:`` takes a comma-separated trigger_id set so a combo can express
"any trigger in this allowlist" (e.g. an incident-class allowlist) as one
clause -- OR lives inside that set, not as a separate operator; the grammar
otherwise only composes clauses with AND. ``obligation:any`` checks EXISTS
semantics against ``regulatory_obligations`` (an entity's subsector can have
more than one applicable row; the clause fires on "at least one", never
"exactly one" -- see ``app.obligations.obligation_applies``).
``not_keyword:`` is a keyword-ABSENCE modifier: it fires (True) when NONE of
its terms appear in the payload text of the active signal(s) that satisfied
the expression's ``trigger_any`` clause (case-insensitive substring match
against ``headline`` + ``evidence_snippet`` +
``raw_events.payload[PAYLOAD_TEXT_FIELD]``). Because a ``not_keyword`` clause
has no text to check without one, and because a second ``trigger_any`` clause
would silently narrow it to only the LAST clause's signals (an earlier
matching signal's keyword would go unchecked), ``parse()`` requires exactly
one ``trigger_any`` clause per expression and rejects any ``not_keyword``
clause that does not follow it -- both R10's and R11's shapes only ever need
one ``trigger_any`` clause, so this is not a loss of expressiveness.

Every clause in an expression is AND-ed together; ``evaluate()`` returns
False as soon as one clause fails, True only if every clause passes.

Malformed expressions raise ``ComboExprError`` at parse time -- the string is
never executed as code.
"""
import json
import re
from collections import namedtuple

from app.obligations import obligation_applies

AND_SEPARATOR = " AND "
TRIGGER_ANY_PREFIX = "trigger_any:"
OBLIGATION_ANY_CLAUSE = "obligation:any"
NOT_KEYWORD_PREFIX = "not_keyword:"

# combo_rules.logic_expr trigger_ids follow the same lowercase_snake_case
# shape every trigger_id in seeds/triggers.csv already uses. Enforcing the
# shape at parse time closes a loophole a looser check would leave open: an
# id list is only split on commas, so without this an unrecognized string
# like "own_incident and obligation:any" (lowercase "and", no comma) would
# silently parse as one bogus trigger_id instead of raising -- exactly the
# kind of malformed input this parser must reject, not swallow.
_TRIGGER_ID_RE = re.compile(r"^[a-z0-9_]+$")

# The raw_events.payload JSON key a not_keyword clause reads for the text of
# the signal that satisfied the preceding trigger_any clause.
#
# U11 trap: U4's capital_project classifier (app/classify/capital_project.py)
# stores award records with USAspending's CAPITALIZED keys ("Description",
# "Award ID", etc.). evidence[0]["text"] is "Award {Award ID}: {Description}",
# and runner.py writes that to signals.evidence_snippet -- so the award
# Description text lands in evidence_snippet, NOT in payload["description"]
# (payload.get("description") is None live because the key is "Description").
# The not_keyword check for Combo 2 therefore reads the award text through
# evidence_snippet; this payload key is a secondary/best-effort text source
# only for signals that DO store a lowercase "description" key.
PAYLOAD_TEXT_FIELD = "description"

# KTD4: Combo 1's incident-class trigger allowlist. A hand-maintained set
# (this project's convention, like TRIGGER_SCOPES). own_incident is the
# filer's own 8-K 1.05 disclosure; pipeline_enforcement_action is U5's PHMSA
# enforcement trigger (PR #112, merged) -- both are account-scoped
# incident/enforcement signals that, co-occurring with an applicable
# regulatory obligation, define Combo 1. Extend this set and
# seeds/combo_rules.csv's combo_incident_regulatory logic_expr together: a
# new id here MUST also join that trigger_any clause. The test in
# tests/test_combos_incident_regulatory.py binds the two so they cannot drift.
INCIDENT_TRIGGER_IDS = ("own_incident", "pipeline_enforcement_action")

TriggerAnyClause = namedtuple("TriggerAnyClause", ["trigger_ids"])
ObligationAnyClause = namedtuple("ObligationAnyClause", [])
NotKeywordClause = namedtuple("NotKeywordClause", ["terms"])
ComboExpr = namedtuple("ComboExpr", ["clauses"])


class ComboExprError(ValueError):
    """A ``combo_rules.logic_expr`` string could not be parsed.

    Raised at parse time, before any evaluation -- the string is never passed
    to ``eval``/``exec``, so an expression this doesn't recognize simply
    fails closed here instead of running as code.
    """


def _split_terms(text, clause_label, expr):
    terms = tuple(t.strip() for t in text.split(","))
    if any(not t for t in terms):
        raise ComboExprError(
            f"{clause_label} clause has an empty term in {expr!r}")
    return terms


def _parse_clause(clause_text, expr):
    if clause_text.startswith(TRIGGER_ANY_PREFIX):
        trigger_ids = _split_terms(
            clause_text[len(TRIGGER_ANY_PREFIX):], "trigger_any", expr)
        for trigger_id in trigger_ids:
            if not _TRIGGER_ID_RE.fullmatch(trigger_id):
                raise ComboExprError(
                    f"trigger_any clause has a malformed trigger_id "
                    f"{trigger_id!r} in {expr!r}")
        return TriggerAnyClause(trigger_ids)
    if clause_text == OBLIGATION_ANY_CLAUSE:
        return ObligationAnyClause()
    if clause_text.startswith(NOT_KEYWORD_PREFIX):
        terms = _split_terms(
            clause_text[len(NOT_KEYWORD_PREFIX):], "not_keyword", expr)
        return NotKeywordClause(terms)
    raise ComboExprError(f"unrecognized clause {clause_text!r} in {expr!r}")


def _validate_clause_shape(clauses, expr):
    """Enforce the invariant ``evaluate()`` relies on for not_keyword text.

    At most one ``trigger_any`` clause per expression, and every
    ``not_keyword`` clause must follow it. Without this, a second
    ``trigger_any`` would silently narrow not_keyword's text to only the
    last clause's signals, and a not_keyword with no preceding trigger_any
    would have no text to check -- both are silent-wrong-answer traps, not
    just style issues, so they are rejected here rather than left to run.
    """
    seen_trigger_any = False
    trigger_any_count = 0
    for clause in clauses:
        if isinstance(clause, TriggerAnyClause):
            trigger_any_count += 1
            seen_trigger_any = True
        elif isinstance(clause, NotKeywordClause) and not seen_trigger_any:
            raise ComboExprError(
                f"not_keyword clause has no preceding trigger_any clause "
                f"in {expr!r}")
    if trigger_any_count > 1:
        raise ComboExprError(
            f"expression has more than one trigger_any clause in {expr!r}")


def parse(expr):
    """Parse a ``combo_rules.logic_expr`` string into a ``ComboExpr``.

    Hand-written and prefix-tagged, split on the literal ``" AND "`` -- never
    ``eval``/``exec``. Raises ``ComboExprError`` on anything malformed:
    an empty expression, an empty clause (leading/trailing/doubled "AND"),
    an unrecognized clause prefix, an empty id/term inside a clause, more
    than one ``trigger_any`` clause, or a ``not_keyword`` clause with no
    preceding ``trigger_any`` clause (see ``_validate_clause_shape``).
    """
    text = (expr or "").strip()
    if not text:
        raise ComboExprError("empty combo expression")
    clause_texts = text.split(AND_SEPARATOR)
    if any(not c.strip() for c in clause_texts):
        raise ComboExprError(f"empty clause between 'AND's in {expr!r}")
    clauses = tuple(_parse_clause(c.strip(), expr) for c in clause_texts)
    _validate_clause_shape(clauses, expr)
    return ComboExpr(clauses)


def _trigger_any_active(conn, entity_id, trigger_ids):
    """True if entity_id has an active signal for any id in trigger_ids."""
    placeholders = ", ".join("?" * len(trigger_ids))
    row = conn.execute(
        "SELECT 1 FROM signals WHERE entity_id = ? AND status = 'active' "
        f"AND trigger_id IN ({placeholders}) LIMIT 1",
        (entity_id, *trigger_ids)).fetchone()
    return row is not None


def _matched_signal_texts(conn, entity_id, trigger_ids):
    """Text of entity_id's active signals whose trigger_id is in trigger_ids.

    One string per matching signal: headline + evidence_snippet + the
    PAYLOAD_TEXT_FIELD value from that signal's raw_events.payload, when
    present and parseable.
    """
    placeholders = ", ".join("?" * len(trigger_ids))
    rows = conn.execute(
        "SELECT s.headline, s.evidence_snippet, r.payload "
        "FROM signals s LEFT JOIN raw_events r "
        " ON r.raw_event_id = s.raw_event_id "
        "WHERE s.entity_id = ? AND s.status = 'active' "
        f"AND s.trigger_id IN ({placeholders})",
        (entity_id, *trigger_ids)).fetchall()
    texts = []
    for row in rows:
        parts = [row["headline"] or "", row["evidence_snippet"] or ""]
        payload_text = None
        if row["payload"]:
            try:
                payload = json.loads(row["payload"])
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                payload_text = payload.get(PAYLOAD_TEXT_FIELD)
        parts.append(payload_text or "")
        texts.append(" ".join(parts))
    return texts


def _terms_absent(text, terms):
    lowered = (text or "").lower()
    return not any(term.lower() in lowered for term in terms)


def evaluate(conn, entity_id, expr):
    """True if entity_id satisfies every clause of expr (AND across clauses).

    ``expr`` may be a raw ``logic_expr`` string (parsed here) or an
    already-parsed ``ComboExpr``. Short-circuits on the first failing clause.
    A ``not_keyword`` clause always has a preceding ``trigger_any`` clause to
    read text from: ``_validate_clause_shape`` is re-run here (not only in
    ``parse()``) so a hand-built ``ComboExpr`` that skipped ``parse()`` can
    never silently reintroduce the trivially-true/narrowed-scope traps it
    guards against.
    """
    parsed = parse(expr) if isinstance(expr, str) else expr
    _validate_clause_shape(parsed.clauses, expr)
    trigger_ids_for_text = ()
    for clause in parsed.clauses:
        if isinstance(clause, TriggerAnyClause):
            if not _trigger_any_active(conn, entity_id, clause.trigger_ids):
                return False
            trigger_ids_for_text = clause.trigger_ids
        elif isinstance(clause, ObligationAnyClause):
            if not obligation_applies(conn, entity_id):
                return False
        elif isinstance(clause, NotKeywordClause):
            texts = _matched_signal_texts(conn, entity_id,
                                          trigger_ids_for_text)
            if not _terms_absent(" ".join(texts), clause.terms):
                return False
    return True
