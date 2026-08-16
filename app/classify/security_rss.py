"""Security-press incident classifier (R9.6, R10.5, R7.12, R4.1, R6.2, R6.4).

Stage-2's security-press incident source. Classifies the two security-press
feeds landed by ``app/ingest/security_rss.py`` - The Record and BleepingComputer
- into own/peer incident signals with a per-source evidence tier (R10.5). The
PRD lists ``security RSS`` under BOTH the ``own_incident`` and ``peer_incident``
trigger primary_sources, so both paths belong here.

Like ransomware.live (``app/classify/ransomware.py``) and UNLIKE the 8-K 1.05
classifier (``app/classify/incident.py``), a security-press item is not
pre-attributed to a watchlist entity: the affected company is free text in the
headline and may or may not be on the watchlist - a genuine own-vs-off-list
split. This classifier is therefore AUTHORITATIVE over attribution: it resolves
the reported company name ITSELF (name-only) and commits a concrete entity_id,
so no reporter/attacker-controlled headline string ever reaches the framework's
headline+evidence resolution context (``runner.py``), where a bare collision
term ("Dominion") could false-corroborate a mis-attributed card.

Two grammars over the item title (precision over recall, R9.4 - a general
security feed is dominated by CVE / malware / policy traffic that names no
victim, so the grammar is a strict gate, not a broad net):

  DISCLOSURE  ``<company> <victim-verb: discloses|confirms|notifies|suffered|
              hit by|investigating|...> ... <cyber-incident phrase>``. The
              company is the grammatical subject disclosing or suffering its own
              incident (mirrors ``app/classify/company_statement.py``). Reported
              by reputable security press, so the card is tiered CORROBORATED
              (R10.5) - one tier below the company's own press release
              (confirmed); customer-facing outreach stays allowed (R7.12).
                matched -> own_incident (account); decision logged (R6.4), the
                           framework validates the entity is active and rolls it
                           up to the top-level account (R6.5).
                review  -> enqueue only, no card (R6.2, the operator's
                           queue-only rule).
                none    -> peer_incident (sector), NAME-FREE.

  LEAK-ADJACENT (BleepingComputer only)  an item whose breach is an attacker or
              leak-site CLAIM ("claims to have breached", "listed on ... leak
              site", "data for sale", "extortion") rather than a victim
              disclosure. Per the fetcher's source policy the classifier must
              down-tier these to UNCONFIRMED_EARLY_WARNING (R7.12): outreach
              suppressed, operator-only. These emit a NAME-FREE peer card ONLY,
              never an own card - an attacker-chosen claim string is exactly the
              source-controlled text that can embed a victim identity, so (more
              conservative than ransomware.py, whose structured ``victim`` field
              is trustworthy) this classifier never extracts a victim from a
              leak claim. It under-attributes (a claim naming a watchlist company
              mints a peer, not an own, card) but never mis-attributes or leaks
              identity (R4.1). The Record is non-leak journalism (its items are
              always corroborated), so it has no leak-adjacent path.

R4.1 discipline: an own card quotes the item title verbatim (the company IS that
account); a peer card is generic and name-free (the reported company is never
printed) and templates no verb the source does not state.

RECALL NOTE (documented, not a gap): a general security feed name-matches almost
nothing against the ~171-entity energy watchlist, so the own path emits at or
near zero today and the name-free peer path is the reliably-firing output. This
classifier exists for the live corpus; its behaviour is pinned by canned
fixtures.

KNOWN LIMITATIONS (accepted, documented):
  * Grammar reads the title only (descriptions are noisier); an incident named
    only in the body is missed.
  * The peer path is class-level: an off-list victim mints a generic sector
    early-warning regardless of the victim's own subsector (mirrors
    ransomware.py). Scoring/decay carries the relevance weight.

  python -m app.classify.security_rss [--source X] [--limit N] [--force]
"""
import html
import json
import re
import sys

from app.classify import runner as classify_runner
from app.resolve import EntityResolver, enqueue_review, record_decision

CLASSIFIER_ID = "incident_security_rss"
PARSER_VERSION = "incident_security_rss/1.0"

# Press reporting of a disclosed incident: fuzzier than a structured SEC item
# but firmer than a leak-site claim.
OWN_CONFIDENCE = 0.7      # a watchlist company's disclosure, as reported
PEER_CONFIDENCE = 0.6     # off-list disclosure read as a sector signal
LEAK_CONFIDENCE = 0.45    # an unverified attacker/leak-site claim

CORROBORATED = "corroborated"
UNCONFIRMED = "unconfirmed_early_warning"

_TAG = re.compile(r"<[^>]+>")

# The company is the disclosing/affected party. Kept to verbs that frame the
# subject as the one experiencing or announcing its own incident - reporting
# verbs are excluded so a vendor's or researcher's report does not read as that
# party's breach. Passive victim forms ("hit by", "targeted by") keep the
# subject as the victim, with the cyber-phrase gate below constraining them.
_VICTIM_VERBS = (
    r"confirms|confirmed|discloses|disclosed|announces|announced|"
    r"reports|reported|notifies|notified|investigating|investigates|"
    r"experienced|experiences|detected|detects|suffered|suffers|"
    r"identified|identifies|responds\s+to|responding\s+to|"
    r"hit\s+by|struck\s+by|impacted\s+by|affected\s+by|targeted\s+by|"
    r"provides\s+(?:an\s+)?update\s+on|"
    r"issues\s+(?:a\s+)?statement\s+(?:on|regarding)")
_COMPANY_VERB = re.compile(
    rf"^(?P<company>.{{2,80}}?)\s+(?:{_VICTIM_VERBS})\b(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL)

# Cyber-specific incident phrases. Bare "breach" is intentionally absent
# (fiduciary/contract "breach" is not a cyber event); every phrase here is
# unambiguously a security incident.
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

# Leak-adjacent markers: the breach is an attacker/leak-site CLAIM, not a victim
# disclosure. Deliberately specific so generic security news never matches.
_LEAK_MARKERS = re.compile(
    r"claims?\s+to\s+have\s+(?:breached|hacked|stolen|attacked|hit)|"
    r"claims?\s+(?:responsibility|the\s+breach)|"
    r"(?:data\s+)?leak\s+site|"
    r"(?:up\s+for|put\s+up\s+for|offered\s+for|listed\s+for)\s+sale|"
    r"data\s+for\s+sale|extortion\s+(?:attempt|demand|scheme)?|"
    r"ransomware\s+gang\s+claims|allegedly\s+(?:breached|hacked|stole)",
    re.IGNORECASE)


def _clean_text(s):
    """Strip HTML tags, unescape entities, collapse whitespace."""
    return " ".join(html.unescape(_TAG.sub(" ", s or "")).split())


def _plausible_company(text):
    """Conservative company-hint filter; '' means emit nothing (mirrors the
    leadership/statement classifiers so a garbled leading segment never
    resolves)."""
    hint = text.strip().strip(",;:.-–—").strip()
    if not 2 <= len(hint) <= 80:
        return ""
    if len(hint.split()) > 10:
        return ""
    if not re.search(r"[A-Za-z]", hint):
        return ""
    return hint


def _record_decision_once(conn, raw_event_id, res):
    """R6.4 log, idempotent under --force: one decision per raw_event."""
    if conn.execute(
            "SELECT 1 FROM entity_match_decisions "
            "WHERE raw_event_id = ? AND decided_by = 'classification'",
            (raw_event_id,)).fetchone() is None:
        record_decision(conn, raw_event_id, res, decided_by="classification")


def _enqueue_review_once(conn, raw_event_id, res):
    """R6.2 queue, idempotent under --force: don't pile duplicate pendings."""
    if conn.execute(
            "SELECT 1 FROM review_queue "
            "WHERE raw_event_id = ? AND disposition = 'pending'",
            (raw_event_id,)).fetchone() is None:
        enqueue_review(conn, raw_event_id, res)


def _event_date(raw):
    return (raw["event_date"] or "")[:10]


def _disclosure_cands(conn, raw, title, company, source_tier):
    """Resolve the disclosing company (name-only) and act on the outcome.
    Returns the candidate list (own or name-free peer), or [] when the name
    goes to review (queued, no card)."""
    resolver = EntityResolver(conn)
    res = resolver.resolve(name=company)

    if res.status == "review":
        # Ambiguous / collision / below-threshold: never auto-fire (R6.2) and,
        # per the operator's queue-only rule, never mint a peer card either.
        # Enqueue here so no injected headline context can upgrade it
        # downstream.
        _enqueue_review_once(conn, raw["raw_event_id"], res)
        return []

    if res.status == "matched":
        _record_decision_once(conn, raw["raw_event_id"], res)
        return [{
            "trigger_id": "own_incident",
            "signal_scope": "account",
            "entity_id": res.entity_id,     # authoritative: no framework re-resolve
            "entity_name_hint": None,
            "event_date": _event_date(raw),
            "headline": title,              # verbatim (R4.1); names the account
            "evidence": [{"text": title, "locator": "title"}],
            "confidence": min(OWN_CONFIDENCE, res.confidence),
            "incident_evidence_level": source_tier,
        }]

    # status == "none": off-list company -> class-level sector card, name-free.
    return [{
        "trigger_id": "peer_incident",
        "signal_scope": "sector",
        "entity_id": None,
        "entity_name_hint": None,
        "event_date": _event_date(raw),
        "headline": "Sector peer disclosed a cybersecurity incident - "
                    "security-press reported",
        "evidence": [{"text":
            "Security-press reporting indicates an organization disclosed a "
            "cybersecurity incident.", "locator": "source"}],
        "confidence": PEER_CONFIDENCE,
        "incident_evidence_level": source_tier,
    }]


def _leak_peer(raw):
    """A BleepingComputer leak-adjacent claim -> one name-free peer card,
    down-tiered to unconfirmed_early_warning (outreach suppressed, R7.12)."""
    return {
        "trigger_id": "peer_incident",
        "signal_scope": "sector",
        "entity_id": None,
        "entity_name_hint": None,
        "event_date": _event_date(raw),
        "headline": "Sector peer named in an unverified breach claim - "
                    "early warning",
        "evidence": [{"text":
            "Security-press reporting references an unverified attacker or "
            "leak-site breach claim against an organization.",
            "locator": "source"}],
        "confidence": LEAK_CONFIDENCE,
        "incident_evidence_level": UNCONFIRMED,
    }


def _classify(conn, raw, source_id, source_tier):
    """One security-press item -> at most one own OR peer candidate.

    Disclosure grammar first (victim-as-subject); if it structurally matches a
    cyber-incident sentence the item is spoken for (a card, or a queued review).
    Otherwise, for BleepingComputer only, a leak-adjacent claim mints a name-free
    unconfirmed peer. Everything else (generic CVE / malware / policy news) is
    dropped.
    """
    payload = json.loads(raw["payload"] or "{}")
    title = _clean_text(payload.get("title"))
    if not title:
        return []

    m = _COMPANY_VERB.match(title)
    if m and _CYBER_RE.search(m.group("rest")):
        company = _plausible_company(m.group("company"))
        if company:
            return _disclosure_cands(conn, raw, title, company, source_tier)
        return []       # cyber sentence with a garbled subject -> unusable

    if source_id == "bleepingcomputer" and _LEAK_MARKERS.search(title):
        return [_leak_peer(raw)]
    return []


def classify_the_record(conn, raw):
    return _classify(conn, raw, "the_record", CORROBORATED)


def classify_bleepingcomputer(conn, raw):
    return _classify(conn, raw, "bleepingcomputer", CORROBORATED)


SOURCES = {
    "the_record": classify_the_record,
    "bleepingcomputer": classify_bleepingcomputer,
}


if __name__ == "__main__":
    sys.exit(classify_runner.cli(
        CLASSIFIER_ID, SOURCES, PARSER_VERSION,
        "Classify security-press items into own/peer incident signals with a "
        "per-source evidence tier."))
