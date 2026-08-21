"""GridSignals seed loader.

Applies schema migrations, then upserts the config CSVs from seeds/ into
their tables. Idempotent: re-running refreshes config rather than
duplicating or erroring, and never clobbers runtime-managed columns
(e.g. source_policies.enabled). Run as: python -m app.db.load_seeds
"""
import csv
import json
import os
import sys

from app.db.connection import get_connection
from app.db.migrate import apply_migrations

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SEEDS_DIR = os.path.join(REPO_ROOT, "seeds")
WATCHLIST_ALIAS_SOURCE = "watchlist_entities.csv"
WATCHLIST_COLLISION_REASON = "seeded from watchlist_entities.csv"
GENERATED_RELATIONSHIP_SOURCE = "gleif"

# R7.2 allowed signal scopes for the MVP triggers. Hand-configured here, not
# a CSV column: applied after the triggers upsert on every load, so a
# fresh-from-seeds rebuild gets them too (a migration UPDATE would run before
# trigger rows exist and be silently lost).
TRIGGER_SCOPES = {
    "leadership_change": ["account"],
    "nerc_enforcement": ["account", "sector"],
    "nerc_cip_revision": ["sector", "regulatory_calendar"],
    "tsa_security_directive": ["sector", "regulatory_calendar"],
    # Stage-2 incidents (R9.6): own_incident is the filer's own disclosure
    # (account); peer_incident is the same disclosure read as a sector-wide
    # class signal (sector, class-phrased, name-free).
    "own_incident": ["account"],
    "peer_incident": ["sector"],
    # R7 (EPA ECHO): genuine judicial-civil consent-decree records resolve to
    # one named defendant, always account-scoped -- no sector-level framing.
    "audit_consent_decree": ["account"],
    # R5 (USAspending.gov): a DOE/CISA/DHS grant or cooperative-agreement
    # award resolves to one named recipient, always account-scoped.
    "capital_project": ["account"],
    # R6 (PHMSA): enforcement cases resolve to one named midstream/LNG
    # operator, always account-scoped -- no sector-level analog defined.
    "pipeline_enforcement_action": ["account"],
}

# Loaded in FK order: products + triggers precede indicator_map.
# col_map renames a CSV column to its table column.
# skip_cols are CSV columns normalized into side tables, not loaded directly.
# update_cols (when set) limits which columns an upsert refreshes, so
# runtime-managed columns survive a reload.
TABLES = [
    {"table": "products", "csv": "products.csv",
     "pk": ["product_id"], "int_cols": ["energy_ot_flag"]},
    {"table": "triggers", "csv": "triggers.csv",
     "pk": ["trigger_id"],
     "int_cols": ["base_strength", "decay_half_life_days", "mvp_flag"],
     # decay_half_life_days is Admin-tunable (R8.7 half-life sliders) - once an
     # operator edits it, a reload must not clobber it back to the CSV value
     # (Pattern B, same guard as source_policies.enabled below). Sibling columns
     # still refresh from the seed CSV so seed corrections keep flowing in.
     "update_cols": ["name", "description", "base_strength", "mvp_flag",
                     "evidence_quality", "primary_sources"]},
    {"table": "indicator_map", "csv": "indicator_map.csv",
     "pk": ["trigger_id", "product_id"], "int_cols": [],
     "col_map": {"notes": "rationale"},
     # (column in this CSV, table whose PK it must exist in)
     "fk_checks": [("trigger_id", "triggers"), ("product_id", "products")]},
    {"table": "cip_product_map", "csv": "cip_product_map.csv",
     "pk": ["cip_standard"], "int_cols": []},
    {"table": "watchlist_entities", "csv": "watchlist_entities.csv",
     "pk": ["entity_id"], "int_cols": [],
     # normalized into entity_aliases / entity_collision_terms (R4.4)
     "skip_cols": ["aliases", "collision_terms"],
     # R8.7 watchlist manager: the operator edits the "curation" columns
     # (subsector, parent_id, richness, coverage_flag, gov_cloud_likelihood,
     # notes, owning_seller) at runtime, so a reload must not clobber those back
     # to the CSV (Pattern B, same guard as source_policies.enabled). Identifier
     # and name columns stay refreshable so seed corrections (CIK fixes, renames)
     # still flow on reload. The runtime columns active/origin (migration 0008)
     # are not in the CSV, so they are never in this SET clause regardless. A
     # fresh rebuild-from-seeds restores pristine curation values (replayable
     # from config_audit). Editable set mirrors data.EDITABLE_ENTITY_COLS.
     "update_cols": ["name", "cik", "lei", "wikidata_qid", "ticker"]},
    {"table": "entity_aliases", "csv": "entity_aliases.csv",
     "pk": ["entity_id", "alias"], "int_cols": [],
     "fk_checks": [("entity_id", "watchlist_entities")]},
    {"table": "entity_collision_terms", "csv": "entity_collision_terms.csv",
     "pk": ["entity_id", "term"], "int_cols": [],
     "fk_checks": [("entity_id", "watchlist_entities")]},
    {"table": "entity_relationships", "csv": "entity_relationships.csv",
     "pk": ["parent_entity_id", "child_entity_id", "relationship_type"],
     "int_cols": [],
     "fk_checks": [("parent_entity_id", "watchlist_entities"),
                   ("child_entity_id", "watchlist_entities")]},
    {"table": "license_matrix", "csv": "license_matrix.csv",
     "pk": ["product_id", "tier"], "int_cols": []},
    {"table": "scoring_weights", "csv": "scoring_weights.csv",
     # weight is REAL; loader passes CSV strings, SQLite REAL affinity coerces
     "pk": ["weight_kind", "key"], "int_cols": [],
     # weight is Admin-tunable (R8.7 weight sliders) - never clobber an operator
     # edit on reload (Pattern B). notes still refreshes from the seed CSV.
     "update_cols": ["notes"]},
    {"table": "source_policies", "csv": "source_policies.csv",
     "pk": ["source_id"], "int_cols": ["ttl", "enabled", "evidence_rank"],
     # enabled is admin/G2-managed after first insert - never clobber it. origin
     # (migration 0009) is a runtime/provenance column absent from the CSV, so it
     # is never in this SET clause (like watchlist active/origin) - operator-added
     # sources keep origin='operator' across reload. No seed-scoped DELETE here,
     # so operator-added source rows survive reload too (Pattern B, R9.5).
     "update_cols": ["name", "access_method", "ttl", "tos_status",
                     "evidence_rank", "rate_limit", "last_policy_review"]},
    # UI badge legend (R4.3/R8.1): plain upsert, FK-referenced by nothing.
    {"table": "badge_legend", "csv": "badge_legend.csv",
     "pk": ["badge_kind", "code"], "int_cols": []},
    # Audit golden set (R9.10): generated-for-review fixtures, plain upsert on
    # golden_id. No FKs, no int cols, no runtime-managed columns. JSON lives in
    # the signal_fixture / expected_results TEXT columns and loads verbatim.
    {"table": "audit_goldens", "csv": "audit_goldens.csv",
     "pk": ["golden_id"], "int_cols": []},
]


def read_rows(csv_path):
    """Return (header, list-of-dict-rows) for non-empty data rows."""
    if not os.path.exists(csv_path):
        # R4.5: missing seed files fail the seed job with a clear error
        raise FileNotFoundError(
            f"Required seed file missing: {csv_path}. "
            f"Restore it under seeds/ and re-run."
        )
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames
        rows = [r for r in reader if any((v or "").strip() for v in r.values())]
    return header, rows


def coerce(value, is_int):
    """Empty cells -> empty string (or 0 for int columns)."""
    if is_int:
        v = (value or "").strip()
        return int(v) if v else 0
    return value if value is not None else ""


def parse_list(cell):
    """Seed list cells may be a JSON array, semicolon-separated, or a single
    value. Returns a list of non-empty strings."""
    cell = (cell or "").strip()
    if not cell:
        return []
    if cell.startswith("["):
        try:
            return [str(v).strip() for v in json.loads(cell) if str(v).strip()]
        except ValueError:
            pass
    return [p.strip() for p in cell.split(";") if p.strip()]


def build_upsert(table, cols, pk, update_cols=None):
    placeholders = ",".join("?" * len(cols))
    if update_cols is None:
        update_cols = [c for c in cols if c not in pk]
    if update_cols:
        set_clause = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
        conflict = f"ON CONFLICT({','.join(pk)}) DO UPDATE SET {set_clause}"
    else:
        conflict = f"ON CONFLICT({','.join(pk)}) DO NOTHING"
    return (f"INSERT INTO {table} ({','.join(cols)}) "
            f"VALUES ({placeholders}) {conflict}")


def duplicated_values(rows, col):
    counts = {}
    for row in rows:
        value = (row.get(col) or "").strip()
        if value:
            counts[value] = counts.get(value, 0) + 1
    return {value for value, count in counts.items() if count > 1}


def apply_trigger_scopes(conn):
    """Set triggers.allowed_scopes for the MVP triggers (R7.2). Returns the
    number of trigger rows updated."""
    updated = 0
    for trigger_id, scopes in TRIGGER_SCOPES.items():
        cur = conn.execute(
            "UPDATE triggers SET allowed_scopes = ? WHERE trigger_id = ?",
            (json.dumps(scopes), trigger_id))
        updated += cur.rowcount
    return updated


def apply_identifiers(conn):
    """Fill cik / lei / wikidata_qid from the generated entity_identifiers.csv
    (written by app.enrich_entities). Fill-if-empty only: a non-empty value
    from the hand-verified watchlist CSV always wins over generated data.
    Duplicate generated identifiers are skipped because deterministic IDs
    must remain one-to-one for entity resolution.
    Returns (cik_filled, lei_filled, qid_filled, source_rows, duplicate_skips)."""
    _, rows = read_rows(os.path.join(SEEDS_DIR, "entity_identifiers.csv"))
    cik_filled = lei_filled = qid_filled = 0
    duplicate_skips = 0
    duplicate_ciks = duplicated_values(rows, "cik")
    duplicate_leis = duplicated_values(rows, "lei")
    duplicate_qids = duplicated_values(rows, "wikidata_qid")
    for row in rows:
        cur = conn.execute(
            "SELECT cik, lei, wikidata_qid FROM watchlist_entities "
            "WHERE entity_id = ?", (row["entity_id"],)).fetchone()
        if cur is None:
            continue
        cik = (row.get("cik") or "").strip()
        lei = (row.get("lei") or "").strip()
        qid = (row.get("wikidata_qid") or "").strip()
        if cik and cik in duplicate_ciks:
            duplicate_skips += 1
        elif cik and not (cur["cik"] or "").strip():
            conn.execute("UPDATE watchlist_entities SET cik=? WHERE entity_id=?",
                         (cik, row["entity_id"]))
            cik_filled += 1
        if lei and lei in duplicate_leis:
            duplicate_skips += 1
        elif lei and not (cur["lei"] or "").strip():
            conn.execute("UPDATE watchlist_entities SET lei=? WHERE entity_id=?",
                         (lei, row["entity_id"]))
            lei_filled += 1
        if qid and qid in duplicate_qids:
            duplicate_skips += 1
        elif qid and not (cur["wikidata_qid"] or "").strip():
            conn.execute(
                "UPDATE watchlist_entities SET wikidata_qid=? WHERE entity_id=?",
                (qid, row["entity_id"]))
            qid_filled += 1
    return cik_filled, lei_filled, qid_filled, len(rows), duplicate_skips


def load(db_path=None):
    conn = get_connection(db_path) if db_path else get_connection()
    pk_sets = {}      # table -> set of single-col PK values inserted (for FK checks)
    skipped = []      # (table, reason) for FK-skipped rows
    summary = []      # (table, loaded_n, source_m)
    aliases = []      # (entity_id, alias) split out of the watchlist CSV
    collisions = []   # (entity_id, term)
    mismatch = False

    try:
        apply_migrations(conn)

        conn.execute("BEGIN")
        for spec in TABLES:
            table = spec["table"]
            csv_path = os.path.join(SEEDS_DIR, spec["csv"])
            header, rows = read_rows(csv_path)
            col_map = spec.get("col_map", {})
            skip_cols = set(spec.get("skip_cols", []))
            csv_cols = [c for c in header if c not in skip_cols]
            cols = [col_map.get(c, c) for c in csv_cols]
            int_cols = set(spec["int_cols"])
            pk = spec["pk"]
            sql = build_upsert(table, cols, pk, spec.get("update_cols"))
            fk_checks = spec.get("fk_checks", [])

            if table == "entity_relationships":
                conn.execute("DELETE FROM entity_relationships WHERE source = ?",
                             (GENERATED_RELATIONSHIP_SOURCE,))

            loaded = 0
            for row in rows:
                skip = False
                for col, ref_table in fk_checks:
                    if row.get(col, "") not in pk_sets.get(ref_table, set()):
                        skipped.append((table, f"{col}={row.get(col, '')!r} "
                                               f"not in {ref_table}"))
                        skip = True
                        break
                if skip:
                    continue
                params = [coerce(row.get(c, ""), c in int_cols) for c in csv_cols]
                conn.execute(sql, params)
                loaded += 1
                if len(pk) == 1:
                    pk_sets.setdefault(table, set()).add(row.get(pk[0], ""))
                if table == "watchlist_entities":
                    eid = row["entity_id"]
                    aliases += [(eid, a) for a in parse_list(row.get("aliases"))]
                    collisions += [(eid, t)
                                   for t in parse_list(row.get("collision_terms"))]

            summary.append((table, loaded, len(rows)))

        # Normalized alias/collision rows split from the watchlist CSV (R4.4)
        conn.execute("DELETE FROM entity_aliases WHERE source = ?",
                     (WATCHLIST_ALIAS_SOURCE,))
        conn.execute("DELETE FROM entity_collision_terms WHERE reason = ?",
                     (WATCHLIST_COLLISION_REASON,))
        for eid, alias in aliases:
            conn.execute(
                "INSERT INTO entity_aliases (entity_id, alias, source) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(entity_id, alias) DO NOTHING",
                (eid, alias, WATCHLIST_ALIAS_SOURCE))
        for eid, term in collisions:
            conn.execute(
                "INSERT INTO entity_collision_terms (entity_id, term, reason) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(entity_id, term) DO NOTHING",
                (eid, term, WATCHLIST_COLLISION_REASON))

        # Generated identifier fills (after watchlist upsert so a reload's
        # empty CSV cells are refilled in the same transaction)
        ident_stats = apply_identifiers(conn)

        # Hand-configured MVP trigger scopes (R7.2), after the triggers upsert
        scopes_updated = apply_trigger_scopes(conn)

        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise

    # Report
    print("=== GridSignals seed load ===")
    for table, loaded, source_m in summary:
        tag = ""
        if loaded != source_m:
            tag = "  MISMATCH"
            mismatch = True
        print(f"{table}: {loaded} rows loaded (source CSV had {source_m} data rows){tag}")
    print(f"entity_aliases: {len(aliases)} split from watchlist CSV")
    print(f"entity_collision_terms: {len(collisions)} split from watchlist CSV")
    cik_n, lei_n, qid_n, ident_rows, duplicate_skips = ident_stats
    print(f"entity_identifiers: {cik_n} cik + {lei_n} lei + {qid_n} wikidata_qid "
          f"filled (source CSV had {ident_rows} rows; "
          f"{duplicate_skips} duplicate value(s) skipped)")
    print(f"trigger allowed_scopes: {scopes_updated} MVP trigger(s) set")

    if skipped:
        print(f"\n{len(skipped)} row(s) skipped on FK warning:")
        for table, reason in skipped:
            print(f"  WARNING [{table}] skipped: {reason}")

    conn.close()

    if mismatch or skipped:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(load())
