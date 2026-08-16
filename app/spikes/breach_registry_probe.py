"""State AG breach-registry spike (R5.5 named exception, R9.6 Stage 2, R10.6).

MEASUREMENT ONLY — this module is NOT a fetcher. It produces a number and a
recommendation, nothing else. It writes nothing to the store, mints no signals,
has no ``runner.cli`` entry point, and is deliberately absent from
``deploy/ingest_pipeline.sh`` (pinned by ``tests/test_breach_registry_probe.py``
on the ``#74`` silent-trial precedent). A future session that "fixes" the
missing pipeline entry has undone the point of the unit.

WHY A SPIKE. ``seeds/triggers.csv`` names "state AG breach DBs" as a primary
source for ``own_incident`` and no fetcher was ever built. 8-K Item 1.05 and
Item 5.02 were both measured at zero, but a breach registry is *designed* to
name the breached organization, so it is a structurally different bet and gets
measured rather than assumed.

THE PINNED HIT DEFINITION. A **hit** is a **distinct watchlist entity named at
least once in the CA or WA registry with a reported date in the trailing 24
months**. An entity named in both states counts once, and an entity named ten
times in one state counts once. Per-state counts, raw row counts and distinct
organization counts are reported alongside as context and are explicitly *not*
the threshold quantity: the CA export reaches back to 2024-01 and WA to
2015-07, so "all rows" and "24 months" can differ by a factor of five and cross
the build line on window choice alone. ``window_cutoff`` and ``analyze`` are
the derivation, and the tests pin them against canned fixtures so the number
the operator reads cannot drift from the number this module computes.

ADJUDICATION, NOT JUST MATCHES. The resolver runs against an alias table that
is known to be incomplete, and registries name legal filing entities ("Pacific
Gas and Electric Company") — exactly the shape that routes to review rather
than to a match (KTD5). Listing only ``matched`` names would bias the
measurement downward by construction. So the report also lists the
review-queued organization names and the top fuzzy near-misses with their
scores, and a hit is an **operator-confirmed** watchlist organization
regardless of how the resolver classified it; the bands below are applied only
after that pass. Resolver ``none`` names are the one bucket no listed surface
covers (a subsidiary name scores below the fuzzy cutoff and returns no
candidates at all), so ``--unmatched-sample N`` prints the first N of them for
eyeballing — the live run happens once, under the ingestion lock, and a second
look should not cost a second fetch.

THRESHOLDS, agreed before the number was known:

  0 hits   stop, and record the negative beside 1.05 and 5.02 *precisely*:
           "CA+WA registries do not name watchlist entities at a usable rate;
           the other 13 publishing states were not machine-accessible and are
           untested." NOT "own_incident is closed on the free public record" —
           a state registry only names organizations that notified *that
           state's* residents, so an in-state utility (the ERCOT/TVA/SRP shape)
           is structurally invisible to CA and WA whether or not it was
           breached.
  1-4      operator ruling: is a card every ~6 months worth a fetcher?
  5+       build U6.

R10.6. The probe reads and reports **organization names and reported dates
only**. The parsers are field allowlists, so no resident/individual column ever
reaches the analysis, the report, or memory beyond the raw response text. The
probe persists nothing at all — there is no artifact for a name to leak into.

  python -m app.spikes.breach_registry_probe --report [--db PATH]
      [--ca-url URL] [--wa-url URL] [--unmatched-sample N]

⚠️ Live network. Sequential, read-only GET, politeness sleep between the two
requests. A state that fails to fetch or parse is reported ``status=...`` and
is NOT a measured zero — the recommendation text degrades accordingly, because
a failed fetch that reads as "no hits" is the one failure mode that would send
this spike to the wrong band.
"""
import argparse
import csv
import io
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import namedtuple
from datetime import datetime, timezone

from app.db.connection import DEFAULT_DB_PATH
from app.resolve import EntityResolver

USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"
POLITENESS_SECONDS = 2.0
HTTP_TIMEOUT = 120

# California DOJ/OAG breach list, CSV export. Header inspected at planning time:
# "Organization Name","Date(s) of Breach (if known)","Reported Date".
CA_URL = "https://oag.ca.gov/privacy/databreach/list-export"
# Washington AG breach notifications on data.wa.gov (Socrata). The dataset id
# was NOT verified at planning time — only that the dataset is published as
# Socrata JSON/XML/CSV — so treat a 404 here as "WA untested", not as zero, and
# re-run with --wa-url once the id is confirmed.
WA_URL = "https://data.wa.gov/resource/sb4j-ce6q.json?$limit=50000"

WINDOW_MONTHS = 24
NEAR_MISS_LIMIT = 15

# R10.6 field allowlists — the ONLY columns that leave the parsers.
CA_ORG_HEADER = "organization name"
CA_DATE_HEADER = "reported date"
WA_ORG_KEYS = ("name_of_business", "business_name", "organization_name",
               "company_name", "organization", "name")
WA_DATE_KEYS = ("date_reported_to_ag", "date_reported_to_attorney_general",
                "date_reported", "reported_date", "date_of_notice",
                "notice_date")

NEGATIVE_FINDING = (
    "CA+WA registries do not name watchlist entities at a usable rate; the "
    "other 13 publishing states were not machine-accessible and are untested.")

# state, organization name, reported date (ISO or "") — nothing else, ever.
BreachRow = namedtuple("BreachRow", ["state", "organization", "reported_date"])

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_US_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


# -- parsing (no network) ---------------------------------------------------

def _iso_date(text):
    """ISO date, or "" when the value is missing or unrecognised. Socrata
    floating timestamps ("2024-03-01T00:00:00.000") and the US M/D/YYYY the CA
    export renders are both accepted; anything else is treated as undated and
    counted separately rather than guessed at."""
    t = (text or "").strip()
    m = _ISO_RE.match(t)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = _US_RE.match(t)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return ""


def _clean(name):
    return " ".join((name or "").split())


def parse_ca_export(text):
    """CA OAG CSV export -> BreachRow list. Organization name and reported date
    only; every other column is dropped at the row boundary (R10.6)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    headers = {(h or "").strip().casefold(): h for h in (reader.fieldnames or [])}
    org_key = headers.get(CA_ORG_HEADER)
    date_key = headers.get(CA_DATE_HEADER)
    if not org_key:
        raise ValueError(
            "CA export has no organization column; headers="
            + repr(sorted(headers)))
    rows = []
    for rec in reader:
        org = _clean(rec.get(org_key))
        if not org:
            continue
        rows.append(BreachRow("CA", org,
                              _iso_date(rec.get(date_key) if date_key else "")))
    return rows


def parse_wa_dataset(text):
    """WA AG Socrata JSON -> BreachRow list, same two-field allowlist.

    The dataset's field names were not verifiable offline, so the org/date keys
    are chosen from the allowlists above by inspecting the records. When none
    match, this raises with the observed keys rather than returning an empty
    list: a silent zero from a renamed column would be indistinguishable from a
    measured zero, and the live run happens once."""
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("WA dataset is not a JSON array of records")
    keys = set()
    for rec in data:
        if isinstance(rec, dict):
            keys.update(k.casefold() for k in rec)
    if not data:
        return []
    org_key = next((k for k in WA_ORG_KEYS if k in keys), None)
    date_key = next((k for k in WA_DATE_KEYS if k in keys), None)
    if not org_key:
        raise ValueError("WA dataset has no recognised organization field; "
                         "keys=" + repr(sorted(keys)))
    rows = []
    for rec in data:
        if not isinstance(rec, dict):
            continue
        lowered = {k.casefold(): v for k, v in rec.items()}
        org = _clean(lowered.get(org_key))
        if not org:
            continue
        rows.append(BreachRow("WA", org,
                              _iso_date(lowered.get(date_key) if date_key
                                        else "")))
    return rows


# -- analysis (no network) --------------------------------------------------

def window_cutoff(now):
    """Earliest reported date inside the trailing 24-month window (inclusive).
    The Feb-29 anniversary falls back to Feb 28 rather than raising."""
    year = now.year - WINDOW_MONTHS // 12
    try:
        return now.replace(year=year).date().isoformat()
    except ValueError:
        return now.replace(year=year, day=28).date().isoformat()


def analyze(conn, rows, state_status=None, now=None, unmatched_sample=0):
    """Run every in-window organization name through the EntityResolver and
    derive the pinned hit count. ``conn`` is read-only; nothing is written and
    no review_queue/entity_match_decisions row is logged — a measurement that
    mutates the store is not a measurement (R6.4 logging belongs to real
    ingestion, not to a spike)."""
    now = now or datetime.now(timezone.utc)
    cutoff = window_cutoff(now)

    states = {}
    for state, status in (state_status or {}).items():
        states[state] = {"status": status, "rows": 0, "in_window": 0,
                         "undated": 0}
    orgs = {}   # organization name -> set of states, in-window only
    for row in rows:
        st = states.setdefault(row.state, {"status": "ok", "rows": 0,
                                           "in_window": 0, "undated": 0})
        st["rows"] += 1
        if not row.reported_date:
            st["undated"] += 1
            continue
        if row.reported_date < cutoff:
            continue
        st["in_window"] += 1
        orgs.setdefault(row.organization, set()).add(row.state)

    names = {r["entity_id"]: r["name"] for r in
             conn.execute("SELECT entity_id, name FROM watchlist_entities")}
    active = conn.execute(
        "SELECT COUNT(*) FROM watchlist_entities WHERE active = 1").fetchone()[0]
    resolver = EntityResolver(conn)

    hits, review, unmatched = {}, [], []
    for org in sorted(orgs):
        res = resolver.resolve(name=org)
        if res.status == "matched":
            hit = hits.setdefault(res.entity_id, {
                "entity_id": res.entity_id,
                "entity_name": names.get(res.entity_id, ""),
                "states": set(), "names": set(), "method": res.method})
            hit["states"].update(orgs[org])
            hit["names"].add(org)
        elif res.status == "review":
            review.append({
                "organization": org,
                "states": sorted(orgs[org]),
                "reason": res.method,
                "candidates": [(eid, names.get(eid, ""), score)
                               for eid, score in res.candidates]})
        else:
            unmatched.append(org)

    near_misses = sorted(
        (r for r in review if r["candidates"]),
        key=lambda r: (-max(c[2] for c in r["candidates"]), r["organization"])
    )[:NEAR_MISS_LIMIT]

    hit_list = [{"entity_id": h["entity_id"], "entity_name": h["entity_name"],
                 "states": sorted(h["states"]), "names": sorted(h["names"]),
                 "method": h["method"]}
                for h in sorted(hits.values(), key=lambda h: h["entity_id"])]

    return {
        "as_of": now.isoformat(),
        "cutoff": cutoff,
        "states": states,
        "total_rows": sum(s["rows"] for s in states.values()),
        "total_in_window": sum(s["in_window"] for s in states.values()),
        "distinct_orgs_in_window": len(orgs),
        "watchlist_active": active,
        "hit_count": len(hit_list),
        "hits": hit_list,
        "review": sorted(review, key=lambda r: r["organization"]),
        "near_misses": near_misses,
        "unmatched_count": len(unmatched),
        "unmatched_sample": sorted(unmatched)[:max(0, unmatched_sample)],
        "recommendation": recommendation(len(hit_list), states),
    }


def recommendation(hit_count, states):
    """The band text, plus the partial-measurement caveat when a state did not
    fetch or parse. A failed state is never a measured zero."""
    failed = sorted(s for s, v in states.items() if v["status"] != "ok")
    if hit_count == 0:
        text = ("0 hits -> STOP, do not build U6. Record the negative beside "
                "8-K 1.05 and 5.02, precisely: " + NEGATIVE_FINDING)
    elif hit_count < 5:
        text = (f"{hit_count} hits -> OPERATOR RULING required: is a card "
                "every ~6 months worth a fetcher?")
    else:
        text = (f"{hit_count} hits -> BUILD U6 (state AG breach-registry "
                "fetcher and own_incident wiring).")
    if failed:
        text = ("PARTIAL MEASUREMENT -- " + ", ".join(failed) + " did not "
                "fetch/parse, so this is not a measured zero for that state; "
                "re-run before recording any negative. " + text)
    return text


def format_report(result):
    """Plain text, pasteable into a PR body."""
    out = ["breach registry probe (R5.5, R9.6 Stage 2, R10.6) -- read-only, "
           "writes nothing",
           f"as_of={result['as_of']}  window=trailing {WINDOW_MONTHS} months  "
           f"reported_date >= {result['cutoff']}",
           "",
           f"HIT COUNT (pinned definition): {result['hit_count']}",
           "  a hit is a distinct watchlist entity named at least once in the "
           "CA or WA registry",
           "  with a reported date in the trailing 24 months; an entity named "
           "in both states counts",
           "  once. The counts below are context, not the threshold quantity.",
           ""]
    out.append("context")
    for state in sorted(result["states"]):
        s = result["states"][state]
        out.append(f"  {state}  status={s['status']}  rows={s['rows']}  "
                   f"in_window={s['in_window']}  undated={s['undated']}")
    out.append(f"  total rows={result['total_rows']}  "
               f"in-window rows={result['total_in_window']}  "
               f"distinct in-window organizations="
               f"{result['distinct_orgs_in_window']}")
    out.append(f"  active watchlist entities={result['watchlist_active']}")

    out += ["", f"matched watchlist entities: {result['hit_count']}"]
    for h in result["hits"]:
        out.append(f"  {h['entity_id']}  {h['entity_name']}  "
                   f"states={','.join(h['states'])}  method={h['method']}  "
                   f"as {'; '.join(h['names'])}")

    out += ["", f"review-queued organization names: {len(result['review'])}",
            "  ADJUDICATE THESE -- a hit is an operator-confirmed watchlist "
            "organization regardless",
            "  of how the resolver classified it (KTD5: registries name legal "
            "filing entities)."]
    for r in result["review"]:
        cands = ", ".join(f"{eid} {name} ({score})"
                          for eid, name, score in r["candidates"]) or "none"
        out.append(f"  \"{r['organization']}\"  states={','.join(r['states'])}"
                   f"  reason={r['reason']}  candidates={cands}")

    out += ["", f"top fuzzy near-misses (score desc): {len(result['near_misses'])}"]
    for r in result["near_misses"]:
        eid, name, score = max(r["candidates"], key=lambda c: c[2])
        out.append(f"  {score}  \"{r['organization']}\" ~ {eid} {name}")

    out += ["", f"unmatched organizations: {result['unmatched_count']}",
            "  names are not listed by default -- off-list third parties are "
            "not this product's",
            "  business, and a resolver 'none' carries no score to rank by. "
            "This bucket is the",
            "  one adjudication surface the lists above do not cover; use "
            "--unmatched-sample N."]
    for org in result["unmatched_sample"]:
        out.append(f"  ? \"{org}\"")

    out += ["", "RECOMMENDATION (provisional -- re-band after the adjudication "
            "pass)", "  " + result["recommendation"], ""]
    return "\n".join(out)


def report(db_path, rows, state_status=None, now=None, out=sys.stdout,
           unmatched_sample=0):
    """Analyze ``rows`` against the store and print the report. The store is
    opened ``mode=ro`` via a URI connection: the harness cannot write even by
    accident, which is the containment the ``#74`` precedent asks for."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        result = analyze(conn, rows, state_status=state_status, now=now,
                         unmatched_sample=unmatched_sample)
    finally:
        conn.close()
    print(format_report(result), file=out)
    return result


# -- fetch (live network; sequential, read-only GET) ------------------------

def fetch(url, accept):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": accept})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def collect(ca_url=CA_URL, wa_url=WA_URL, sleep=POLITENESS_SECONDS):
    """Fetch both registries in sequence and parse them. Returns
    ``(rows, state_status)``; a state that fails contributes no rows and a
    non-"ok" status, never a silent zero."""
    rows, status = [], {}
    for state, url, accept, parse in (
            ("CA", ca_url, "text/csv", parse_ca_export),
            ("WA", wa_url, "application/json", parse_wa_dataset)):
        if rows or status:
            time.sleep(sleep)
        try:
            rows.extend(parse(fetch(url, accept)))
            status[state] = "ok"
        except (urllib.error.URLError, TimeoutError, ValueError,
                UnicodeError) as exc:
            status[state] = f"FAILED ({type(exc).__name__}: {exc})"
    return rows, status


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Measure whether the CA/WA state AG breach registries name "
                    "watchlist entities. Read-only; writes nothing, mints "
                    "nothing, is not a fetcher.")
    parser.add_argument("--report", action="store_true", required=True,
                        help="the only mode: fetch, measure, print")
    parser.add_argument("--db", default=DEFAULT_DB_PATH,
                        help=f"store to read (default {DEFAULT_DB_PATH})")
    parser.add_argument("--ca-url", default=CA_URL)
    parser.add_argument("--wa-url", default=WA_URL)
    parser.add_argument("--unmatched-sample", type=int, default=0,
                        help="print the first N unmatched organization names")
    args = parser.parse_args(argv)
    # The report's own wording is ASCII, but a registry organization name is
    # not guaranteed to be. The live run happens once under the ingestion lock,
    # so a cp1252 console must not turn an accented name into the reason the
    # measurement is lost.
    sys.stdout.reconfigure(errors="replace")
    rows, status = collect(args.ca_url, args.wa_url)
    report(args.db, rows, state_status=status,
           unmatched_sample=args.unmatched_sample)
    return 0


if __name__ == "__main__":
    sys.exit(main())
