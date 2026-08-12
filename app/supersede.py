"""Docket-based supersession of superseded regulatory proposals (R8.1 status).

A **proposal** signal (a "Proposed Rule"/NOPR) is flipped
``status -> 'superseded'`` once a later **final-rule** signal of the SAME
trigger shares a regulatory docket with it. This is a status change only - the
score and its components are frozen, exactly as decayed/dismissed rows are
(``app.scoring.rescore`` only touches status='active' rows).

Docket-only, precision-first (R7.2 discipline)
    The match is on parsed docket tokens alone; there is NO title-matching
    fallback. A proposal and a final that do not share a docket stay
    independent even if their titles are near-identical.

Docket parsing
    A signal's regulatory dockets live in its raw_events payload under
    ``docket_ids`` (a list of freeform strings). One string may bundle
    several dockets (e.g. "Docket Nos. RM24-4-000 and RM20-19-000"), so we
    extract every ``RM<digits>-<digits>-<digits>`` token with a regex rather
    than comparing the raw strings. ``type`` distinguishes proposals
    ("Proposed Rule") from finals ("Rule").

Determinism / idempotency
    Deterministic (no clock, no randomness) and insert-safe to re-run: an
    already-superseded proposal is status!='active', so the next run skips it
    and flips nothing. Runs under the single-writer ingestion lock (R3.2).

CLI: python -m app.supersede - takes the lock, supersedes
data/gridsignals.db, prints a one-line summary.
"""
import json
import re
import sys

from app.db.connection import get_connection
from app.ingest.runner import ingest_lock

PROPOSAL_TYPE = "Proposed Rule"
FINAL_TYPE = "Rule"

# Matches a single FERC rulemaking docket token, e.g. RM24-8-000. Several may
# appear inside one docket_ids string ("Docket Nos. RM24-4-000 and ...").
_DOCKET_RE = re.compile(r"RM\d+-\d+-\d+")


def _dockets(payload):
    """Set of RM docket tokens parsed from a payload's docket_ids strings."""
    tokens = set()
    for entry in payload.get("docket_ids") or []:
        tokens.update(_DOCKET_RE.findall(str(entry)))
    return tokens


def _load_regulatory(conn):
    """Every signal joined to its raw_events payload, decoded to
    {signal_id, trigger_id, status, event_date, type, dockets}. Signals whose
    payload will not parse are skipped (no docket => cannot match)."""
    out = []
    for row in conn.execute(
            "SELECT s.signal_id, s.trigger_id, s.status, s.event_date, "
            " r.payload FROM signals s "
            "JOIN raw_events r ON r.raw_event_id = s.raw_event_id").fetchall():
        try:
            payload = json.loads(row["payload"] or "")
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        out.append({
            "signal_id": row["signal_id"],
            "trigger_id": row["trigger_id"],
            "status": row["status"],
            "event_date": row["event_date"] or "",
            "type": payload.get("type") or "",
            "dockets": _dockets(payload),
        })
    return out


def supersede(conn):
    """Flip each active proposal to 'superseded' when a later final rule of the
    same trigger shares a docket. Returns {'flipped': n, 'proposals_seen': m}.
    Deterministic and idempotent - a re-run flips 0 (superseded proposals are
    no longer active)."""
    rows = _load_regulatory(conn)
    finals = [r for r in rows
              if r["type"] == FINAL_TYPE and r["dockets"]]
    proposals = [r for r in rows if r["type"] == PROPOSAL_TYPE]

    flipped = 0
    for prop in proposals:
        if prop["status"] != "active" or not prop["dockets"]:
            continue
        if any(f["trigger_id"] == prop["trigger_id"]
               and f["event_date"] > prop["event_date"]
               and f["dockets"] & prop["dockets"]
               for f in finals):
            conn.execute(
                "UPDATE signals SET status = 'superseded' WHERE signal_id = ?",
                (prop["signal_id"],))
            flipped += 1
    conn.commit()
    return {"flipped": flipped, "proposals_seen": len(proposals)}


def main():
    with ingest_lock():
        conn = get_connection()
        try:
            summary = supersede(conn)
        finally:
            conn.close()
    print(f"supersede: success flipped={summary['flipped']} "
          f"proposals_seen={summary['proposals_seen']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
