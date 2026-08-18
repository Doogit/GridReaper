"""CISA ICS advisories classifier: energy-sector exploited-vuln signals
(R9.6, R7.2, R4.1, R10.6).

Reads app/ingest/cisa_ics.py's stored advisories and mints ONE sector-scoped
signal per advisory that names Energy among CISA's own "Critical
Infrastructure Sectors" label - "timing, not targeting": an ICS vulnerability
affecting energy-sector equipment classes, not a claim about any specific
watchlist account.

ics_cve_kev's seeded description ("New exploited vuln in a product the
account is known to run") reads account-shaped, but the store has no
per-account tech-stack data to back that claim (which product a given
account actually runs). This classifier is therefore granted
signal_scope=['sector'] ONLY (app/db/load_seeds.py TRIGGER_SCOPES), never
'account' - no candidate here ever carries an entity_id.
app/classify/runner.py drops anything a trigger's allowed_scopes config
doesn't grant (R7.2), so a future seed regression that removed the sector
grant fails loudly (dropped_scope), not silently.

Advisories outside the energy sector (per CISA's own label) emit nothing -
precision over recall (R9.4): a mapping error here would misfile an
unrelated-sector vulnerability as an energy-sector signal. The sector match
is on the exact, comma-split "Critical Infrastructure Sectors" tokens, not a
substring search of the whole description - CISA's CVE prose elsewhere in
the advisory could otherwise mention "energy" in an unrelated sense (a power
budget, an energy company named as an unaffected example, etc.).

  python -m app.classify.cisa_ics [--source X] [--limit N] [--force]
"""
import json
import re
import sys

from app.classify import runner as classify_runner

CLASSIFIER_ID = "cisa_ics"
PARSER_VERSION = "cisa_ics/1.0"
SOURCE_ID = "cisa_ics"

TRIGGER_ID = "ics_cve_kev"
CONFIDENCE = 0.8   # primary CISA advisory; the sector label is CISA's own,
                   # not an inference from free text (mirrors PEER_CONFIDENCE
                   # in app/classify/incident.py for a confirmed class-level
                   # read of a primary source)

_SECTORS_RE = re.compile(
    r"Critical Infrastructure Sectors:\s*</strong>\s*(.*?)\s*</li>",
    re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
ENERGY = "energy"


def _energy_sectors_text(description):
    """The verbatim "Critical Infrastructure Sectors" list text from a CISA
    advisory description, or None when the advisory's description has no
    such list or the list doesn't name Energy. Matched on the exact,
    comma-split sector tokens (not a substring search) so no other CISA
    sector name, and no unrelated use of the word "energy" elsewhere in the
    advisory body, can false-positive."""
    m = _SECTORS_RE.search(description or "")
    if not m:
        return None
    raw_list = _TAG_RE.sub("", m.group(1)).strip()
    sectors = [s.strip() for s in raw_list.split(",") if s.strip()]
    if not any(s.lower() == ENERGY for s in sectors):
        return None
    return raw_list


def classify_cisa_ics(conn, raw):
    """One CISA ICS advisory naming Energy in its Critical Infrastructure
    Sectors list -> one sector-scoped signal. Everything else emits
    nothing."""
    payload = json.loads(raw["payload"] or "{}")
    title = (payload.get("title") or "").strip()
    description = payload.get("description") or ""
    sectors_text = _energy_sectors_text(description)
    if sectors_text is None:
        return []

    headline = "CISA ICS advisory affecting energy-sector equipment"
    if title:
        headline += f": {title}"

    evidence = [{
        "text": (f"CISA's Critical Infrastructure Sectors for this "
                 f"advisory: {sectors_text}."),
        "locator": "description",
    }]
    if title:
        evidence.append({"text": title, "locator": "title"})

    return [{
        "trigger_id": TRIGGER_ID,
        "signal_scope": "sector",
        "entity_id": None,
        "entity_name_hint": None,
        "event_date": raw["event_date"] or "",
        "headline": headline,
        "evidence": evidence,
        "confidence": CONFIDENCE,
    }]


SOURCES = {SOURCE_ID: classify_cisa_ics}


if __name__ == "__main__":
    sys.exit(classify_runner.cli(
        CLASSIFIER_ID, SOURCES, PARSER_VERSION,
        "Classify CISA ICS advisories into energy-sector exploited-vuln "
        "signals."))
