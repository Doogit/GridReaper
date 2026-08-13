"""License-play snapshot generation (R7.6, R7.9, R7.10, R7.11).

Materializes one ``license_play_snapshots`` row per (signal, matching
license_play_candidate), pinning the play text and its licensing evidence
basis at generation time:

  python -m app.plays

Immutability (R7.6)
    An existing (signal_id, play_id) row is never overwritten — not by
    re-runs and not by later runs with a different generation_version.
    Generation is insert-or-skip, so re-runs are no-ops.

Gov-cloud gating (R7.10)
    For signals WITH an entity, a play whose candidate family is in
    GOV_UNAVAILABLE_FAMILIES (sec_copilot) is suppressed — no row is
    written — when the gov predicate holds:

        entity.tenant_cloud_environment in {'gcc','gcc_high','dod','azure_gov'}
        OR entity.gov_cloud_likelihood == 'likely'

    The row's absence plus the family's "not available — ..." gcc_high
    license_fact is the audit trail. Signals without an entity (sector /
    regulatory_calendar scope) are never suppressed but stay sector-phrased.

Text construction
    display_text (operator-facing): "Recommended path: ..." and
    "Discovery: ..." lines from the candidate, plus a "Gov-cloud caution:"
    line when the entity is gov-possible (tenant_cloud_environment in
    GOV_CLOUD_ENVIRONMENTS OR gov_cloud_likelihood in {'likely','possible'})
    and the family carries a "not available — ..." gcc_high fact.

    outreach_safe_text (R7.11, customer-facing): scope-phrased lead-in plus
    the candidate's conditional discovery question. The sector lead-in
    quotes the signal's own sourced headline and event date — outreach
    never invents sector color; every clause traces to a stored row. It
    never interpolates any license_facts.price_note (so price strings from
    non-primary facts can never leak), never asserts the account's current
    tier (conditional discovery phrasing only, R7.7), and signals whose
    scope != 'account' carry no account-specific wording. Prerequisites are
    never stated as recommendations — the only prerequisite-touching
    content is the candidate's discovery question, which already models
    prerequisites as discovery (R7.9, agent365 family included).

fact_ids pins the evidence basis: every license_facts row whose product_id
is the candidate's family (commercial and gcc_high segments), ordered by
fact_id. generated_at is bookkeeping only, not identity.
"""
import json
import sys
from datetime import datetime, timezone

from app.db.connection import get_connection
from app.ingest.runner import ingest_lock

GENERATION_VERSION = "plays/1.0"

# R7.10: tenant environments where commercial-only SKUs cannot land
GOV_CLOUD_ENVIRONMENTS = {"gcc", "gcc_high", "dod", "azure_gov"}
# families suppressed under the gov predicate (gcc_high "not available")
GOV_UNAVAILABLE_FAMILIES = {"sec_copilot"}
# likelihoods meriting an operator caution line (broader than suppression)
GOV_POSSIBLE_LIKELIHOODS = {"likely", "possible"}
NOT_AVAILABLE_PREFIX = "not available — "

# Outreach lead-ins reference the signal's own sourced event (headline +
# event date) - never invented sector color: every clause in outreach text
# must trace to a stored, sourced row (R4.1 discipline applied to drafts).
ACCOUNT_OUTREACH = (
    "Given the recent development at your organization, a licensing check "
    "may be timely. {question}")
SECTOR_OUTREACH = (
    "{event}. In that context, a {product} licensing check may be timely "
    "for affected operators. {question}")


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def gov_suppressed(entity):
    """R7.10 suppression predicate (exact): tenant_cloud_environment in
    GOV_CLOUD_ENVIRONMENTS OR gov_cloud_likelihood == 'likely'."""
    return ((entity["tenant_cloud_environment"] or "")
            in GOV_CLOUD_ENVIRONMENTS
            or (entity["gov_cloud_likelihood"] or "") == "likely")


def _gov_possible(entity):
    """Caution predicate: any gov tenant environment, or gov_cloud_likelihood
    'likely'/'possible'. Wider than suppression on purpose — a 'possible'
    account keeps the play but the operator sees the availability caveat."""
    return ((entity["tenant_cloud_environment"] or "")
            in GOV_CLOUD_ENVIRONMENTS
            or (entity["gov_cloud_likelihood"] or "")
            in GOV_POSSIBLE_LIKELIHOODS)


def _display_text(cand, product, entity, not_available_fact):
    lines = [f"Recommended path: {cand['recommended_path']}",
             f"Discovery: {cand['discovery_question']}"]
    if (entity is not None and not_available_fact is not None
            and _gov_possible(entity)):
        lines.append(
            f"Gov-cloud caution: {product} gcc_high fact reads "
            f"\"{not_available_fact['price_note']}\" — confirm the tenant "
            f"environment before positioning.")
    return "\n".join(lines)


def _outreach_safe_text(cand, product, sig):
    # R7.12: an incident not cleared for customer-facing use
    # (customer_facing_allowed=0, e.g. an unconfirmed early warning) suppresses
    # the OUTREACH OPENER only - the play/snapshot is still generated so the
    # operator keeps the internal recommendation. The verification-first
    # replacement asserts nothing unsourced (R4.1 governs drafts too).
    if not sig["customer_facing_allowed"]:
        return ("Verification-first: this is an unverified early warning - no "
                "customer-facing opener until the incident is confirmed by the "
                "company, a regulator, or an SEC filing.")
    if sig["signal_scope"] == "account":
        return ACCOUNT_OUTREACH.format(question=cand["discovery_question"])
    event = sig["headline"] or "A recent regulatory development"
    if sig["event_date"]:
        event += f" ({sig['event_date']})"
    return SECTOR_OUTREACH.format(event=event, product=product,
                                  question=cand["discovery_question"])


def generate_snapshots(conn, generation_version=GENERATION_VERSION):
    """Insert-or-skip snapshots for every signal x matching candidate.
    Returns {snapshots_new, snapshots_existing, suppressed_gov,
    signals_seen}."""
    candidates_by_trigger = {}
    for r in conn.execute(
            "SELECT play_id, trigger_id, product_id, recommended_path, "
            " discovery_question FROM license_play_candidates "
            "ORDER BY play_id"):
        candidates_by_trigger.setdefault(r["trigger_id"], []).append(r)

    facts_by_family = {}
    for r in conn.execute(
            "SELECT fact_id, product_id, segment, price_note "
            "FROM license_facts WHERE product_id IS NOT NULL "
            "ORDER BY fact_id"):
        facts_by_family.setdefault(r["product_id"], []).append(r)

    product_names = {r["product_id"]: r["name"] for r in conn.execute(
        "SELECT product_id, name FROM products")}
    entities = {r["entity_id"]: r for r in conn.execute(
        "SELECT entity_id, tenant_cloud_environment, gov_cloud_likelihood "
        "FROM watchlist_entities")}
    existing = {(r["signal_id"], r["play_id"]) for r in conn.execute(
        "SELECT signal_id, play_id FROM license_play_snapshots")}

    counts = {"snapshots_new": 0, "snapshots_existing": 0,
              "suppressed_gov": 0, "signals_seen": 0}
    now = _utcnow()
    for sig in conn.execute(
            "SELECT signal_id, entity_id, signal_scope, trigger_id, "
            " headline, event_date, customer_facing_allowed "
            "FROM signals ORDER BY signal_id").fetchall():
        counts["signals_seen"] += 1
        entity = entities.get(sig["entity_id"]) if sig["entity_id"] else None
        for cand in candidates_by_trigger.get(sig["trigger_id"], []):
            if (sig["signal_id"], cand["play_id"]) in existing:
                counts["snapshots_existing"] += 1
                continue
            family = cand["product_id"]
            if (entity is not None and family in GOV_UNAVAILABLE_FAMILIES
                    and gov_suppressed(entity)):
                counts["suppressed_gov"] += 1
                continue
            family_facts = facts_by_family.get(family, [])
            not_available_fact = next(
                (f for f in family_facts if f["segment"] == "gcc_high"
                 and (f["price_note"] or "").startswith(
                     NOT_AVAILABLE_PREFIX)), None)
            product = product_names.get(family, family)
            conn.execute(
                "INSERT INTO license_play_snapshots (signal_id, play_id, "
                " fact_ids, generated_at, generation_version, display_text, "
                " outreach_safe_text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sig["signal_id"], cand["play_id"],
                 json.dumps([f["fact_id"] for f in family_facts]),
                 now, generation_version,
                 _display_text(cand, product, entity, not_available_fact),
                 _outreach_safe_text(cand, product, sig)))
            counts["snapshots_new"] += 1
    conn.commit()
    return counts


def main():
    with ingest_lock():
        conn = get_connection()
        try:
            s = generate_snapshots(conn)
        finally:
            conn.close()
    print(f"plays: success new={s['snapshots_new']} "
          f"existing={s['snapshots_existing']} "
          f"suppressed_gov={s['suppressed_gov']} "
          f"signals_seen={s['signals_seen']} "
          f"generation_version={GENERATION_VERSION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
