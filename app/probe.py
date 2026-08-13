"""Store readiness probe (internal diagnostic; KTD1 - not a PRD MUST).

``python -m app.probe`` opens a READ-ONLY connection and prints a plain
aligned readiness table so a chunk is never scoped over an empty/sparse
store (see CLAUDE.md step 0 and docs/solutions/combos-r7.3-are-data-blocked-
probe-cooccurrence.md, which this module's section (c) generalizes). Always
exits 0 - it is a diagnostic, never a gate. No writes; stdlib only.

Sections: (a) raw_events per source_id with freshness; (b) signals grouped
by trigger/scope/incident tier; (c) account-signal co-occurrence (entities
with >=2 distinct triggers - combos readiness, R7.3); (d) GDELT window age;
(e) review_queue pending depth (R6.2); (f) incident_tier_edits count (R8.7).
Timestamps UTC ISO-8601 (R10.2).
"""
import argparse
import sqlite3
from datetime import datetime, timezone

from app.db.connection import get_connection

GDELT_SOURCE_ID = "gdelt"


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _print_header(title):
    print(f"\n-- {title} --")


def _print_rows(rows, none_label="none"):
    """Print pre-formatted row strings, or a `none_label` line if empty."""
    if not rows:
        print(f"  {none_label}")
        return
    for row in rows:
        print(f"  {row}")


def probe_raw_events(conn, source_filter=None):
    """(a) raw_events per source_id: count + max(event_date) freshness."""
    sql = (
        "SELECT source_id, COUNT(*) AS n, MAX(event_date) AS freshest "
        "FROM raw_events"
    )
    params = ()
    if source_filter:
        sql += " WHERE source_id = ?"
        params = (source_filter,)
    sql += " GROUP BY source_id ORDER BY source_id"
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        source_id = r["source_id"] or "(null)"
        freshest = r["freshest"] or "none"
        out.append(f"{source_id:<28} count={r['n']:<8} freshest={freshest}")
    return out


def probe_signals(conn, source_filter=None):
    """(b) signals grouped by trigger_id/signal_scope/incident_evidence_level."""
    sql = (
        "SELECT trigger_id, signal_scope, incident_evidence_level, COUNT(*) AS n "
        "FROM signals"
    )
    params = ()
    if source_filter:
        sql += (
            " WHERE raw_event_id IN "
            "(SELECT raw_event_id FROM raw_events WHERE source_id = ?)"
        )
        params = (source_filter,)
    sql += (
        " GROUP BY trigger_id, signal_scope, incident_evidence_level "
        "ORDER BY trigger_id, signal_scope, incident_evidence_level"
    )
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        trigger_id = r["trigger_id"] or "(null)"
        scope = r["signal_scope"] or "(null)"
        tier = r["incident_evidence_level"] or "n/a"
        out.append(
            f"trigger={trigger_id:<20} scope={scope:<20} tier={tier:<24} "
            f"count={r['n']}"
        )
    return out


def probe_cooccurrence(conn):
    """(c) account-signal co-occurrence: entities with >=2 distinct triggers
    (combos readiness, R7.3). See docs/solutions/combos-r7.3-are-data-blocked-
    probe-cooccurrence.md for the query this generalizes."""
    rows = conn.execute(
        "SELECT entity_id, COUNT(DISTINCT trigger_id) AS trigs "
        "FROM signals WHERE entity_id IS NOT NULL "
        "GROUP BY entity_id HAVING trigs >= 2 ORDER BY trigs DESC, entity_id"
    ).fetchall()
    return [f"{r['entity_id']:<12} distinct_triggers={r['trigs']}" for r in rows]


def probe_gdelt_window(conn):
    """(d) GDELT window age: oldest/newest event_date for source_id='gdelt'."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(event_date) AS oldest, MAX(event_date) AS newest "
        "FROM raw_events WHERE source_id = ?",
        (GDELT_SOURCE_ID,),
    ).fetchone()
    if not row or not row["n"]:
        return []
    return [f"count={row['n']:<8} oldest={row['oldest']} newest={row['newest']}"]


def probe_review_queue(conn):
    """(e) review_queue pending depth (R6.2)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM review_queue WHERE disposition = 'pending'"
    ).fetchone()
    n = row["n"] if row else 0
    return [] if not n else [f"pending={n}"]


def probe_tier_edits(conn):
    """(f) incident_tier_edits count (R8.7)."""
    row = conn.execute("SELECT COUNT(*) AS n FROM incident_tier_edits").fetchone()
    n = row["n"] if row else 0
    return [] if not n else [f"total={n}"]


def run_probe(conn, source_filter=None):
    """Print the full readiness report to stdout. Never raises for empty data."""
    print(f"GridSignals store probe - {_now_iso()}")
    if source_filter:
        print(f"(filtered to source_id={source_filter})")

    _print_header("raw_events by source")
    _print_rows(probe_raw_events(conn, source_filter))

    _print_header("signals by trigger/scope/tier")
    _print_rows(probe_signals(conn, source_filter))

    _print_header("account-signal co-occurrence (>=2 distinct triggers)")
    _print_rows(probe_cooccurrence(conn))

    _print_header("GDELT window age")
    _print_rows(probe_gdelt_window(conn))

    _print_header("review_queue pending depth")
    _print_rows(probe_review_queue(conn))

    _print_header("incident_tier_edits count")
    _print_rows(probe_tier_edits(conn))


def main():
    parser = argparse.ArgumentParser(
        description="Print a store readiness report (internal diagnostic; KTD1)."
    )
    parser.add_argument(
        "--source", default=None,
        help="filter raw_events/signals sections to one source_id",
    )
    args = parser.parse_args()

    conn = get_connection()
    try:
        conn.execute("PRAGMA query_only=ON;")
        run_probe(conn, source_filter=args.source)
    except sqlite3.Error as exc:
        print(f"probe error (non-fatal): {exc}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
