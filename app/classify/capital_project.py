"""USAspending.gov capital-project classifier: DOE/CISA/DHS grant and
cooperative-agreement awards -> account-scoped capital_project signals
(R4.1, R5).

Precision over recall, matching app/classify/regulatory.py's and
app/classify/environmental_enforcement.py's convention: this classifier
re-checks agency qualification independently rather than trusting
app/ingest/usaspending.py's fetch-time agency filter alone (defense in
depth -- a raw event reaching this classifier might in principle have come
from a reprocessed or hand-loaded fixture that never went through the
fetcher's filter).

ACCEPT RULE. A raw event classifies when its payload carries a non-empty
Award ID, Recipient Name and Awarding Agency, AND the Awarding Agency is
Department of Energy or Department of Homeland Security (DHS toptier
covers CISA, a DHS sub-agency; the Awarding Sub Agency field is checked
too in case a response ever carries CISA under a different toptier
label). A record missing any required field, or funded by any other
agency, is not a capital_project candidate -- skipped, never fabricated
(R4.1). Award ID is required here (not just Recipient Name/Awarding
Agency) so evidence text can never cite a blank "Award : ..." prefix for a
hand-loaded or reprocessed record that never went through the fetcher's
own required-field check.

The award Description is quoted into evidence verbatim (not summarized):
this is the free text a later unit's keyword-absence check (no-OT-tooling
combo) reads, so this classifier must never paraphrase or truncate it
beyond the shared MAX_HEADLINE_CHARS headline cap, which affects only the
headline, never the evidence text.

Entity attribution: the recipient name is passed to the framework as
entity_name_hint, which resolves it against the watchlist (or routes it to
review / drops it, R6.2) exactly like every other name-only classifier in
this codebase -- this module never matches a name itself.

R10.6 (field allowlist / no unvetted PII): app/ingest/usaspending.py
requests an explicit ``fields`` allowlist from the API (Award ID,
Recipient Name, Awarding Agency, Awarding Sub Agency, Start Date,
Description) -- no individual officer/signatory field is ever requested or
stored, so none is available for this classifier to quote even if a future
schema addition introduced one upstream. ``_evidence()`` below additionally
only ever quotes that same fixed, named subset into headline/evidence
text, matching app/classify/environmental_enforcement.py's precedent for
this discipline.

Run: python -m app.classify.capital_project [--source X] [--limit N] [--force]
"""
import json
import sys

from app.classify import runner as classify_runner

CLASSIFIER_ID = "capital_project"
PARSER_VERSION = "capital_project/1.0"

QUALIFYING_TOPTIER_AGENCIES = frozenset({
    "Department of Energy", "Department of Homeland Security"})
_QUALIFYING_TOPTIER_AGENCIES_CASEFOLD = frozenset(
    a.casefold() for a in QUALIFYING_TOPTIER_AGENCIES)
CISA_SUBAGENCY_MARKER = "Cybersecurity and Infrastructure Security Agency"

CONFIDENCE = 0.75
MAX_HEADLINE_CHARS = 140


def _is_qualifying_agency(agency, subagency):
    if agency.casefold() in _QUALIFYING_TOPTIER_AGENCIES_CASEFOLD:
        return True
    return CISA_SUBAGENCY_MARKER.casefold() in (subagency or "").casefold()


def _headline(recipient, agency):
    text = f"USAspending capital-project award ({agency}): {recipient}"
    if len(text) > MAX_HEADLINE_CHARS:
        text = text[:MAX_HEADLINE_CHARS - 1].rstrip() + "…"
    return text


def _evidence(award):
    ev = [{"text": (f"Award {award.get('Award ID', '')}: "
                    f"{award.get('Description') or '(no description provided)'}"),
          "locator": "description"}]
    agency_text = award.get("Awarding Agency", "")
    if award.get("Awarding Sub Agency"):
        agency_text += f" ({award['Awarding Sub Agency']})"
    ev.append({"text": f"Awarding agency: {agency_text}",
              "locator": "awarding_agency"})
    if award.get("Start Date"):
        ev.append({"text": f"Award start date: {award['Start Date']}",
                  "locator": "start_date"})
    return ev


def classify_usaspending(conn, raw):
    """USAspending award record -> capital_project candidate, DOE/CISA/DHS
    grants and cooperative agreements only. See the module docstring for
    the accept rule."""
    try:
        award = json.loads(raw["payload"] or "")
    except ValueError:
        return []
    if not isinstance(award, dict):
        return []

    award_id = (award.get("Award ID") or "").strip()
    recipient = (award.get("Recipient Name") or "").strip()
    agency = (award.get("Awarding Agency") or "").strip()
    if not award_id or not recipient or not agency:
        return []
    if not _is_qualifying_agency(agency, award.get("Awarding Sub Agency")):
        return []

    event_date = raw["event_date"] or (award.get("Start Date") or "").strip()
    return [{
        "trigger_id": "capital_project", "signal_scope": "account",
        "entity_id": None, "entity_name_hint": recipient,
        "event_date": event_date,
        "headline": _headline(recipient, agency),
        "evidence": _evidence(award),
        "confidence": CONFIDENCE,
    }]


SOURCES = {
    "usaspending": classify_usaspending,
}


if __name__ == "__main__":
    sys.exit(classify_runner.cli(
        CLASSIFIER_ID, SOURCES, PARSER_VERSION,
        "Classify USAspending.gov awards into capital_project signals."))
