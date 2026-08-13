"""Company-statement incident classifier (R9.6, R10.5, R7.12, R4.1, R6.2).

Stage-2 own-incident source: a watchlist company's **own press release
disclosing a cyber incident**. Runs over the two press-wire sources
(``presswire_prnewswire`` / ``presswire_globenewswire``) already fetched for
the leadership classifier, with a different, incident-specific grammar. A
company's own statement about its own breach is a primary source, so the card
is tiered ``confirmed`` (R10.5): the framework leaves
``customer_facing_allowed=1`` so careful outreach may be drafted (R7.12).

Attribution: press-wire payloads carry no ``entity_id`` (RSS), so the company
name is the leading title segment and the **framework** resolves it against
the watchlist (``EntityResolver``), logging the decision (R6.4) and routing
ambiguous/below-threshold names to the review queue instead of firing (R6.2) -
the same name-resolution path leadership uses. Unlike the ransomware
classifier there is no attacker-controlled string in the payload, so the
standard framework resolution (headline+evidence as context) is safe here.

Grammar (precision over recall, R9.4 - a strict conjunction over the title):

  <company>  <victim verb>  ... <cyber-incident phrase>

- ``<company>`` is the leading segment before the verb (the grammatical
  subject), filtered for plausibility - the same conservative hint rule
  leadership uses. This frames the announcing company as the affected party,
  not a security vendor or law firm merely mentioning an incident.
- ``<victim verb>`` (confirms / discloses / notifies / responds to / provides
  update on / investigating / experienced / detected / ...) marks the company
  as the party disclosing, not reporting on someone else.
- ``<cyber-incident phrase>`` must appear in the post-verb remainder, and is
  cyber-specific: bare "breach" is deliberately excluded so a "breaches of
  fiduciary duties" securities-litigation release never fires (a real false
  positive in the backfill), while "data breach" / "cybersecurity incident" /
  "ransomware attack" / ... do.

Only ``own_incident`` (account scope) is emitted - a press release is the
company's statement about **itself**; inferring a sector-peer signal from one
company's release is unsupported and noisy, so no peer card is minted (the
peer path belongs to the trackers, not to statements). The headline is the
press-release title **quoted verbatim** - no verb the release does not state
is templated onto the card (R4.1).

RECALL NOTE (documented, not a gap): the current press-wire backfill contains
no cyber-incident releases (the wires we poll are dominated by earnings /
appointment / litigation-notice traffic), so this classifier emits at or near
zero signals today. It exists for the live corpus; its behaviour is pinned by
canned fixtures. Recovering recall would need broader breach-notification
feeds (state-AG breach DBs, security RSS), which are separate source chunks.

  python -m app.classify.company_statement [--source X] [--limit N] [--force]
"""
import html
import json
import re
import sys

from app.classify import runner as classify_runner

CLASSIFIER_ID = "company_statement"
PARSER_VERSION = "company_statement/1.0"
TRIGGER_ID = "own_incident"          # reuse the Stage-2 own-incident trigger

CONFIDENCE = 0.85    # a company's own primary statement; grammar is fuzzier
                     # than a structured SEC item (incident.py OWN=0.9)

_TAG = re.compile(r"<[^>]+>")

# The company is the disclosing/affected party. Kept to verbs that frame the
# subject as the one experiencing or announcing its own incident - reporting
# verbs ("releases", "publishes a report on") are excluded so a vendor's
# threat report does not read as that vendor's breach.
_VICTIM_VERBS = (
    r"confirms|discloses|announces|reports|notifies|investigating|investigates|"
    r"experienced|experiences|detected|detects|suffered|suffers|"
    r"identified|identifies|responds\s+to|provides\s+(?:an\s+)?update\s+on|"
    r"issues\s+(?:a\s+)?statement\s+(?:on|regarding)|"
    r"statement\s+(?:on|regarding)|updates?\s+on")
_COMPANY_VERB = re.compile(
    rf"^(?P<company>.{{2,80}}?)\s+(?:{_VICTIM_VERBS})\b(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL)

# Cyber-specific incident phrases. Bare "breach" is intentionally absent
# (fiduciary-duty / contract "breach" is not a cyber event); every phrase here
# is unambiguously a security incident.
_CYBER_PHRASES = [
    "cybersecurity incident", "cyber security incident", "cyber incident",
    "cyberattack", "cyber attack", "cyber-attack",
    "data breach", "data security incident", "data-security incident",
    "security breach", "network breach", "security incident",
    "ransomware attack", "ransomware incident", "ransomware event",
    "ransomware", "unauthorized access", "malware attack",
    "information security incident", "data theft",
]
_CYBER_RE = re.compile(
    "|".join(re.escape(p) for p in _CYBER_PHRASES), re.IGNORECASE)


def _clean_text(s):
    """Strip HTML tags, unescape entities, collapse whitespace."""
    return " ".join(html.unescape(_TAG.sub(" ", s or "")).split())


def _plausible_company(text):
    """Conservative company-hint filter; '' means emit nothing (mirrors the
    leadership classifier's rule so a garbled leading segment never resolves)."""
    hint = text.strip().strip(",;:.-–—").strip()
    if not 2 <= len(hint) <= 80:
        return ""
    if len(hint.split()) > 10:
        return ""
    if not re.search(r"[A-Za-z]", hint):
        return ""
    return hint


def classify_presswire(conn, raw):
    """Press-release breach disclosure -> one own_incident (confirmed) cand."""
    payload = json.loads(raw["payload"] or "{}")
    title = _clean_text(payload.get("title"))
    if not title:
        return []

    m = _COMPANY_VERB.match(title)
    if not m:
        return []
    if not _CYBER_RE.search(m.group("rest")):   # incident phrase after the verb
        return []
    company = _plausible_company(m.group("company"))
    if not company:
        return []

    return [{
        "trigger_id": TRIGGER_ID,
        "signal_scope": "account",
        "entity_id": None,
        "entity_name_hint": company,
        "event_date": raw["event_date"] or "",
        "headline": title,                       # quoted verbatim (R4.1)
        "evidence": [{"text": title, "locator": "title"}],
        "confidence": CONFIDENCE,
        "incident_evidence_level": "confirmed",
    }]


SOURCES = {
    "presswire_prnewswire": classify_presswire,
    "presswire_globenewswire": classify_presswire,
}


if __name__ == "__main__":
    sys.exit(classify_runner.cli(
        CLASSIFIER_ID, SOURCES, PARSER_VERSION,
        "Classify company-own cyber-incident press releases into own_incident "
        "confirmed signals."))
