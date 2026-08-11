"""GridReaper seed loader.

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
     "int_cols": ["base_strength", "decay_half_life_days", "mvp_flag"]},
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
     "skip_cols": ["aliases", "collision_terms"]},
    {"table": "license_matrix", "csv": "license_matrix.csv",
     "pk": ["product_id", "tier"], "int_cols": []},
    {"table": "source_policies", "csv": "source_policies.csv",
     "pk": ["source_id"], "int_cols": ["ttl", "enabled", "evidence_rank"],
     # enabled is admin/G2-managed after first insert - never clobber it
     "update_cols": ["name", "access_method", "ttl", "tos_status",
                     "evidence_rank", "rate_limit", "last_policy_review"]},
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

        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise

    # Report
    print("=== GridReaper seed load ===")
    for table, loaded, source_m in summary:
        tag = ""
        if loaded != source_m:
            tag = "  MISMATCH"
            mismatch = True
        print(f"{table}: {loaded} rows loaded (source CSV had {source_m} data rows){tag}")
    print(f"entity_aliases: {len(aliases)} split from watchlist CSV")
    print(f"entity_collision_terms: {len(collisions)} split from watchlist CSV")

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
