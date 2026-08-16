"""Precomputed signal aggregates (R8.10).

Refreshes the one GROUP BY read the UI performs — the Explore Analytics tab's
three-way signal count (trigger / scope / incident evidence tier) — into the
0012 ``aggregate_state`` + ``signal_aggregates`` tables:

  python -m app.aggregates

Runs nightly as a pipeline step (``deploy/ingest_pipeline.sh``), after scoring
and plays so the counts describe the same store the digest is about.

As-of, not "current" (R10.2)
    Every refresh stamps ``aggregate_state.computed_at`` and the
    ``status_filter`` its counts cover. A precomputed number is a claim about a
    past store, so the stamp travels with the numbers all the way to the reader
    — ``app.ui.data.analytics_counts`` never returns counts without it.

Staleness basis
    ``basis_fingerprint`` is a cheap JSON fingerprint of the store the counts
    were taken from: per-status signal counts plus the high-water
    ``incident_tier_edits`` id. It is one grouped count on one column plus one
    MAX, so the reader can afford to recompute it on every read, and it moves on
    every mutation that can change these numbers — new signals (classification),
    status flips (supersede / retract / decay) and operator re-tiers (R8.7).
    Rescoring moves neither the fingerprint nor these counts.

    The fingerprint is deliberately NOT a hash of the counts themselves: that
    would cost exactly as much as recomputing the aggregate and would defeat the
    point of precomputing it.

Refresh is destructive and idempotent: the aggregate's rows are replaced whole
inside one transaction with its state row, so a stamp can never describe a
different refresh than the rows beside it. Nothing else in the store is touched.
"""
import json
import sys
from datetime import datetime, timezone

from app.db.connection import get_connection
from app.ingest.runner import ingest_lock

REFRESH_VERSION = "aggregates/1.0"

# The single named aggregate this module maintains. One name, so the reader
# asks for it by constant rather than by string.
ANALYTICS_COUNTS = "explore_analytics_counts"

# Status filter the nightly refresh precomputes, matching the feed's and the
# Analytics tab's default. A read asking for any other filter is served live.
DEFAULT_STATUSES = ("active",)

# Dimensions in the order the reader returns them.
DIMENSIONS = ("trigger", "scope", "incident_tier")


def _utcnow_iso(now=None):
    """UTC ISO-8601 (R10.2). ``now`` is an injectable datetime, matching
    app.scoring.rescore, so a refresh is deterministic under test."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).isoformat()


def _placeholders(n):
    return ",".join("?" * n)


def status_key(statuses):
    """Canonical JSON form of a status filter, so a stored filter and a
    requested one compare by value rather than by call-site ordering."""
    return json.dumps(sorted(set(statuses)))


def compute_counts(conn, statuses=DEFAULT_STATUSES):
    """Live three-way signal counts: ``{'trigger': [...], 'scope': [...],
    'incident_tier': [...]}``, each list plain dicts (key/label/count),
    most-frequent first. This is the canonical computation — the aggregate
    refresh materializes it and ``app.ui.data`` falls back to it — so the
    precomputed and the live path can never drift apart.

    Every counted row is a sourced signal (R4.1); the count carries its
    dimension identity so it stays inspectable back to the same signals the
    cards render. Incident tier is NULL for non-incident signals, so a NULL
    tier is not a bucket.
    """
    statuses = tuple(statuses)
    ph = _placeholders(len(statuses))
    trigger = conn.execute(
        "SELECT s.trigger_id AS key, t.name AS label, COUNT(*) AS count "
        "FROM signals s LEFT JOIN triggers t ON t.trigger_id = s.trigger_id "
        f"WHERE s.status IN ({ph}) "
        "GROUP BY s.trigger_id ORDER BY count DESC, s.trigger_id",
        list(statuses)).fetchall()
    scope = conn.execute(
        "SELECT s.signal_scope AS key, COUNT(*) AS count "
        f"FROM signals s WHERE s.status IN ({ph}) "
        "GROUP BY s.signal_scope ORDER BY count DESC, s.signal_scope",
        list(statuses)).fetchall()
    tier = conn.execute(
        "SELECT s.incident_evidence_level AS key, COUNT(*) AS count "
        f"FROM signals s WHERE s.status IN ({ph}) "
        " AND s.incident_evidence_level IS NOT NULL "
        "GROUP BY s.incident_evidence_level ORDER BY count DESC, "
        "s.incident_evidence_level", list(statuses)).fetchall()
    return {
        "trigger": [{"key": r["key"], "label": r["label"] or r["key"],
                     "count": r["count"]} for r in trigger],
        "scope": [{"key": r["key"], "label": r["key"], "count": r["count"]}
                  for r in scope],
        "incident_tier": [{"key": r["key"], "label": r["key"],
                           "count": r["count"]} for r in tier],
    }


def basis_fingerprint(conn):
    """Cheap JSON fingerprint of the store these counts derive from. See the
    module docstring for what it does and does not detect."""
    status_counts = {}
    for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM signals GROUP BY status"):
        status_counts[r["status"] or ""] = r["n"]
    max_edit = conn.execute(
        "SELECT MAX(edit_id) AS m FROM incident_tier_edits").fetchone()["m"]
    return json.dumps({"status_counts": status_counts,
                       "max_tier_edit": max_edit}, sort_keys=True)


def refresh(conn, statuses=DEFAULT_STATUSES, now=None):
    """Recompute and replace the analytics-counts aggregate. Returns
    ``{'rows': n, 'computed_at': iso}``. One transaction: the state row and its
    counts always agree."""
    computed_at = _utcnow_iso(now)
    counts = compute_counts(conn, statuses)
    basis = basis_fingerprint(conn)

    conn.execute("DELETE FROM signal_aggregates WHERE aggregate_name = ?",
                 (ANALYTICS_COUNTS,))
    conn.execute(
        "INSERT INTO aggregate_state (aggregate_name, computed_at, "
        " status_filter, basis, refresh_version) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(aggregate_name) DO UPDATE SET computed_at = excluded."
        "computed_at, status_filter = excluded.status_filter, "
        "basis = excluded.basis, refresh_version = excluded.refresh_version",
        (ANALYTICS_COUNTS, computed_at, status_key(statuses), basis,
         REFRESH_VERSION))
    rows = 0
    for dimension in DIMENSIONS:
        for row in counts[dimension]:
            conn.execute(
                "INSERT INTO signal_aggregates (aggregate_name, dimension, "
                " key, label, count) VALUES (?, ?, ?, ?, ?)",
                (ANALYTICS_COUNTS, dimension, row["key"], row["label"],
                 row["count"]))
            rows += 1
    conn.commit()
    return {"rows": rows, "computed_at": computed_at}


def main():
    with ingest_lock():
        conn = get_connection()
        try:
            result = refresh(conn)
        finally:
            conn.close()
    print(f"aggregates: success name={ANALYTICS_COUNTS} "
          f"rows={result['rows']} computed_at={result['computed_at']} "
          f"refresh_version={REFRESH_VERSION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
