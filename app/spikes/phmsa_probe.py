"""PHMSA pipeline-enforcement spike (R9.6 combo-engine plan, U2).

MEASUREMENT ONLY -- this module is NOT a fetcher. It produces one count and a
recommendation, nothing else. It writes nothing to the store, mints no
signals, has no ingestion-runner CLI entry point, and is deliberately absent
from ``deploy/ingest_pipeline.sh`` (same containment discipline as
``app/spikes/ferc_elibrary_probe.py`` / ``app/spikes/breach_registry_probe.py``,
the precedent this module follows in shape).

WHY A SPIKE. R6 of the combo-engine plan would add a new PHMSA-sourced
account-capable trigger, but only if PHMSA enforcement data actually names
midstream/LNG watchlist accounts at a usable rate. This measures before any
commitment to a real fetcher.

OPEN QUESTION 2, RESOLVED LIVE. The plan's origin document flagged a reported
403 on a PHMSA "incident-flagged-files" domain, and separately claimed
``primis.phmsa.dot.gov/enforcement-data/`` works. This module never touches
the flagged-files domain at all -- it targets ONLY
``primis.phmsa.dot.gov``. Verified live this session (2026-08-18, plain GET,
honest ``GridSignals/0.1`` UA, no compat fix of any kind):

  * ``https://primis.phmsa.dot.gov/enforcement-data/`` (the landing page) --
    HTTP 200.
  * ``https://primis.phmsa.dot.gov/enforcement-documents/PHMSA%20Pipeline%20
    Enforcement%20Raw%20Data.txt`` (the tab-delimited feed the landing page
    links to as its "Raw Data" download, extracted from the page's HTML by
    hand this session) -- HTTP 200, ~2MB, 5,073 data rows.

Neither request was blocked. Unlike the CISA ICS advisories feed (U7a), which
sits behind an Akamai WAF rule that 403s Python's default TLS 1.3 handshake
and needed a documented TLS-1.2-pin compatibility fix (see
``app/ingest/cisa_ics.py``), PHMSA's enforcement-data host required no
request-level adjustment at all -- a plain ``urllib`` GET with the honest UA
is sufficient. ``probe_enforcement_feed`` below still reports any future
block explicitly (endpoint, status code, body snippet) rather than crashing
or returning a silent zero, in case that ever changes.

THE FEED. Tab-delimited, one row per enforcement CASE (not per order). The
columns this probe reads: ``Operator_Name``, ``Case_Type``, ``Opened_Date``
(``M/D/YY``, 2-digit year -- PHMSA's own format, confirmed against the live
feed). ``Case_Type`` takes one of five values in the live data: "Corrective
Action Order", "Notice of Probable Violation", "Warning Letter", "Notice of
Amendment", "Safety Order". Only the first three are QUALIFYING_CASE_TYPES
per the unit brief; Notice of Amendment and Safety Order are excluded.

THE HIT DEFINITION. A hit is a distinct watchlist entity, scoped to the
``midstream``/``lng`` subsectors (see ``seeds/watchlist_entities.csv``), named
as ``Operator_Name`` on at least one qualifying-case-type row with an
``Opened_Date`` in the trailing 24 months. An entity named on multiple
qualifying rows counts once.

ADJUDICATION, NOT JUST MATCHES. PHMSA case records name the specific
subsidiary/operating entity ("Plains Pipeline, L.P.", "Sabine Pass LNG, L.P.
(Cheniere)"), not the parent watchlist name -- exactly the shape
``app/spikes/breach_registry_probe.py``'s KTD5 discipline exists for. This
module reuses that discipline: the resolver's ``review`` bucket
(subsidiary/ambiguous names below the auto-match bar) is reported for the
operator to adjudicate, and ``hit_count`` from ``matched`` alone is a lower
bound until that pass happens -- listing only auto-matched names would bias
the measurement downward by construction.

THRESHOLD, per the unit brief: hit_count >= 3 -> BUILD (recommend U5, subject
to operator approval of the new ``seeds/triggers.csv`` row per R6); < 3 ->
DO NOT BUILD.

  python -m app.spikes.phmsa_probe --report [--db PATH]

Live network: one sequential, read-only GET, UA
``GridSignals/0.1 (+https://github.com/Doogit/GridSignals)``.
"""
import argparse
import csv
import http.client
import io
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from collections import namedtuple
from datetime import date, datetime, timezone
from pathlib import Path

from app.db.connection import DEFAULT_DB_PATH
from app.resolve import EntityResolver

USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"
HTTP_TIMEOUT = 60

# Live-verified 2026-08-18 (see module docstring): the tab-delimited "Raw
# Data" download linked from https://primis.phmsa.dot.gov/enforcement-data/.
# Hardcoded rather than scraped at runtime -- a future session that finds
# this 404ing should re-extract the link from the landing page by hand and
# revise this constant, the same discipline DATA_FERC_PROBE_URL follows in
# ferc_elibrary_probe.py. Deliberately NOT the separately-reported 403'ing
# phmsa.dot.gov incident-flagged-files domain -- this probe never touches
# that host.
RAW_DATA_URL = ("https://primis.phmsa.dot.gov/enforcement-documents/"
                 "PHMSA%20Pipeline%20Enforcement%20Raw%20Data.txt")

WINDOW_MONTHS = 24
BUILD_THRESHOLD = 3

QUALIFYING_CASE_TYPES = frozenset({
    "Corrective Action Order", "Notice of Probable Violation",
    "Warning Letter"})

MIDSTREAM_LNG_SUBSECTORS = ("midstream", "lng")

# operator name, case type, opened date (ISO or "" if unparseable/absent).
PhmsaRecord = namedtuple("PhmsaRecord", ["operator_name", "case_type",
                                         "opened_date"])

_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")


# -- parsing (no network) ---------------------------------------------------

def _clean(text):
    return " ".join((text or "").split())


def _iso_date(text):
    """PHMSA's own ``M/D/YY`` format (2-digit year) -> ISO date, or "" when
    missing or unparseable -- never guessed at. A 2-digit year uses the usual
    pivot (00-49 -> 2000s, 50-99 -> 1900s); the live feed's observed range
    (1990s-2026) never approaches that boundary. A stray 4-digit year is
    accepted as-is, defensively."""
    t = "" if text is None else str(text).strip()
    m = _DATE_RE.match(t)
    if not m:
        return ""
    month, day, year_raw = m.groups()
    year = int(year_raw)
    if len(year_raw) == 2:
        year += 2000 if year < 50 else 1900
    try:
        return date(year, int(month), int(day)).isoformat()
    except ValueError:
        return ""


def parse_records(text):
    """Tab-delimited PHMSA enforcement feed text -> PhmsaRecord list. A row
    missing ``Operator_Name`` or ``Case_Type`` is dropped, never guessed at;
    ``Opened_Date`` is parsed leniently and an unparseable/missing value
    yields "" (excluded later by the window filter, not counted as a drop
    here)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")), delimiter="\t")
    records = []
    for rec in reader:
        org = _clean(rec.get("Operator_Name"))
        case_type = _clean(rec.get("Case_Type"))
        if not org or not case_type:
            continue
        records.append(PhmsaRecord(org, case_type,
                                   _iso_date(rec.get("Opened_Date"))))
    return records


# -- analysis (no network) ---------------------------------------------------

def window_cutoff(now):
    """Earliest opened date inside the trailing 24-month window (inclusive).
    The Feb-29 anniversary falls back to Feb 28 rather than raising (same
    derivation as ``breach_registry_probe.window_cutoff``)."""
    year = now.year - WINDOW_MONTHS // 12
    try:
        return now.replace(year=year).date().isoformat()
    except ValueError:
        return now.replace(year=year, day=28).date().isoformat()


def analyze(conn, records, source_status=None, now=None):
    """Run every in-window, qualifying-case-type record through the
    EntityResolver, scoped to midstream/LNG watchlist entities. ``conn`` is
    read-only; nothing is written."""
    now = now or datetime.now(timezone.utc)
    cutoff = window_cutoff(now)

    qualifying = [r for r in records
                  if r.case_type in QUALIFYING_CASE_TYPES
                  and r.opened_date and r.opened_date >= cutoff]
    # A present Opened_Date COLUMN proves nothing about its VALUES parsing --
    # a renamed/reshaped column (or a 200 OK maintenance/interstitial page
    # that happens to keep Operator_Name/Case_Type-shaped text) leaves every
    # record's opened_date == "" while total_records still looks like a
    # believable full fetch. Counted here so recommendation() can refuse to
    # call that a measured zero, the same discipline
    # ferc_elibrary_probe.analyze's undated_documents already applies.
    undated = sum(1 for r in records if not r.opened_date)

    midstream_lng_ids = {
        r["entity_id"] for r in conn.execute(
            "SELECT entity_id FROM watchlist_entities WHERE active = 1 "
            "AND subsector IN (?, ?)", MIDSTREAM_LNG_SUBSECTORS)}
    names = {r["entity_id"]: r["name"] for r in
             conn.execute("SELECT entity_id, name FROM watchlist_entities")}
    resolver = EntityResolver(conn)

    hits, review, unmatched = {}, [], []
    excluded_non_midstream = set()
    for r in qualifying:
        res = resolver.resolve(name=r.operator_name)
        if res.status == "matched":
            if res.entity_id in midstream_lng_ids:
                hit = hits.setdefault(res.entity_id, {
                    "entity_id": res.entity_id,
                    "entity_name": names.get(res.entity_id, ""),
                    "operator_names": set(), "case_types": set(),
                    "method": res.method})
                hit["operator_names"].add(r.operator_name)
                hit["case_types"].add(r.case_type)
            else:
                excluded_non_midstream.add(r.operator_name)
        elif res.status == "review":
            review.append({
                "operator_name": r.operator_name, "case_type": r.case_type,
                "reason": res.method,
                "candidates": [(eid, names.get(eid, ""), score)
                               for eid, score in res.candidates]})
        else:
            unmatched.append(r.operator_name)

    hit_list = [{"entity_id": h["entity_id"], "entity_name": h["entity_name"],
                 "operator_names": sorted(h["operator_names"]),
                 "case_types": sorted(h["case_types"]), "method": h["method"]}
                for h in sorted(hits.values(), key=lambda h: h["entity_id"])]

    status = source_status or {}
    return {
        "as_of": now.isoformat(),
        "cutoff": cutoff,
        "source_status": status,
        "total_records": len(records),
        "qualifying_records": len(qualifying),
        "undated_count": undated,
        "hit_count": len(hit_list),
        "hits": hit_list,
        "review": sorted(review, key=lambda r: r["operator_name"]),
        "unmatched_count": len(unmatched),
        "excluded_non_midstream_count": len(excluded_non_midstream),
        "recommendation": recommendation(len(hit_list), status,
                                         undated_count=undated,
                                         total_records=len(records)),
    }


def recommendation(hit_count, source_status=None, undated_count=0,
                   total_records=0):
    """The band text, plus two "this is not a measured zero" caveats: a
    blocked/failed fetch (no content ever retrieved), and a parse anomaly
    (content retrieved, but every single record's opened_date failed to
    parse -- almost always a renamed/reshaped Opened_Date column rather than
    a genuine 24-months-old feed)."""
    blocked = sorted(s for s, v in (source_status or {}).items()
                     if not v.startswith("ok"))
    if hit_count >= BUILD_THRESHOLD:
        text = (f"{hit_count} distinct midstream/LNG watchlist accounts with "
                f"a qualifying enforcement action (CAO/NOPV/Warning Letter) "
                f"in the trailing {WINDOW_MONTHS} months (threshold "
                f"{BUILD_THRESHOLD}) -> BUILD U5 (PHMSA enforcement fetcher "
                "and new trigger), pending operator approval of the new "
                "seeds/triggers.csv row per R6.")
    else:
        text = (f"{hit_count} distinct midstream/LNG watchlist accounts with "
                f"a qualifying enforcement action (CAO/NOPV/Warning Letter) "
                f"in the trailing {WINDOW_MONTHS} months (threshold "
                f"{BUILD_THRESHOLD}) -> DO NOT BUILD U5.")
    if blocked:
        text = ("BLOCKED MEASUREMENT -- " +
                "; ".join(f"{s}: {source_status[s]}" for s in blocked) +
                ". hit_count is 0 because no enforcement data was ever "
                "retrieved, not because a completed search found nothing. " +
                text)
    elif total_records and undated_count == total_records:
        text = (f"PARSE ANOMALY -- all {total_records} fetched records have "
                "an unparseable/missing opened_date (likely a "
                "renamed/reshaped Opened_Date column in the feed, not a "
                "genuine 24-months-old dataset); hit_count is not a valid "
                "measurement until the parser is fixed. " + text)
    return text


def format_report(result):
    """Plain text, pasteable into a PR body."""
    out = ["PHMSA pipeline-enforcement probe (R9.6 combo-engine plan, U2) -- "
           "read-only, writes nothing",
           f"as_of={result['as_of']}  window=trailing {WINDOW_MONTHS} months "
           f"opened_date >= {result['cutoff']}", ""]

    status = result["source_status"]
    if status:
        out.append("source status")
        for src in sorted(status):
            out.append(f"  {src}: {status[src]}")
        out.append("")

    out.append(f"context: total records={result['total_records']}  "
               f"qualifying (case-type + in-window)="
               f"{result['qualifying_records']}  "
               f"undated (opened_date unparseable/missing)="
               f"{result['undated_count']}")
    out.append("")

    out += [f"HIT COUNT (distinct midstream/LNG watchlist accounts): "
            f"{result['hit_count']}", ""]
    for h in result["hits"]:
        out.append(f"  {h['entity_id']}  {h['entity_name']}  "
                   f"method={h['method']}  case_types="
                   f"{','.join(h['case_types'])}  as "
                   f"{'; '.join(h['operator_names'])}")

    out += ["", f"review-queued operator names: {len(result['review'])}",
            "  ADJUDICATE THESE -- a hit is operator-confirmed regardless of "
            "how the resolver classified it (PHMSA names the operating "
            "subsidiary, not the parent watchlist name)."]
    for r in result["review"]:
        cands = ", ".join(f"{eid} {name} ({score})"
                          for eid, name, score in r["candidates"]) or "none"
        out.append(f"  \"{r['operator_name']}\"  case_type={r['case_type']}  "
                   f"reason={r['reason']}  candidates={cands}")

    out += ["", f"unmatched operator names (off-list): "
            f"{result['unmatched_count']}",
            f"excluded (matched, but not midstream/LNG subsector): "
            f"{result['excluded_non_midstream_count']}", ""]

    out += ["RECOMMENDATION (provisional -- re-band after the adjudication "
            "pass; hit_count above counts only auto-matches, and the "
            "review-queued names above may well contain the parent watchlist "
            "entity under a subsidiary name, per the module docstring's KTD5 "
            "discipline)",
            "  " + result["recommendation"], ""]
    return "\n".join(out)


def read_only_connect(db_path):
    """Open the store ``mode=ro`` (same containment as
    ``breach_registry_probe.read_only_connect``, including the ``as_uri()``
    fragment-truncation guard)."""
    return sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro",
                           uri=True)


def report(db_path, records, source_status=None, now=None, out=sys.stdout):
    """Analyze ``records`` against the store and print the report."""
    conn = read_only_connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = analyze(conn, records, source_status=source_status, now=now)
    finally:
        conn.close()
    print(format_report(result), file=out)
    return result


# -- fetch (live network; one sequential, read-only GET) --------------------

def probe_enforcement_feed(url=RAW_DATA_URL):
    """One GET against the PHMSA enforcement-data raw feed. Returns
    ``(status, body_or_None)`` -- ``status`` is ``"ok"``, a ``"BLOCKED
    (...)"`` string on an HTTP error, or a ``"FAILED (...)"`` string on a
    lower-level network failure. Never crashes the caller."""
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "text/plain"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        return "ok", body
    except urllib.error.HTTPError as exc:
        with exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
        return (f"BLOCKED ({url} -> {exc.code} {exc.reason}: "
                f"{detail[:200]})", None)
    except (OSError, http.client.HTTPException) as exc:
        return f"FAILED ({url} -> {type(exc).__name__}: {exc})", None


def collect(url=RAW_DATA_URL):
    """Fetch and parse the enforcement feed. Returns ``(records, status)``; a
    blocked/failed fetch contributes no records and a non-"ok" status, never
    a silent zero."""
    status, body = probe_enforcement_feed(url)
    if status != "ok":
        return [], {"phmsa_enforcement_data": status}
    return parse_records(body), {"phmsa_enforcement_data": "ok"}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Measure whether PHMSA enforcement data names "
                    "midstream/LNG watchlist accounts. Read-only; writes "
                    "nothing, mints nothing, is not a fetcher.")
    parser.add_argument("--report", action="store_true", required=True,
                        help="the only mode: fetch, measure, print")
    parser.add_argument("--db", default=DEFAULT_DB_PATH,
                        help=f"store to read (default {DEFAULT_DB_PATH})")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")
    # Prove the store opens read-only before spending the network run.
    read_only_connect(args.db).close()
    records, status = collect()
    report(args.db, records, source_status=status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
