"""License facts + play candidates from seeded config (R7.5, R7.6, R7.7).

Transforms the seeded ``license_matrix`` staging rows into normalized
``license_facts`` and generates ``license_play_candidates`` from
``indicator_map``. Rebuild is a pure function of seeded config and is safe
to re-run:

  python -m app.licensing

SKU -> product family
    ``license_matrix.product_id`` holds granular SKU ids
    (``defender_endpoint_p1``) while ``license_facts.product_id`` and
    ``license_play_candidates.product_id`` FK-reference ``products``
    (family ids like ``def_endpoint``). SKU_FAMILY maps every matrix SKU to
    its family; ``suite_*`` baseline suites map to None (they are licensing
    context, not catalog products) and land with a NULL product_id. The SKU
    identity stays queryable: ``fact_id = "{sku}:{tier}:{segment}"`` and
    ``sku_or_plan = "{sku}:{tier}"`` (the matrix primary key). A matrix SKU
    missing from SKU_FAMILY is a hard error so new rows never drop silently.

gcc_high segment mapping (conservative and lossless)
    Every matrix row yields one ``segment='commercial'`` fact whose
    price_note is the matrix addon_price_note. The freeform ``gcc_high``
    cell additionally yields one ``segment='gcc_high'`` fact whose
    price_note prefixes the verbatim cell text:
      - cell starts with "yes" (any case)           -> "available — {raw}"
      - cell is "no" or starts with "no "/"no("     -> "not available — {raw}"
        (unavailability is a fact; R7.10 suppression reads it)
      - anything else (limited/partial/unknown/n-a/
        "Azure Gov path only"/prose)                -> "needs_curation — {raw}"
      - empty cell                                  -> no gcc_high fact
    The raw cell is always preserved verbatim after the prefix.

Play candidates
    One candidate per indicator_map row: ``play_id = "{trigger}:{family}"``,
    assumed_baseline 'E3' (the E5 Security add-on is the default first
    step-up play), recommended_path taken from the family's canonical play
    SKU in FAMILY_PLAY_SKU — chosen as the SKU/tier whose upgrade_path best
    describes the entry step-up from an E3 baseline. The discovery question
    is conditional and never asserts the account's current tier (R7.7).

Ownership: this module is the transform writer of ``license_facts`` (no
config_version column there). rebuild upserts every transform-produced fact in
place, refreshing ONLY the matrix-derived columns and EXCLUDING every column in
EDITABLE_FACT_COLS, so an operator's Admin edit (price_note / verified_date /
...) is never clobbered (Pattern B). Operator-added facts (fact_ids the
transform never produces) are left untouched. Transform-owned facts that stop
being produced are dropped only when no snapshot references them (mirrors the
candidate keep-referenced logic); a fact cited by a snapshot is always kept.
Candidates are upserted in place (play_ids are deterministic), never
delete+regenerated: ``license_play_snapshots.play_id`` FK-references
candidates, so a delete would crash the refresh on any DB that has ever
generated a card (R7.6 — old cards must keep explaining themselves).
Candidates that stop being generated are removed only when no snapshot
references them; referenced stale ones are kept as pinned history and
reported. After a rebuild the summary also reports snapshot fact_ids that
no longer resolve (a SKU/tier rename orphans pinned evidence — loud, not
silent). R7.10/R7.12 gating lives in app/plays.py — this is the data layer.
"""
import json
import sys

from app.db.connection import get_connection

CONFIG_VERSION = "licensing/1.0"

# Fact columns an operator may edit in the Admin license-fact editor (R8.7).
# Canonical here (a low-level module with no UI deps) so app.ui.data imports it
# without a cycle. rebuild() EXCLUDES these from its ON CONFLICT refresh so an
# operator edit survives a rebuild (Pattern B, the license-facts analogue of the
# seed loader's update_cols). fact_id/product_id/sku_or_plan/segment encode the
# fact's matrix identity and are never editable.
EDITABLE_FACT_COLS = (
    "price_note", "included_or_addon", "prerequisite", "effective_date",
    "verified_date", "source_quality", "source_url",
)

# Matrix SKU id -> products.product_id family (None = baseline suite, kept
# with NULL product_id). Every license_matrix.product_id must appear here.
SKU_FAMILY = {
    # baseline suites: licensing context, not catalog products
    "suite_m365_e3": None,
    "suite_m365_e5": None,
    "suite_m365_e7": None,
    "suite_e5_security_addon": None,
    "suite_e5_compliance_addon": None,
    "suite_business_premium": None,
    "suite_f3": None,
    "defender_endpoint_p1": "def_endpoint",
    "defender_endpoint_p2": "def_endpoint",
    "defender_for_business": "def_endpoint",   # SMB endpoint SKU
    "defender_vuln_mgmt": "def_endpoint",      # TVM is in def_endpoint keywords
    "defender_identity": "def_identity",
    "defender_office365_p1": "def_office",
    "defender_office365_p2": "def_office",
    "defender_cloud_apps": "def_cloudapps",
    "defender_cloud_cspm": "def_cloud",
    "defender_cloud_servers_p1": "def_cloud",
    "defender_cloud_servers_p2": "def_cloud",
    "defender_cloud_sql": "def_cloud",
    "defender_cloud_containers": "def_cloud",
    "defender_iot_ot_xs": "def_iot",
    "defender_iot_ot_s": "def_iot",
    "defender_iot_ot_m": "def_iot",
    "defender_iot_ot_l": "def_iot",
    "defender_iot_ot_xl": "def_iot",
    "defender_iot_eiot": "def_iot",
    "sentinel_analytics_payg": "sentinel",
    "sentinel_analytics_commit": "sentinel",
    "sentinel_datalake": "sentinel",
    "sentinel_e5_grant": "sentinel",
    "security_copilot_included": "sec_copilot",
    "security_copilot_scu": "sec_copilot",
    # Entra ID P1/P2 are baseline identity tiers; entra_suite is the nearest
    # catalog family (no standalone Entra ID product in products.csv)
    "entra_id_p1": "entra_suite",
    "entra_id_p2": "entra_suite",
    "entra_suite": "entra_suite",
    "entra_id_governance": "entra_suite",
    "entra_internet_access": "entra_suite",
    "entra_private_access": "entra_suite",
    "entra_agent_id": "entra_agentid",
    "agent_365": "agent365",
    "purview_e5_compliance": "purview",
    "purview_dspm_ai": "purview",
    "purview_payg_datagov": "purview",
    "purview_payg_irm_ai": "purview",
    "intune_p1": "intune",
    "intune_p2": "intune",
    "intune_suite": "intune",
    "intune_device": "intune",
    "defender_easm": "def_easm",
    "defender_ti_free": "def_ti",
    "defender_ti_premium": "def_ti",
    "ghas_secret_protection": "github_as",
    "ghas_code_security": "github_as",
    "ghas_both": "github_as",
}

# family -> (matrix SKU, tier) whose upgrade_path is the recommended entry
# step-up from an E3 baseline. Every family used in indicator_map needs one.
FAMILY_PLAY_SKU = {
    "def_endpoint": ("defender_endpoint_p2", "e5"),      # E3 -> E5 Security addon
    "def_identity": ("defender_identity", "e5"),         # E3 -> E5 Security addon
    # entry OT site size; its upgrade_path describes the purchase mechanics
    "def_iot": ("defender_iot_ot_xs", "site_license"),
    "def_cloud": ("defender_cloud_servers_p2", "consumption"),  # flagship CWP tier
    "def_easm": ("defender_easm", "consumption"),
    "def_ti": ("defender_ti_premium", "standalone"),     # paid TI workspace
    "sentinel": ("sentinel_analytics_payg", "consumption"),  # start PAYG, commit later
    "sec_copilot": ("security_copilot_included", "e5"),  # E5 entitlement first
    "entra_suite": ("entra_suite", "addon"),
    "entra_agentid": ("entra_agent_id", "e7"),
    "agent365": ("agent_365", "addon"),
    "purview": ("purview_e5_compliance", "e5"),          # E3 -> E5 Compliance addon
}

# unambiguous tier -> prerequisite; everything else stays ''
TIER_PREREQUISITE = {
    "addon_to_e3": "E3",
    "addon_to_mdep2": "MDE P2",
}

DISCOVERY_QUESTION = (
    "What licensing tier are you on today — E3 or E5? "
    "If E3, the typical next step for {product} is: {path}."
)


def _snapshot_cited_fact_ids(conn):
    """Set of fact_ids cited by any license_play_snapshots row. fact_ids is a
    JSON array TEXT (not a real FK), so this hand-scans and json.loads each,
    guarding malformed JSON the same way signal_detail does."""
    cited = set()
    for r in conn.execute("SELECT fact_ids FROM license_play_snapshots"):
        try:
            ids = json.loads(r["fact_ids"] or "[]")
        except ValueError:
            ids = []
        if isinstance(ids, list):
            cited.update(ids)
    return cited


def gcc_high_note(cell):
    """Map the freeform gcc_high cell to a gcc_high-segment price_note, or
    None for an empty cell. Lossless: the raw cell follows the prefix."""
    raw = (cell or "").strip()
    if not raw:
        return None
    low = raw.lower()
    if low.startswith("yes"):
        return f"available — {raw}"
    if low == "no" or low.startswith("no ") or low.startswith("no("):
        return f"not available — {raw}"
    return f"needs_curation — {raw}"


def build_facts(matrix_rows):
    """license_facts rows (as tuples in column order) from matrix rows."""
    facts = []
    unmapped = sorted({r["product_id"] for r in matrix_rows
                       if r["product_id"] not in SKU_FAMILY})
    if unmapped:
        raise ValueError(
            f"license_matrix product_id(s) missing from SKU_FAMILY: "
            f"{unmapped}. Map them (or None for suites) in app/licensing.py.")
    for r in matrix_rows:
        sku, tier = r["product_id"], r["tier"]
        family = SKU_FAMILY[sku]
        prerequisite = TIER_PREREQUISITE.get(tier, "")
        common = (family, f"{sku}:{tier}", r["included_or_addon"],
                  prerequisite, "", r["verified_date"], r["source_quality"],
                  r["source_url"])
        facts.append((f"{sku}:{tier}:commercial", "commercial",
                      r["addon_price_note"]) + common)
        note = gcc_high_note(r["gcc_high"])
        if note is not None:
            facts.append((f"{sku}:{tier}:gcc_high", "gcc_high", note) + common)
    return facts


def rebuild(conn):
    """Regenerate this config version's license_facts and
    license_play_candidates from the seeded config, preserving operator edits.
    Returns counts.

    license_facts is upserted in place: each transform-produced fact refreshes
    ONLY its matrix identity columns (product_id, sku_or_plan, segment) and
    EXCLUDES every EDITABLE_FACT_COL, so an operator's Admin edit is never
    clobbered (Pattern B). Operator-added facts (sku_or_plan IS NULL, never in
    the transform output) are left untouched. Transform-owned facts that stop
    being produced are dropped only when no snapshot cites them.

    Tradeoff: because the EDITABLE_FACT_COLs are frozen on an existing fact, a
    seed CONTENT correction to one of those columns (e.g. a new price_note or
    verified_date in license_matrix.csv) does NOT propagate to an already-existing
    fact on rebuild - the frozen value protects operator edits. A from-scratch
    rebuild (empty license_facts) yields pristine matrix values for every fact."""
    matrix_rows = conn.execute(
        "SELECT product_id, tier, included_or_addon, addon_price_note, "
        " upgrade_path, gcc_high, source_quality, verified_date, source_url "
        "FROM license_matrix ORDER BY product_id, tier").fetchall()
    facts = build_facts(matrix_rows)

    # Upsert in place. The DO UPDATE refreshes only the matrix-derived identity
    # columns (product_id, sku_or_plan, segment); every EDITABLE_FACT_COL is
    # omitted from the SET clause, so an operator edit survives a rebuild.
    for (fact_id, segment, price_note, family, sku_or_plan, included_or_addon,
         prerequisite, effective_date, verified_date, source_quality,
         source_url) in facts:
        conn.execute(
            "INSERT INTO license_facts (fact_id, product_id, sku_or_plan, "
            " segment, price_note, included_or_addon, prerequisite, "
            " effective_date, verified_date, source_quality, source_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(fact_id) DO UPDATE SET product_id=excluded.product_id, "
            " sku_or_plan=excluded.sku_or_plan, segment=excluded.segment",
            (fact_id, family, sku_or_plan, segment, price_note,
             included_or_addon, prerequisite, effective_date, verified_date,
             source_quality, source_url))

    # Drop transform-owned facts (sku_or_plan IS NOT NULL) that this rebuild no
    # longer produced AND that no snapshot cites - mirrors the stale-candidate
    # keep-referenced logic below. Operator-added facts (sku_or_plan IS NULL)
    # are never dropped here.
    produced_fact_ids = {f[0] for f in facts}
    snapshot_cited = _snapshot_cited_fact_ids(conn)
    for r in conn.execute(
            "SELECT fact_id FROM license_facts "
            "WHERE sku_or_plan IS NOT NULL ORDER BY fact_id").fetchall():
        fid = r["fact_id"]
        if fid in produced_fact_ids or fid in snapshot_cited:
            continue
        conn.execute("DELETE FROM license_facts WHERE fact_id = ?", (fid,))

    upgrade_paths = {(r["product_id"], r["tier"]): r["upgrade_path"]
                     for r in matrix_rows}
    product_names = {r["product_id"]: r["name"] for r in conn.execute(
        "SELECT product_id, name FROM products")}
    indicator_rows = conn.execute(
        "SELECT trigger_id, product_id FROM indicator_map "
        "ORDER BY trigger_id, product_id").fetchall()

    # Upsert candidates in place: snapshots FK-reference play_id, so a
    # delete+regenerate would crash on any DB that has generated a card.
    generated_ids = set()
    for r in indicator_rows:
        family = r["product_id"]
        if family not in FAMILY_PLAY_SKU:
            raise ValueError(
                f"indicator_map family {family!r} has no FAMILY_PLAY_SKU "
                f"entry in app/licensing.py.")
        sku_tier = FAMILY_PLAY_SKU[family]
        if sku_tier not in upgrade_paths:
            raise ValueError(
                f"FAMILY_PLAY_SKU[{family!r}] = {sku_tier!r} not found in "
                f"license_matrix.")
        path = upgrade_paths[sku_tier]
        play_id = f"{r['trigger_id']}:{family}"
        generated_ids.add(play_id)
        conn.execute(
            "INSERT INTO license_play_candidates (play_id, trigger_id, "
            " product_id, assumed_baseline, recommended_path, "
            " discovery_question, rank_rule, config_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(play_id) DO UPDATE SET trigger_id=excluded.trigger_id, "
            " product_id=excluded.product_id, "
            " assumed_baseline=excluded.assumed_baseline, "
            " recommended_path=excluded.recommended_path, "
            " discovery_question=excluded.discovery_question, "
            " rank_rule=excluded.rank_rule, "
            " config_version=excluded.config_version",
            (play_id, r["trigger_id"], family, "E3", path,
             DISCOVERY_QUESTION.format(path=path,
                                       product=product_names[family]),
             "", CONFIG_VERSION))

    # Stale candidates: drop the unreferenced, keep+report the pinned.
    stale_kept = []
    for r in conn.execute(
            "SELECT play_id FROM license_play_candidates ORDER BY play_id"):
        if r["play_id"] in generated_ids:
            continue
        referenced = conn.execute(
            "SELECT 1 FROM license_play_snapshots WHERE play_id = ? LIMIT 1",
            (r["play_id"],)).fetchone()
        if referenced:
            stale_kept.append(r["play_id"])
        else:
            conn.execute(
                "DELETE FROM license_play_candidates WHERE play_id = ?",
                (r["play_id"],))

    # Integrity sweep: snapshot fact_ids must still resolve in license_facts
    # after the rebuild (a SKU/tier rename orphans pinned evidence). Checked
    # against the live table, so an operator-added cited fact resolves too.
    live_fact_ids = {r["fact_id"] for r in conn.execute(
        "SELECT fact_id FROM license_facts")}
    orphaned_fact_refs = 0
    for r in conn.execute("SELECT fact_ids FROM license_play_snapshots"):
        try:
            ids = json.loads(r["fact_ids"] or "[]")
        except ValueError:
            ids = []
        if not isinstance(ids, list):
            ids = []
        orphaned_fact_refs += sum(1 for i in ids if i not in live_fact_ids)
    conn.commit()

    by_segment = {}
    for f in facts:
        by_segment[f[1]] = by_segment.get(f[1], 0) + 1
    return {"facts": len(facts), "facts_by_segment": by_segment,
            "candidates": len(generated_ids), "stale_kept": stale_kept,
            "orphaned_fact_refs": orphaned_fact_refs}


def main():
    conn = get_connection()
    try:
        counts = rebuild(conn)
    finally:
        conn.close()
    seg = ", ".join(f"{k}={v}" for k, v in sorted(
        counts["facts_by_segment"].items()))
    print(f"license_facts: {counts['facts']} ({seg})")
    print(f"license_play_candidates: {counts['candidates']} "
          f"(config_version={CONFIG_VERSION})")
    if counts["stale_kept"]:
        print(f"WARNING: {len(counts['stale_kept'])} stale candidate(s) kept "
              f"(referenced by snapshots): {counts['stale_kept']}")
    if counts["orphaned_fact_refs"]:
        print(f"WARNING: {counts['orphaned_fact_refs']} snapshot fact "
              f"reference(s) no longer resolve in license_facts (SKU/tier "
              f"rename?) - pinned evidence is orphaned.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
