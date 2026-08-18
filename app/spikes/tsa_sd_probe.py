"""TSA Security Directive probe (R3, R4 of the combo-engine plan; R9.6).

MEASUREMENT ONLY -- this module is NOT a fetcher. It opens a READ-ONLY
connection (``mode=ro``, mirroring ``app/probe.py``'s connection style)
against the already-ingested ``federal_register`` raw_events and applies
lightweight keyword matching to count classifiable TSA Security Directive
volume. It writes nothing to the store, mints no signals, has no
``runner.cli`` entry point, and is deliberately absent from
``deploy/ingest_pipeline.sh`` (same containment discipline as
``app/spikes/ferc_elibrary_probe.py`` / ``breach_registry_probe.py``).

WHY A SPIKE. ``app/obligations.py``'s ``APPLICABILITY`` dict already maps
``tsa_security_directive`` to real subsectors (``lng``, ``midstream``) --
MVP-ready on the classification/obligation-derivation side -- but no signal
has ever been classified from real data, because no TSA SD ingestion exists.
This probe answers Open Question 1 of the combo-engine plan: is the
already-ingested ``federal_register`` corpus (FERC + TSA documents, R5.5)
usable for R8, or does R8 need a dedicated TSA.gov fetcher?

RELEVANCE HEURISTIC. Not a full classifier -- a lightweight keyword pass over
each ``federal_register`` raw event's stored payload (title/abstract/action,
the same fields ``app/ingest/federal_register.py`` requests). A document
counts as TSA-SD-relevant when it (a) names the Transportation Security
Administration in ``agency_names``, (b) mentions a pipeline/LNG term, and (c)
mentions a security-directive-shaped term. The Federal Register agency filter
already admits TSA documents on many unrelated surface-transportation topics
(rail, mass transit, aviation); (b)+(c) are what narrow to the pipeline/LNG
security subset -- this is what "scoped to midstream/LNG accounts" means
here: a TSA Security Directive regulates the pipeline/LNG owner/operator
CLASS uniformly (the same subsector-level predicate ``app/obligations.py``
already encodes for ``tsa_security_directive``), not a named account, so
scoping is by document topic, not by per-entity name resolution -- unlike the
FERC eLibrary / breach-registry spikes, this probe never touches
``EntityResolver``.

CAVEAT ON A ZERO COUNT. Real TSA Security Directives themselves (the SD
Pipeline-2021-01/-02 lineage and successors) are Sensitive Security
Information issued directly to designated owners/operators -- they are not
published in the Federal Register. A 0 count here is therefore NOT proof no
TSA SD activity exists; it measures only whether SD-ADJACENT public
rulemaking (e.g. a proposed pipeline cybersecurity rule) shows up in this
corpus. ``recommendation`` states this caveat plainly rather than letting a
0 read as "TSA does nothing here."

  python -m app.spikes.tsa_sd_probe --report [--db PATH]

No live network at all -- this queries the local store that
``app/ingest/federal_register.py`` already populated.
"""
import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.db.connection import DEFAULT_DB_PATH

SOURCE_ID = "federal_register"
WINDOW_MONTHS = 24

TSA_AGENCY_NAME = "transportation security administration"
PIPELINE_LNG_TERMS = ("pipeline", "lng", "liquefied natural gas")
SECURITY_DIRECTIVE_TERMS = (
    "security directive", "cybersecurity", "cyber security",
    "security program", "security measures", "security requirements")

# federal_register.py stores event_date as the API's publication_date
# (YYYY-MM-DD, no time component). Window membership is a plain string
# comparison against cutoff, which is only valid for that exact shape -- a
# stray non-ISO value would otherwise sort unpredictably against cutoff and
# silently corrupt the in/out-of-window classification this probe exists to
# report. Anything that doesn't match this shape is treated as undated
# (see ``analyze``), never guessed at.
_EVENT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Subsectors app/obligations.py's APPLICABILITY already maps
# tsa_security_directive to. Reported as context only -- a TSA SD document
# names no individual account (see module docstring), so this is not a
# per-row filter.
TSA_SD_SUBSECTORS = ("lng", "midstream")


def window_cutoff(now):
    """Earliest event_date inside the trailing WINDOW_MONTHS window
    (inclusive). Feb-29 anniversary falls back to Feb 28 (same derivation as
    ``breach_registry_probe.window_cutoff``)."""
    year = now.year - WINDOW_MONTHS // 12
    try:
        return now.replace(year=year).date().isoformat()
    except ValueError:
        return now.replace(year=year, day=28).date().isoformat()


def is_tsa_sd_relevant(payload):
    """Lightweight keyword relevance check over one federal_register
    document's stored payload dict. Not a full classifier -- see module
    docstring."""
    agency_names = payload.get("agency_names") or []
    if isinstance(agency_names, str):
        agency_names = [agency_names]
    agency_text = " ".join(str(a) for a in agency_names).lower()
    if TSA_AGENCY_NAME not in agency_text:
        return False

    text = " ".join(str(payload.get(f) or "") for f in
                    ("title", "abstract", "action")).lower()
    has_pipeline_lng = any(term in text for term in PIPELINE_LNG_TERMS)
    has_security = any(term in text for term in SECURITY_DIRECTIVE_TERMS)
    return has_pipeline_lng and has_security


def analyze(conn, now=None):
    """Scan federal_register raw_events in ``conn`` (read-only in the live
    path, hermetic in-memory in tests) for TSA-SD-relevant, in-window
    documents. Nothing is written; a malformed payload is skipped, not
    fabricated (R4.1), and counted separately.

    A relevant document with no ``event_date``, or one that doesn't match
    the API's plain ``YYYY-MM-DD`` shape, is counted as ``undated_matches`` --
    distinct from ``out_of_window_matches`` (a well-formed date before
    cutoff). Conflating the two would either mislabel an undated document as
    date-excluded, or -- worse -- let a malformed date silently win a raw
    string comparison against cutoff and flip a match's in/out-of-window
    classification."""
    now = now or datetime.now(timezone.utc)
    cutoff = window_cutoff(now)

    total = 0
    malformed = 0
    undated = 0
    out_of_window = 0
    matches = []
    rows = conn.execute(
        "SELECT event_date, payload FROM raw_events WHERE source_id = ?",
        (SOURCE_ID,))
    for event_date, payload_text in rows:
        total += 1
        try:
            payload = json.loads(payload_text) if payload_text else {}
        except (ValueError, TypeError):
            malformed += 1
            continue
        if not isinstance(payload, dict):
            malformed += 1
            continue
        if not is_tsa_sd_relevant(payload):
            continue
        if not event_date or not _EVENT_DATE_RE.match(event_date):
            undated += 1
            continue
        if event_date < cutoff:
            out_of_window += 1
            continue
        matches.append({
            "document_number": payload.get("document_number") or "",
            "title": payload.get("title") or "",
            "event_date": event_date,
        })

    return {
        "as_of": now.isoformat(),
        "cutoff": cutoff,
        "total_raw_events": total,
        "malformed_payloads": malformed,
        "undated_matches": undated,
        "out_of_window_matches": out_of_window,
        "match_count": len(matches),
        "matches": sorted(matches, key=lambda m: m["event_date"], reverse=True),
        "recommendation": recommendation(len(matches), out_of_window),
    }


def recommendation(match_count, out_of_window_count=0):
    """R3's explicit source-strategy call (Open Question 1): reuse
    federal_register, build a dedicated TSA.gov fetcher, or neither."""
    if match_count == 0:
        text = (
            "0 classifiable TSA Security Directive document(s) in the "
            f"trailing {WINDOW_MONTHS}-month window -> do NOT reuse "
            "federal_register for R8 on this evidence alone, and do NOT "
            "default to building a dedicated TSA.gov fetcher either: real "
            "Security Directives are Sensitive Security Information issued "
            "directly to designated operators, not published in the Federal "
            "Register, so a 0 here does not prove TSA SD activity is absent "
            "-- it proves this window of the corpus does not carry it. "
            "NEITHER source is supported by this measurement; R8 should stay "
            "unscoped until the operator confirms whether any public TSA "
            "SD-adjacent feed exists at all.")
        if out_of_window_count:
            text += (
                f" NOTE: {out_of_window_count} relevant document(s) were "
                "found OUTSIDE the trailing window -- the corpus format is "
                "proven workable, only this window came back empty; widen "
                "the window before treating this as a clean negative.")
        return text
    return (
        f"{match_count} classifiable TSA Security Directive document(s) found "
        f"in the existing federal_register corpus (trailing {WINDOW_MONTHS} "
        "months) -> REUSE federal_register for R8; a dedicated TSA.gov "
        "fetcher is not needed.")


def format_report(result):
    """Plain text, pasteable into a PR body."""
    out = ["TSA Security Directive probe (R3, R9.6) -- read-only, writes "
           "nothing, local store only",
           f"as_of={result['as_of']}  window=trailing {WINDOW_MONTHS} months  "
           f"event_date >= {result['cutoff']}",
           "",
           f"context: federal_register raw_events scanned="
           f"{result['total_raw_events']}  malformed_payloads="
           f"{result['malformed_payloads']}  "
           f"relevant_but_undated={result['undated_matches']}  "
           f"relevant_but_out_of_window={result['out_of_window_matches']}",
           "(context only -- applicable subsectors per app/obligations.py: "
           + ", ".join(TSA_SD_SUBSECTORS) + ")",
           "",
           f"MATCH COUNT (in-window, TSA-SD-relevant): {result['match_count']}"]
    for m in result["matches"]:
        out.append(f"  {m['event_date']}  {m['document_number']}  {m['title']}")
    out += ["", "RECOMMENDATION", "  " + result["recommendation"], ""]
    return "\n".join(out)


def read_only_connect(db_path):
    """Open the store ``mode=ro`` (containment precedent: ``app/probe.py``,
    ``ferc_elibrary_probe.read_only_connect``). ``Path.as_uri()`` avoids the
    ``#`` fragment-truncation trap documented there -- a db path containing
    one, pasted into an f-string, would truncate the URI and take
    ``?mode=ro`` with it."""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro",
                           uri=True)
    conn.execute("PRAGMA query_only=ON;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def report(db_path, now=None, out=sys.stdout):
    """Analyze the store at ``db_path`` and print the report."""
    conn = read_only_connect(db_path)
    try:
        result = analyze(conn, now=now)
    finally:
        conn.close()
    print(format_report(result), file=out)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Measure classifiable TSA Security Directive volume in "
                    "the existing federal_register corpus. Read-only; writes "
                    "nothing, mints nothing, is not a fetcher.")
    parser.add_argument("--report", action="store_true", required=True,
                        help="the only mode: query, measure, print")
    parser.add_argument("--db", default=DEFAULT_DB_PATH,
                        help=f"store to read (default {DEFAULT_DB_PATH})")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")
    report(args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
