"""EPA ECHO environmental enforcement classifier: genuine judicial civil
consent-decree case records -> account-scoped audit_consent_decree signals
(R4.1, R7, R9.4).

Precision over recall, matching app/classify/regulatory.py's convention:
EPA's Enforcement Case Search (ICIS-FE&C) case-category field distinguishes
AFR (Administrative - Formal, an order/penalty EPA settles under its own
authority) from JDC (Judicial Civil, referred to DOJ and entered by a
federal court) -- only the latter is, by EPA/DOJ practice, resolved via
consent decree. app/ingest/epa_echo.py already fetch-filters to JDC only
(mirroring KEEP_FORMS in app/ingest/edgar.py); this classifier re-checks it
rather than trusting the fetcher's filter, and adds two further conjuncts
neither the fetch filter nor a bare JDC flag can express:

  civil, not criminal    CivilCriminalIndicator == "CI". A small number of
                          JDC cases are criminal referrals; those end in a
                          plea or verdict, never a consent decree.
  actually settled        SettlementDate is set. An open JDC case (still in
                          litigation, no SettlementDate yet) has no decree to
                          report -- reporting one would be R4.1 fabrication,
                          the same failure mode as "a routine fine with no
                          consent decree" generalized to "a case with no
                          consent decree yet".

ECHO's case data has no literal "IsConsentDecree" boolean -- confirmed
against the live API 2026-08-18: EnfOutcome reads "Final Order With Penalty"
even for settled JDC cases naming oil & gas defendants (e.g. HILCORP ENERGY
COMPANY, case 03-2022-7006). JDC + civil + settled is the closest defensible,
non-fabricated proxy the source actually offers, so it is stated as a proxy
in the card evidence (see the "classifier_note" evidence item below) rather
than claimed as a literal field match.

Entity attribution: CaseName often carries trailing case-management
annotations ECHO appends itself ("(LEAD)", "(NATIONAL CASE)", "(NC)", ...)
or a co-defendant marker ("... , ET AL.") that are not part of the company's
legal name and would defeat the resolver's fuzzy match; _clean_case_name
strips every parenthetical (repeatedly, so a nested "(A (B) C)" fully clears
instead of leaving a stray ")") and a trailing "ET AL." before the name
reaches entity_name_hint -- ECHO's set of annotation tokens is not
documented, so stripping structurally rather than matching a known token
list is safer. The framework resolves the cleaned name against the
watchlist or routes it to review (R6.2); this module never matches a name
itself.

R10.6 (field allowlist): case_rest_services' response schema (inspected live
2026-08-18 via its own metadata endpoint) carries no individual
attorney/officer/signatory field -- every field is case- or defendant-
entity-level (CaseNumber, CaseName, CaseCategoryCode, PrimaryLaw, dates,
penalty amounts, ...). _evidence() below only ever quotes a fixed, named
subset of those fields (never the raw record wholesale), so this stays true
even if a future ECHO schema change added a field this module does not name.

Run: python -m app.classify.environmental_enforcement [--source X]
     [--limit N] [--force]
"""
import json
import re
import sys

from app.classify import runner as classify_runner

CLASSIFIER_ID = "environmental_enforcement"
PARSER_VERSION = "environmental_enforcement/1.0"

JUDICIAL_CIVIL_CATEGORY = "JDC"
CIVIL_INDICATOR = "CI"

CONFIDENCE = 0.75
MAX_HEADLINE_CHARS = 140

# Non-nesting on purpose: applied in a fixed-point loop by _clean_case_name
# so a nested "(A (B) C)" clears from the innermost pair outward instead of
# a single greedy pass leaving a stray ")" behind.
_PAREN_RE = re.compile(r"\([^()]*\)")
_TRAILING_ET_AL_RE = re.compile(r",?\s*ET\s+AL\.?\s*$", re.IGNORECASE)


def _clean_case_name(name):
    """Strip ECHO's own trailing case-management annotations (including
    nested parentheticals) and a co-defendant "ET AL." marker so the
    company's actual legal name reaches the resolver."""
    stripped = name or ""
    while True:
        new = _PAREN_RE.sub("", stripped)
        if new == stripped:
            break
        stripped = new
    stripped = _TRAILING_ET_AL_RE.sub("", stripped)
    return " ".join(stripped.split()).strip(" ,")


def _headline(case_name, primary_law):
    law = primary_law or "environmental"
    text = f"EPA judicial civil enforcement settlement ({law}): {case_name}"
    if len(text) > MAX_HEADLINE_CHARS:
        text = text[:MAX_HEADLINE_CHARS - 1].rstrip() + "…"
    return text


def _evidence(case):
    ev = [{"text": (f"Case {case.get('CaseNumber', '')}: "
                    f"{case.get('CaseName', '')}"),
           "locator": "case_name"}]
    if case.get("PrimaryLaw"):
        ev.append({"text": f"Primary law: {case['PrimaryLaw']}",
                   "locator": "primary_law"})
    if case.get("SettlementDate"):
        ev.append({"text": f"Settled {case['SettlementDate']}",
                   "locator": "settlement_date"})
    if case.get("EnfOutcome"):
        ev.append({"text": f"Enforcement outcome: {case['EnfOutcome']}",
                   "locator": "enf_outcome"})
    ev.append({"text": ("Judicial civil (JDC) case category: referred to "
                        "DOJ and filed in federal court, the mechanism by "
                        "which an EPA environmental enforcement settlement "
                        "becomes a consent decree."),
              "locator": "classifier_note"})
    return ev


def classify_epa_echo(conn, raw):
    """EPA ECHO case record -> audit_consent_decree candidate, genuine
    judicial-civil-settled cases only. See the module docstring for the
    accept rule."""
    try:
        case = json.loads(raw["payload"] or "")
    except ValueError:
        return []
    if not isinstance(case, dict):
        return []

    if case.get("CaseCategoryCode") != JUDICIAL_CIVIL_CATEGORY:
        return []
    if case.get("CivilCriminalIndicator") != CIVIL_INDICATOR:
        return []
    if not (case.get("SettlementDate") or "").strip():
        return []   # not yet resolved -- no decree to report (R4.1)

    raw_name = (case.get("CaseName") or "").strip()
    name = _clean_case_name(raw_name)
    if not name:
        return []

    return [{
        "trigger_id": "audit_consent_decree", "signal_scope": "account",
        "entity_id": None, "entity_name_hint": name,
        "event_date": raw["event_date"] or "",
        "headline": _headline(name, case.get("PrimaryLaw")),
        "evidence": _evidence(case),
        "confidence": CONFIDENCE,
    }]


SOURCES = {
    "epa_echo": classify_epa_echo,
}


if __name__ == "__main__":
    sys.exit(classify_runner.cli(
        CLASSIFIER_ID, SOURCES, PARSER_VERSION,
        "Classify EPA ECHO enforcement case records into "
        "audit_consent_decree signals."))
