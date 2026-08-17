"""Regulatory obligation derivation (R8.3, R7.2, R8.4, R4.1).

Materializes one ``regulatory_obligations`` row per stored regulatory signal
that carries a durable compliance clock:

    python -m app.obligations

The table has existed since 0001 with no writer at all. This is its producer,
and it is the backend the Account 360 Compliance Calendar reads.

How an obligation relates to entities
    It does NOT key to one. No signal in the store has ever carried an
    entity_id - every regulatory signal is sector- or regulatory-calendar-
    scoped (R7.2) - so an account-keyed obligation would produce zero rows
    forever. The 0001 schema already says so: the table has no entity column,
    it has ``affected_scope`` + ``applicability_rule``.

    So an obligation is stored once per rule, class-scoped, and the reader
    joins it to accounts through ``applicability_rule``, a machine-readable
    predicate of the form ``subsector_in:a;b;c`` over
    watchlist_entities.subsector, optionally narrowed by a second clause
    ``|exclude_entities:E0001;E0002`` naming the individual entity_ids the
    subsector label over-admits. Both sets live in APPLICABILITY below, keyed
    by trigger.

    That predicate is a FILTER, not a claim. NERC registration status is not
    in the store and is not derivable from it, so the affected_scope label
    says "registration not verified per account" and ``verified_at`` stays
    NULL on every derived row. The predicate's job is only to keep a FERC CIP
    deadline off a refiner's page - it never asserts that a matching account
    is a registered entity. The exclusion clause is the same filter at entity
    granularity: it removes accounts whose subsector label is the only reason
    they were admitted, and it likewise asserts nothing about the ones left.

Fields, and what stays NULL (R4.1: no field is invented)
    regulator       the matched agency's own ``name`` from the payload
    rule_name       the document's own title
    effective_date  the payload's ``effective_on`` - REQUIRED; a signal with
                    no effective date yields no obligation (it has no clock)
    compliance_date always NULL. FERC states compliance dates in the order
                    body, which is not in the fetched metadata. Guessing one
                    from effective_on would be a fabricated deadline.
    mapped_products cip_product_map product ids for every CIP-### standard
                    the document NAMES in its title/abstract; NULL when the
                    document names none. Topic-keyword matching ("supply
                    chain" -> CIP-013) is deliberately not done - it is an
                    inference, not a citation.
    verified_at     always NULL - nothing here was operator-verified.
    signal_id       the derivation anchor (0013), FK-enforced.

Idempotency (mirrors app/plays.py)
    obligation_id is derived from signal_id, and generation is insert-or-skip:
    a re-run over unchanged input adds zero rows and never overwrites an
    existing one. A later correction to the Federal Register payload does not
    rewrite a stored obligation; the stored row is what the operator saw.

Not included, deliberately
    nerc_enforcement signals - an enforcement notice is a past action, not a
    forward obligation. Proposed rules whose only date anchor is
    comments_close_on - a comment deadline is Regulatory Monitor material
    (R8.4), not a compliance clock.
"""
import json
import re
import sys
from datetime import datetime, timezone

from app.db.connection import get_connection
from app.ingest.runner import ingest_lock

OBLIGATION_ID_PREFIX = "obligation:"

_CIP_STD_RE = re.compile(r"\bCIP-(\d{3})\b")

# The predicate grammar app/ui/data.py reads back. Spelled out on both sides
# rather than imported, so the reader does not drag the ingestion lock into the
# UI import path; tests/test_obligations.py binds the two ends.
SUBSECTOR_RULE_PREFIX = "subsector_in:"
EXCLUDE_ENTITIES_RULE_PREFIX = "exclude_entities:"
RULE_CLAUSE_SEPARATOR = "|"

# trigger_id -> (agency slug that identifies the regulator in the payload,
#                affected_scope label, applicable watchlist subsectors,
#                entity_ids the subsector label over-admits).
#
# The sets are a coarse regulated-class filter, not a registration claim (see
# the module docstring). NERC CIP binds owners/operators of Bulk Electric
# System facilities, so the subsector set is every subsector whose label
# denotes an electric utility / generator / grid operator role; hydrocarbon and
# oilfield-services subsectors are out of scope for CIP. TSA pipeline security
# directives bind designated critical pipeline and LNG owner/operators.
#
# Two subsector labels do not survive that test on their own:
#   storage      one member, a battery-systems integrator - it supplies BES
#                assets, it does not own or operate them.
#   renewables   one label over two populations. The generators (IPP-style
#                project owners) plausibly own BES facilities; the panel,
#                inverter, tracker, fuel-cell and turbine manufacturers listed
#                below are equipment makers and installers. The seed CSV is
#                read-only and has no finer subsector, so the manufacturers are
#                named here as an entity-level exclusion.
# "Registration not verified" hedges uncertainty about a plausible operator;
# it does not license listing a category that has no BES role at all.
_CIP_NON_OPERATOR_RENEWABLES = (
    "E0155",  # First Solar - panel manufacturer
    "E0156",  # Enphase Energy - inverter manufacturer
    "E0158",  # Bloom Energy - fuel-cell manufacturer
    "E0159",  # Sunrun - residential solar installer
    "E0160",  # Array Technologies - tracker manufacturer
    "E0161",  # Plug Power - hydrogen fuel-cell manufacturer
    "E0164",  # GE Vernova - grid/turbine equipment manufacturer
)
APPLICABILITY = {
    "nerc_cip_revision": (
        "federal-energy-regulatory-commission",
        "NERC-registered Bulk Electric System owners and operators "
        "(registration not verified per account)",
        ("coop_distribution", "coop_gt", "coop_transmission", "federal",
         "federal_pma", "ipp", "iou_electric", "muni_public", "public",
         "renewables", "rto_iso", "state_authority", "state_owned"),
        _CIP_NON_OPERATOR_RENEWABLES,
    ),
    "tsa_security_directive": (
        "transportation-security-administration",
        "TSA-designated critical pipeline and LNG owners and operators "
        "(designation not verified per account)",
        ("lng", "midstream"),
        (),
    ),
}


def applicability_rule(subsectors, excluded_entities):
    """The stored predicate for one applicability entry.

    ``subsector_in:a;b;c``, plus ``|exclude_entities:E1;E2`` when the entry
    names entities the subsector labels over-admit. The clause is omitted
    entirely when there are none - an empty exclusion list is not written,
    because the reader treats one as malformed and fails closed.
    """
    rule = SUBSECTOR_RULE_PREFIX + ";".join(subsectors)
    if excluded_entities:
        rule += (RULE_CLAUSE_SEPARATOR + EXCLUDE_ENTITIES_RULE_PREFIX
                 + ";".join(excluded_entities))
    return rule


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _regulator(payload, slug):
    """The matched agency's own ``name`` field - never a name we compose."""
    for agency in payload.get("agencies") or []:
        if isinstance(agency, dict) and agency.get("slug") == slug:
            return agency.get("name") or None
    return None


def _mapped_products(products_by_standard, text):
    """';'-joined product ids for every CIP standard the document NAMES.

    Returns None when the document names no CIP-### standard - an honest
    empty is better than a topic-keyword guess (R4.1).
    """
    ids = []
    for match in _CIP_STD_RE.finditer(text):
        for product_id in products_by_standard.get(
                f"CIP-{match.group(1)}", ()):
            if product_id not in ids:
                ids.append(product_id)
    return ";".join(ids) or None


def derive_obligations(conn):
    """Insert-or-skip one obligation per qualifying regulatory signal.

    Returns {obligations_new, obligations_existing, signals_seen,
    skipped_no_clock}.
    """
    products_by_standard = {}
    for r in conn.execute(
            "SELECT cip_standard, product_ids FROM cip_product_map "
            "ORDER BY cip_standard"):
        products_by_standard[r["cip_standard"]] = [
            p for p in (r["product_ids"] or "").split(";") if p]

    existing = {r["obligation_id"] for r in conn.execute(
        "SELECT obligation_id FROM regulatory_obligations")}

    counts = {"obligations_new": 0, "obligations_existing": 0,
              "signals_seen": 0, "skipped_no_clock": 0}
    now = _utcnow()
    rows = conn.execute(
        "SELECT s.signal_id, s.trigger_id, s.source_url, r.payload "
        "FROM signals s JOIN raw_events r "
        " ON r.raw_event_id = s.raw_event_id "
        "WHERE s.status = 'active' AND s.trigger_id IN ({}) "
        "ORDER BY s.signal_id".format(
            ", ".join("?" * len(APPLICABILITY))),
        tuple(sorted(APPLICABILITY))).fetchall()

    for sig in rows:
        counts["signals_seen"] += 1
        obligation_id = OBLIGATION_ID_PREFIX + sig["signal_id"]
        if obligation_id in existing:
            counts["obligations_existing"] += 1
            continue
        try:
            payload = json.loads(sig["payload"] or "")
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            counts["skipped_no_clock"] += 1
            continue
        # A durable compliance clock is the whole point of the row (R7.2).
        effective_date = payload.get("effective_on") or None
        rule_name = (payload.get("title") or "").strip()
        (slug, affected_scope, subsectors,
         excluded_entities) = APPLICABILITY[sig["trigger_id"]]
        regulator = _regulator(payload, slug)
        if not (effective_date and rule_name and regulator):
            counts["skipped_no_clock"] += 1
            continue
        conn.execute(
            "INSERT INTO regulatory_obligations (obligation_id, source_url, "
            " regulator, rule_name, affected_scope, applicability_rule, "
            " effective_date, compliance_date, mapped_products, verified_at, "
            " signal_id, derived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)",
            (obligation_id, sig["source_url"], regulator, rule_name,
             affected_scope,
             applicability_rule(subsectors, excluded_entities),
             effective_date,
             _mapped_products(
                 products_by_standard,
                 f"{rule_name} {payload.get('abstract') or ''}"),
             sig["signal_id"], now))
        existing.add(obligation_id)
        counts["obligations_new"] += 1
    conn.commit()
    return counts


def main():
    with ingest_lock():
        conn = get_connection()
        try:
            c = derive_obligations(conn)
        finally:
            conn.close()
    print(f"obligations: success new={c['obligations_new']} "
          f"existing={c['obligations_existing']} "
          f"signals_seen={c['signals_seen']} "
          f"skipped_no_clock={c['skipped_no_clock']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
