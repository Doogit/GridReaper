"""Incident classifier: SEC 8-K Item 1.05 material cyber incidents (R9.6,
R10.5, R7.12, R4.1, R7.2).

Stage-2's first incident source and the highest-signal, most-structured one:
Item 1.05 of an 8-K is the SEC's dedicated "Material Cybersecurity Incidents"
item, so its mere presence in a filing's item list IS the signal - no body
text is needed (submissions payloads carry none). This mirrors the leadership
8-K 5.02 path, minus 5.02's title-token requirement: 5.02 is generic officer
churn, 1.05 is cyber-specific by definition.

EDGAR submissions are polled per active watchlist CIK (app.ingest.edgar), so
every stored 8-K is filed by a tracked entity and pre-attributed with
``entity_id``. One 1.05 filing therefore yields two candidates:

  own_incident   account scope, the filer's own disclosure (R10.5 confirmed:
                 a primary SEC filing). The framework validates the entity is
                 active, rolls it up to the top-level account, and - because
                 the tier is confirmed - leaves customer_facing_allowed=1 so
                 careful outreach may be drafted (R7.12).
  peer_incident  sector scope, the same disclosure read as a class signal to
                 sector peers. Class-phrased and name-free in the headline (the
                 filer is not named in a sector card, mirroring the
                 URE-anonymized enforcement card); the framework attaches no
                 entity. Also confirmed. The card is sector-wide: subsector-
                 precise peer targeting is deferred - it needs a subsector
                 dimension on signals, and a subsector label in the headline
                 would be an unsourced (R4.1), mutable claim on a card the
                 scope does not actually filter by subsector.

Both candidates are confirmed because the evidence is a primary SEC filing.
Precision over recall: only Item 1.05 fires; no verb the payload does not
state is templated onto the card (the SEC item title is quoted verbatim).

KNOWN LIMITATION (documented, not a gap): an 8-K and its later 8-K/A amending
the same incident are distinct raw_events, so an amendment can mint a second
card for one incident. Grouping accession families is deferred; both forms are
accepted because an 8-K/A 1.05 is itself a material update worth surfacing.

  python -m app.classify.incident [--source X] [--limit N] [--force]
"""
import json
import sys

from app.classify import runner as classify_runner

CLASSIFIER_ID = "incident"
PARSER_VERSION = "incident/1.0"
SOURCE_ID = "sec_edgar_submissions"

EDGAR_FORMS = ("8-K", "8-K/A")
INCIDENT_ITEM = "1.05"
ITEM_LABEL = "Material Cybersecurity Incidents"  # SEC's own name for Item 1.05

OWN_CONFIDENCE = 0.9    # pre-attributed primary filing; item is cyber-specific
PEER_CONFIDENCE = 0.8   # equally confirmed, but a class-level inference


def classify_edgar_incident(conn, raw):
    """8-K/8-K-A carrying Item 1.05 -> own + peer confirmed-incident candidates.

    Item 1.05 alone is sufficient (it is the cyber-incident item); no title
    token is inspected. entity_id is required (submissions are pre-attributed);
    a filing without one cannot be attributed and is skipped."""
    payload = json.loads(raw["payload"] or "{}")
    if payload.get("form") not in EDGAR_FORMS:
        return []
    if INCIDENT_ITEM not in (payload.get("items") or []):
        return []
    entity_id = (payload.get("entity_id") or "").strip()
    if not entity_id:
        return []

    form = payload.get("form")
    accession = (payload.get("accessionNumber") or "").strip()
    filing_date = (payload.get("filingDate") or "").strip()
    event_date = (payload.get("reportDate") or payload.get("filingDate")
                  or raw["event_date"] or "")

    # Evidence is the primary filing itself (R4.1); the item title is the SEC's
    # fixed name for 1.05, quoted - no claim the payload does not state.
    primary = f"SEC Form {form} Item {INCIDENT_ITEM} ({ITEM_LABEL})"
    if accession:
        primary += f", accession {accession}"
    if filing_date:
        primary += f", filed {filing_date}"
    evidence = [{"text": primary + ".", "locator": "items"}]
    desc = (payload.get("primaryDocDescription") or "").strip()
    if desc and desc.lower() not in {"8-k", (form or "").lower()}:
        evidence.append({"text": desc, "locator": "primaryDocDescription"})

    own = {
        "trigger_id": "own_incident",
        "signal_scope": "account",
        "entity_id": entity_id,
        "entity_name_hint": None,
        "event_date": event_date,
        "headline": f"SEC Form {form} Item {INCIDENT_ITEM}: {ITEM_LABEL}",
        "evidence": evidence,
        "confidence": OWN_CONFIDENCE,
        "incident_evidence_level": "confirmed",
    }

    peer = {
        "trigger_id": "peer_incident",
        "signal_scope": "sector",
        "entity_id": None,
        "entity_name_hint": None,
        "event_date": event_date,
        "headline": (f"Sector peer filed SEC Form {form} Item "
                     f"{INCIDENT_ITEM} ({ITEM_LABEL})"),
        "evidence": evidence,
        "confidence": PEER_CONFIDENCE,
        "incident_evidence_level": "confirmed",
    }
    return [own, peer]


SOURCES = {SOURCE_ID: classify_edgar_incident}


if __name__ == "__main__":
    sys.exit(classify_runner.cli(
        CLASSIFIER_ID, SOURCES, PARSER_VERSION,
        "Classify SEC 8-K Item 1.05 material cybersecurity incidents into "
        "own/peer confirmed-incident signals."))
