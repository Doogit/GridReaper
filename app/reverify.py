"""License re-verification due-date sweep (R10.7).

    python -m app.reverify

R10.7 has two halves. The Admin banner half already exists: the page reads
``app.ui.data.stale_facts`` on every load and lists every license fact whose
``verified_date`` is older than the window (or is empty/unparseable — unknown
verification cannot be proven fresh). The SEMI-ANNUAL JOB half is this module.
Without it the banner only tells an operator who happens to open Admin; the
scheduled sweep names the due facts in the container log on its own cadence, so
the re-verification work announces itself.

Staleness is NOT redefined here. The sweep calls ``data.stale_facts`` and takes
its window default, so the job and the banner can never drift to two different
notions of "due" — that drift is the whole failure mode a second constant would
introduce. Reading the UI's read seam from a backend job is the deliberate
tradeoff: it is stdlib-only and read-only, and one shared query beats two.

What the job does NOT do: re-verify anything. R10.7's task is walking the
validation checklist in the licensing reference doc (E7 contents/price, Security
Copilot SCU mechanics, Agent 365 prerequisites, price tables, Sentinel meters,
Defender for IoT site tiers, Entra/Purview/Intune/GHAS prices, gov-cloud
availability) against primary Microsoft sources — a human act. This module
writes nothing to the store, invents no ``verified_date``, and exits 0 whether
or not facts are due: having re-verification work is not a failure.
"""
import sys
from datetime import datetime, timezone

from app.db.connection import get_connection
from app.ui import data


def _utcnow_iso(now=None):
    """UTC ISO-8601 (R10.2); ``now`` injectable so a sweep is deterministic."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).isoformat()


def sweep(conn, now=None):
    """The facts due for re-verification, with the denominator they came from.

    ``now`` is an injectable aware datetime, the type ``data.stale_facts``
    takes, so the sweep and the banner age a fact identically.

    Returns ``{"due": [rows], "facts_total": int, "as_of": iso}``. ``due`` is
    ``data.stale_facts`` verbatim (oldest first, unknown-verified last), so each
    row carries its own ``age_days`` — the reader sees how stale each fact is,
    not just that it crossed a threshold. ``facts_total`` is the denominator:
    "3 due" means nothing without "of how many".
    """
    due = data.stale_facts(conn, now=now)
    facts_total = conn.execute(
        "SELECT COUNT(*) AS n FROM license_facts").fetchone()["n"]
    return {"due": due, "facts_total": facts_total, "as_of": _utcnow_iso(now)}


def format_sweep(result):
    """Render a ``sweep`` result as log lines: summary first, then one per fact.

    Returns the lines as a list so the shaping is testable without a store.
    """
    lines = [
        f"reverify: success due={len(result['due'])} "
        f"facts={result['facts_total']} as_of={result['as_of']}"
    ]
    for fact in result["due"]:
        age = fact["age_days"]
        lines.append(
            f"reverify: due fact_id={fact['fact_id']} "
            f"product={fact['product_id']} "
            f"verified_date={fact['verified_date'] or '(none)'} "
            f"age_days={'unknown' if age is None else age}")
    return lines


def main():
    """Sweep license-fact verification due-dates (R10.7). Returns 0."""
    conn = get_connection()
    try:
        result = sweep(conn)
    finally:
        conn.close()
    for line in format_sweep(result):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
