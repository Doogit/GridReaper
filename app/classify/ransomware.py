"""ransomware.live early-warning incident classifier (R9.6, R10.5, R7.12,
R4.1, R6.2, R6.4, R6.5, R7.2).

Stage-2's operator-only early-warning incident source. ransomware.live is a
leak-site tracker: a victim appearing on it is an *unverified claim* by a
ransomware group, not a confirmed breach. Every card this classifier mints is
therefore tiered ``unconfirmed_early_warning`` (R10.5); the framework then
derives customer_facing_allowed=0 (R7.12) - operator-only, outreach opener
suppressed - and precision reporting keeps these cards out of account
precision (R8.6).

Unlike the 8-K 1.05 classifier (app/classify/incident.py), whose EDGAR
payloads are pre-attributed to a watchlist CIK, ransomware.live carries *all*
victims keyed only by company name. Attribution is a genuine own-vs-off-list
split, and each record yields at most ONE card.

This classifier is AUTHORITATIVE over that attribution: it resolves the victim
name itself (the only identity the record carries) and acts on the result,
rather than handing a name hint to the framework's own re-resolution. That is
deliberate. A card's headline/evidence necessarily quote the attacker-chosen
GROUP name, and the framework builds its resolution context from a candidate's
headline+evidence (runner.py); were the own path to re-resolve there, that
injected group string could act as false corroboration and either fire a
mis-attributed card or upgrade an ambiguous name past the review gate. By
resolving here (name-only, so only the victim's own identity counts) and
committing a concrete entity_id, no leak-site text ever feeds resolution.

  matched  -> own_incident (account). The decision is logged (R6.4) and the
             framework validates the entity is active and rolls it up to the
             top-level account (R6.5). The own card may name the entity (it IS
             that account) and quotes the sourced group name.
  review   -> ambiguous / collision / below-threshold (R6.2/R6.3). Enqueued for
             a human and NO card is minted (Kevin's queue-only rule). Enqueued
             here so the framework never gets a chance to re-resolve it.
  none     -> peer_incident (sector), but ONLY for an energy-industry victim
             (see PEER_ACTIVITIES). Off-list victim read as a class signal to
             sector peers, and NAME-FREE in the strong sense: neither the
             victim name NOR the attacker-controlled group string is printed
             (a crew can name itself after its victim), so the sector card
             leaks no company identity (R4.1).

A peer card asserts the victim is a *sector peer* of the watchlist, so that
claim is checked rather than assumed (R4.1: never template a claim the source
does not support). ransomware.live tracks victims across every industry - in a
100-record sample only 2 were "Energy & Utilities", against 15 Manufacturing,
15 Professional Services, 13 Technology and 11 Healthcare - so classifying the
whole feed as peers made 97 of 104 stored signals a sector claim the source
never made. The tracker's own ``activity`` tag is the check.

Industry only: geography is deliberately NOT filtered (Kevin's ruling). A
utility in another country is still an industry peer of a US utility; the
sector lesson travels, the jurisdiction does not have to. Size/revenue would
be a defensible second axis but the record carries no size field, so it is
not attempted.

R4.1 discipline: the evidence is the tracker listing itself, quoted as a claim
("listed on ... leak site ... unverified"); it never templates a verb the
source does not state - "breached"/"confirmed" are forbidden.

Idempotence: normal re-runs skip already-classified events, so the resolution
side effects fire once. A ``--force`` reclassification reprocesses events, so
the decision/review writes are guarded (``_record_decision_once`` /
``_enqueue_review_once``) to stay stable, matching the signals insert-or-skip.

KNOWN LIMITATIONS (accepted, documented):
  * ransomware.live exposes no stable per-victim id, so an upstream record
    revision mints a new raw_event (content-hash keyed, see
    app/ingest/ransomware.py); one real incident can surface as several cards.
  * Attribution is name-only: a watchlist victim whose leak-site string differs
    enough from its stored name/aliases resolves to ``none`` and mints a PEER
    card instead of an OWN one (it under-attributes, but never leaks identity).
    Seeding common leak-site abbreviations as aliases mitigates.
  * The industry gate trusts the tracker's ``activity`` tag. An energy victim
    tagged with another industry (or "Not Found" - 17 of 100 in the sample)
    mints no peer card. That under-fires by design: an unknown industry is not
    evidence of a match. The own path is unaffected, so a watchlist victim
    still fires whatever its tag says.

Data credit: ransomware.live (https://www.ransomware.live), CC-BY-4.0 (R10.4).

  python -m app.classify.ransomware [--limit N] [--force]
"""
import json
import sys

from app.classify import runner as classify_runner
from app.resolve import EntityResolver, enqueue_review, record_decision

CLASSIFIER_ID = "incident_ransomware"
PARSER_VERSION = "incident_ransomware/1.1"
SOURCE_ID = "ransomware_live"

# ransomware.live ``activity`` values that make an off-list victim an industry
# peer of the watchlist. Matched on a normalized form ("&" -> "and", casefolded)
# so upstream punctuation/case drift does not silently drop a real peer. The
# watchlist spans electric utilities AND oil & gas / midstream / refining
# (see the subsector weights), so both bands count. Anything else - including
# an absent or "Not Found" tag - mints no peer card.
PEER_ACTIVITIES = frozenset({
    "energy",
    "energy and utilities",
    "utilities",
    "oil and gas",
})

# Unconfirmed early warning: intentionally low confidence, and outreach is
# suppressed by the tier regardless (R7.12).
OWN_CONFIDENCE = 0.5
PEER_CONFIDENCE = 0.45
TIER = "unconfirmed_early_warning"


def is_peer_industry(activity):
    """True when the tracker's ``activity`` tag places the victim in the
    watchlist's industry. Normalizes "&" to "and" and casefolds so
    "Energy & Utilities" / "energy and utilities" both match.

    Public because the Explore Ransomware Activity aggregate (app/ui/data.py)
    highlights exactly the industry rows that mint peer cards; sharing this one
    predicate is what keeps the panel and the gate from drifting apart."""
    normalized = " ".join((activity or "").replace("&", "and").split()).lower()
    return normalized in PEER_ACTIVITIES


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


def classify_ransomware(conn, raw):
    """One ransomware.live victim record -> at most one own OR peer candidate.

    Resolve the victim name (name-only - the only identity the record carries)
    and act: matched -> own (account, decision logged); review -> queue only,
    no card (R6.2); none -> name-free peer (sector). A record with no victim
    name is unusable -> [].
    """
    payload = json.loads(raw["payload"] or "{}")
    victim = (payload.get("victim") or "").strip()
    if not victim:
        return []
    group = (payload.get("group") or "").strip()
    event_date = ((payload.get("discovered") or payload.get("attackdate")
                   or raw["event_date"] or "")[:10])
    seen = f", discovered {event_date}" if event_date else ""

    resolver = EntityResolver(conn)   # per event; watchlist is small and the
    #                                   run holds the single-writer lock, so it
    #                                   always reflects the current entities.
    res = resolver.resolve(name=victim)

    if res.status == "review":
        # Ambiguous / collision / below-threshold: never auto-fire (R6.2) and,
        # per Kevin's queue-only rule, never mint a peer card either. Enqueue
        # here so no injected leak-site context can upgrade it downstream.
        _enqueue_review_once(conn, raw["raw_event_id"], res)
        return []

    if res.status == "matched":
        _record_decision_once(conn, raw["raw_event_id"], res)
        leak = (f"the {group} ransomware leak site" if group
                else "a ransomware leak site")
        evidence = [{
            "text": f"ransomware.live lists \"{victim}\" as a victim on {leak}{seen}.",
            "locator": "victim"}]
        return [{
            "trigger_id": "own_incident",
            "signal_scope": "account",
            "entity_id": res.entity_id,     # authoritative: no framework re-resolve
            "entity_name_hint": None,
            "event_date": event_date,
            "headline": f"Listed on {leak} - unverified early warning",
            "evidence": evidence,
            "confidence": min(OWN_CONFIDENCE, res.confidence),
            "incident_evidence_level": TIER,
        }]

    # status == "none": off-list victim -> class-level sector card, but only if
    # the tracker places the victim in the watchlist's industry. A peer card
    # asserts "sector peer"; an off-industry victim would make that a claim the
    # source never supports (R4.1), so it mints nothing.
    if not is_peer_industry(payload.get("activity")):
        return []

    # Name-free in the strong sense - neither victim NOR the attacker-controlled
    # group string is printed, so the card leaks no company identity (R4.1).
    evidence = [{
        "text": f"ransomware.live lists an organization on a ransomware leak site{seen}.",
        "locator": "source"}]
    return [{
        "trigger_id": "peer_incident",
        "signal_scope": "sector",
        "entity_id": None,
        "entity_name_hint": None,
        "event_date": event_date,
        "headline": "Sector peer listed on a ransomware leak site - unverified early warning",
        "evidence": evidence,
        "confidence": PEER_CONFIDENCE,
        "incident_evidence_level": TIER,
    }]


SOURCES = {SOURCE_ID: classify_ransomware}


if __name__ == "__main__":
    sys.exit(classify_runner.cli(
        CLASSIFIER_ID, SOURCES, PARSER_VERSION,
        "Classify ransomware.live victims into own/peer unconfirmed "
        "early-warning incident signals."))
