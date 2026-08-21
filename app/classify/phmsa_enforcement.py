"""PHMSA pipeline-enforcement classifier: genuine CAO/NOPV/Warning Letter
enforcement cases against midstream/LNG watchlist accounts ->
pipeline_enforcement_action account-scoped signals (R6, U5 of the
combo-engine plan).

app/ingest/phmsa.py already fetch-filters to the three qualifying case types
(mirroring KEEP_FORMS in app/ingest/edgar.py and the JDC filter in
app/ingest/epa_echo.py); this classifier re-checks Case_Type rather than
trusting the fetcher's filter, matching app/classify/environmental_
enforcement.py's convention.

SUBSECTOR SCOPING IS THIS CLASSIFIER'S JOB, NOT THE FETCHER'S. Unlike EPA
ECHO (fetch-filtered by SIC/NAICS to oil & gas, so any resolved entity is
already in-scope), PHMSA's feed covers every PHMSA-regulated pipeline
operator nationwide -- gas, liquid, and LNG, watchlist or not. R6 restricts
the trigger to midstream/LNG watchlist entities specifically, so this
classifier follows app/classify/ransomware.py's pattern instead of app/
classify/regulatory.py's: it resolves the operator name itself (the only
identity the record carries), checks the subsector of the account the
resolved entity ROLLS UP TO (see below), and either emits a candidate
carrying an already-resolved entity_id (bypassing the framework's own
re-resolution, exactly as ransomware.py does for own_incident) or emits
nothing. Doing the resolve here, once, is also what lets the subsector check
happen before a candidate is ever built -- the framework's generic
entity_name_hint path resolves AFTER the candidate is constructed and has no
subsector concept at all.

CHECK THE ROLLED-UP ACCOUNT, NOT THE RAW RESOLVED ENTITY. The framework's
_process_candidate (app/classify/runner.py) unconditionally re-derives
entity_id via top_level_entity() for every account-scope candidate, even one
that already carries an entity_id (R6.5's parent rollup). A resolved entity
whose own subsector is midstream/LNG could in principle roll up to a parent
in a different subsector; checking the child's subsector alone would then
pass a candidate whose card ultimately lands on an out-of-scope account. This
classifier therefore checks _entity_subsector on
classify_runner.top_level_entity(conn, res.entity_id) -- the same entity the
framework will actually store the signal against -- not on res.entity_id
directly. No current seed data has a midstream/LNG entity with a
differently-scoped parent, but the check must hold regardless of today's
data (found in code review).

  matched, rolled-up account is midstream/LNG -> emits the candidate
                              (entity_id set directly; decision logged,
                              R6.4).
  matched, rolled-up account is another subsector -> excluded; the record
                              names a real watchlist entity, but the account
                              its signal would be stored against sits outside
                              midstream/LNG (R6).
  review                   -> R6.2: below-threshold/ambiguous names never
                              auto-fire. Enqueued so no injected PHMSA text
                              can upgrade it downstream; no candidate.
  none                     -> off-list operator (e.g. a non-watchlist
                              pipeline company). No sector-level analog is
                              defined for this trigger, so nothing is
                              emitted -- account-only, per R6's brief.

SEVERITY. R6's brief asks for base_strength/evidence_quality "differentiated
by enforcement severity (CAO/NOPV higher than Warning Letter)". Both of
those columns are per-TRIGGER constants in seeds/triggers.csv (one value for
every pipeline_enforcement_action signal, not per-signal) -- there is no
schema mechanism to vary either by an individual case's severity. The
signal-level field that DOES vary per case is ``confidence``, which is what
this classifier actually differentiates (CASE_TYPE_INFO below), and
the evidence explicitly names the severity tier in a dedicated "severity"
locator so a reviewer can see it on the card regardless. Note this does not
change a card's SCORE: app/scoring.py's score formula
(base_strength * decay * account_fit * scope_fit) does not read
``confidence`` at all, so the severity signal here is legible on the card,
not scoring-relevant -- a schema-driven limitation surfaced in the PR rather
than silently worked around.

R10.6 (field allowlist): PHMSA's enforcement-data schema (inspected live
2026-08-18 via the feed's own header row) carries no individual
attorney/officer/signatory field -- see app/ingest/phmsa.py's module
docstring. _evidence() below only ever quotes a fixed, named subset of the
stored columns (never the raw record wholesale), so this stays true even if
a future PHMSA schema change added a field this module does not name.

Run: python -m app.classify.phmsa_enforcement [--source X] [--limit N]
     [--force]
"""
import json
import sys
from collections import namedtuple

from app.classify import runner as classify_runner
from app.resolve import EntityResolver, enqueue_review, record_decision

CLASSIFIER_ID = "phmsa_enforcement"
PARSER_VERSION = "phmsa_enforcement/1.0"

QUALIFYING_CASE_TYPES = frozenset({
    "Corrective Action Order", "Notice of Probable Violation",
    "Warning Letter"})

MIDSTREAM_LNG_SUBSECTORS = ("midstream", "lng")

MAX_HEADLINE_CHARS = 140

# Severity-differentiated per-signal confidence (see module docstring for why
# this, not base_strength/evidence_quality, is what actually varies), paired
# with the evidence text naming the tier -- one dict keyed by case_type
# rather than two, so a future case-type addition to one half can't be
# missed from the other (found in code review).
CaseTypeInfo = namedtuple("CaseTypeInfo", ["confidence", "severity_note"])

CASE_TYPE_INFO = {
    "Corrective Action Order": CaseTypeInfo(
        0.85,
        "Corrective Action Order (CAO): PHMSA's most severe pre-hearing "
        "enforcement mechanism, ordering immediate corrective measures to "
        "address a pipeline integrity threat."),
    "Notice of Probable Violation": CaseTypeInfo(
        0.8,
        "Notice of Probable Violation (NOPV): a formal PHMSA finding of a "
        "probable regulatory violation, which may lead to a civil penalty "
        "or compliance order."),
    "Warning Letter": CaseTypeInfo(
        0.7,
        "Warning Letter: PHMSA's least severe enforcement notice, "
        "documenting a violation without a proposed civil penalty."),
}


def _headline(case_type, operator_name):
    text = f"PHMSA {case_type}: {operator_name}"
    if len(text) > MAX_HEADLINE_CHARS:
        text = text[:MAX_HEADLINE_CHARS - 1].rstrip() + "…"
    return text


def _evidence(row, case_type):
    ev = [{"text": (f"CPF {row.get('CPF_Number', '')}: {case_type} against "
                    f"{row.get('Operator_Name', '')}"),
           "locator": "case"}]
    if (row.get("Violation_Category") or "").strip():
        ev.append({"text": f"Violation category: {row['Violation_Category']}",
                   "locator": "violation_category"})
    if (row.get("Cited_Regulations") or "").strip():
        ev.append({"text": f"Cited regulations: {row['Cited_Regulations']}",
                   "locator": "cited_regulations"})
    if (row.get("Opened_Date") or "").strip():
        ev.append({"text": f"Opened {row['Opened_Date']}",
                   "locator": "opened_date"})
    ev.append({"text": CASE_TYPE_INFO[case_type].severity_note,
               "locator": "severity"})
    return ev


def _entity_subsector(conn, entity_id):
    row = conn.execute(
        "SELECT subsector FROM watchlist_entities WHERE entity_id = ?",
        (entity_id,)).fetchone()
    return (row["subsector"] or "") if row else ""


def classify_phmsa_enforcement(conn, raw):
    """One PHMSA enforcement-feed row -> at most one
    pipeline_enforcement_action candidate for a midstream/LNG watchlist
    entity. See the module docstring for the accept rule."""
    try:
        row = json.loads(raw["payload"] or "")
    except ValueError:
        return []
    if not isinstance(row, dict):
        return []

    case_type = (row.get("Case_Type") or "").strip()
    operator_name = (row.get("Operator_Name") or "").strip()
    if case_type not in QUALIFYING_CASE_TYPES or not operator_name:
        return []

    resolver = EntityResolver(conn)   # per event; watchlist is small and the
    #                                   run holds the single-writer lock, so
    #                                   it always reflects current entities
    #                                   (same rationale as
    #                                   app.classify.ransomware).
    res = resolver.resolve(name=operator_name)

    if res.status == "review":
        # R6.2: below-threshold/ambiguous names never auto-fire. Enqueued
        # here (not left for the framework) so no injected PHMSA text can
        # upgrade it downstream -- matches app.classify.ransomware's
        # rationale for resolving authoritatively.
        enqueue_review(conn, raw["raw_event_id"], res)
        return []
    if res.status != "matched":
        return []   # off-list operator; no sector-level analog for R6
    # Scope-check the ROLLED-UP account, not the raw resolved entity: the
    # framework's _process_candidate unconditionally re-derives entity_id via
    # top_level_entity() even when a candidate already carries one (R6.5), so
    # a midstream/LNG subsidiary whose parent sits in a different subsector
    # would otherwise pass this gate on the child's subsector while the card
    # actually lands on the parent's account -- exactly the "outside the
    # trigger's scope" case this check exists to prevent (found in code
    # review; no current seed data hits it, but the check must match what
    # actually gets stored, not an intermediate node in the entity graph).
    account_entity_id = classify_runner.top_level_entity(conn, res.entity_id)
    if _entity_subsector(conn, account_entity_id) not in MIDSTREAM_LNG_SUBSECTORS:
        return []   # real watchlist match, but the account it rolls up to
                     # is outside the trigger's scope

    record_decision(conn, raw["raw_event_id"], res, decided_by="classification")
    return [{
        "trigger_id": "pipeline_enforcement_action",
        "signal_scope": "account",
        "entity_id": res.entity_id,     # authoritative: no framework re-resolve
        "entity_name_hint": None,
        # raw["event_date"] is the fetcher's already-ISO-parsed Opened_Date
        # (R10.2); row["Opened_Date"] (quoted in evidence above) is PHMSA's
        # own raw M/D/YY string.
        "event_date": raw["event_date"] or "",
        "headline": _headline(case_type, operator_name),
        "evidence": _evidence(row, case_type),
        "confidence": min(CASE_TYPE_INFO[case_type].confidence, res.confidence),
    }]


SOURCES = {
    "phmsa_enforcement": classify_phmsa_enforcement,
}


if __name__ == "__main__":
    sys.exit(classify_runner.cli(
        CLASSIFIER_ID, SOURCES, PARSER_VERSION,
        "Classify PHMSA enforcement cases into pipeline_enforcement_action "
        "signals for midstream/LNG watchlist accounts."))
