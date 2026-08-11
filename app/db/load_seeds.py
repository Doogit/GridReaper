"""GridReaper seed loader.

Reads app/db/schema.sql, then upserts the six config CSVs from seeds/ into
their tables. Idempotent (rule 13): re-running refreshes rows rather than
duplicating or erroring. Run as: python -m app.db.load_seeds
"""
import csv
import os
import sys

from app.db.connection import get_connection

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SCHEMA_PATH = os.path.join(HERE, "schema.sql")
SEEDS_DIR = os.path.join(REPO_ROOT, "seeds")

# Loaded in FK order: products + triggers precede indicator_map.
TABLES = [
    {"table": "products", "csv": "products.csv",
     "pk": ["product_id"], "int_cols": ["energy_ot_flag"]},
    {"table": "triggers", "csv": "triggers.csv",
     "pk": ["trigger_id"],
     "int_cols": ["base_strength", "decay_half_life_days", "mvp_flag"]},
    {"table": "indicator_map", "csv": "indicator_map.csv",
     "pk": ["trigger_id", "product_id"], "int_cols": [],
     # (column in this CSV, table whose PK it must exist in)
     "fk_checks": [("trigger_id", "triggers"), ("product_id", "products")]},
    {"table": "cip_product_map", "csv": "cip_product_map.csv",
     "pk": ["cip_standard"], "int_cols": []},
    {"table": "watchlist_entities", "csv": "watchlist_entities.csv",
     "pk": ["entity_id"], "int_cols": []},
    {"table": "license_matrix", "csv": "license_matrix.csv",
     "pk": ["product_id", "tier"], "int_cols": []},
]


def read_rows(csv_path):
    """Return (header, list-of-dict-rows) for non-empty data rows."""
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


def build_upsert(table, cols, pk):
    placeholders = ",".join("?" * len(cols))
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
    skipped = []      # (table, reason, row) for FK-skipped rows
    summary = []      # (table, loaded_n, source_m)
    mismatch = False

    try:
        with open(SCHEMA_PATH, encoding="utf-8") as fh:
            conn.executescript(fh.read())

        conn.execute("BEGIN")
        for spec in TABLES:
            table = spec["table"]
            csv_path = os.path.join(SEEDS_DIR, spec["csv"])
            header, rows = read_rows(csv_path)
            cols = header
            int_cols = set(spec["int_cols"])
            pk = spec["pk"]
            sql = build_upsert(table, cols, pk)
            fk_checks = spec.get("fk_checks", [])

            loaded = 0
            for row in rows:
                skip = False
                for col, ref_table in fk_checks:
                    if row.get(col, "") not in pk_sets.get(ref_table, set()):
                        skipped.append((table, f"{col}={row.get(col, '')!r} "
                                               f"not in {ref_table}", row))
                        skip = True
                        break
                if skip:
                    continue
                params = [coerce(row.get(c, ""), c in int_cols) for c in cols]
                conn.execute(sql, params)
                loaded += 1
                # Track single-column PKs so later tables can FK-validate.
                if len(pk) == 1:
                    pk_sets.setdefault(table, set()).add(row.get(pk[0], ""))

            summary.append((table, loaded, len(rows)))

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

    if skipped:
        print(f"\n{len(skipped)} row(s) skipped on FK warning:")
        for table, reason, _ in skipped:
            print(f"  WARNING [{table}] skipped: {reason}")

    conn.close()

    if mismatch or skipped:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(load())
