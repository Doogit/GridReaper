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
after that pass. Resolver ``none`` names would otherwise escape adjudication
entirely — a subsidiary name scores below the fuzzy cutoff and comes back with
no candidates and no score — so ``token_overlap`` lists, by default, every
unmatched organization sharing a distinctive token with a watchlist name or
alias, with the token shown. ``--unmatched-sample N`` additionally dumps the
first N of the remainder: the live run happens once, under the ingestion lock,
and a second look should not cost a second fetch.

Because of that bucket the hit count is a **lower bound** until the operator
has read the review-queued and token-overlap lists.

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
import http.client
import io
import json
import re
import sqlite3
import sys
import time
import urllib.request
from collections import namedtuple
from datetime import date, datetime, timezone
from pathlib import Path

from app.db.connection import DEFAULT_DB_PATH
from app.resolve import EntityResolver, normalize

USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"
POLITENESS_SECONDS = 2.0
HTTP_TIMEOUT = 120

# California DOJ/OAG breach list, CSV export. Header inspected at planning time:
# "Organization Name","Date(s) of Breach (if known)","Reported Date".
CA_URL = "https://oag.ca.gov/privacy/databreach/list-export"
# Washington AG breach notifications on data.wa.gov (Socrata). Dataset id
# sb4j-ca4h, verified 2026-08-16 against the catalog API. Treat a fetch failure
# here as "WA untested", never as zero: a state registry only names
# organizations that notified THAT state's residents, so an unreachable state
# and a state with nothing to report produce the same empty list.
WA_ROW_CAP = 50000
WA_URL = f"https://data.wa.gov/resource/sb4j-ca4h.json?$limit={WA_ROW_CAP}"

WINDOW_MONTHS = 24
NEAR_MISS_LIMIT = 15

# R10.6 field allowlists — the ONLY columns that leave the parsers.
CA_ORG_HEADER = "organization name"
CA_DATE_HEADER = "reported date"
WA_ORG_KEYS = ("name_of_business", "business_name", "organization_name",
               "company_name", "organization", "name")
# datesubmitted first: it is the verified WA column ("the date the notifying
# entity submitted their notice to the Attorney General's Office"), the direct
# analogue of CA's "Reported Date". The rest are speculative fallbacks and must
# never outrank it. dateaware/datestart/dateend are also published and are NOT
# reported dates — they describe the breach, not the notification.
WA_DATE_KEYS = ("datesubmitted", "date_reported_to_ag",
                "date_reported_to_attorney_general", "date_reported",
                "reported_date", "date_of_notice", "notice_date")

# Display-only ranking of the resolver's `none` bucket (see token_overlap).
# These tokens recur across too many energy company names to distinguish
# anything, so an overlap on one of them is noise, not a lead.
GENERIC_TOKENS = frozenset({
    "energy", "company", "inc", "llc", "corp", "holdings", "group",
    "services", "power", "electric", "gas", "resources", "international",
    "technologies"})
MIN_TOKEN_CHARS = 4

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
    t = "" if text is None else str(text).strip()
    m = _ISO_RE.match(t)
    if m:
        return _valid_date(m.group(1), m.group(2), m.group(3))
    m = _US_RE.match(t)
    if m:
        return _valid_date(m.group(3), m.group(1), m.group(2))
    return ""


def _valid_date(year, month, day):
    """Real calendar date or "". Both registries publish US month-first dates,
    so a DD/MM/YYYY value cannot be told from M/D/YYYY when the day is <= 12 —
    but when it is not, the range check refuses it instead of minting a
    "2026-14-03" that would sort as in-window and count toward the number."""
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return ""


def _clean(name):
    return " ".join((name or "").split())


def parse_ca_export(text):
    """CA OAG CSV export -> BreachRow list. Organization name and reported date
    only; every other column is dropped at the row boundary (R10.6).

    Both halves of the allowlist fail loud, for the reason spelled out in
    ``parse_wa_dataset``: a missing date HEADER would make every row undated,
    every row out of window, and CA's contribution zero — under ``status=ok``.
    A header that is present with empty values is a different thing and stays
    tolerated: those rows are genuinely undated and are counted as such."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    headers = {(h or "").strip().casefold(): h for h in (reader.fieldnames or [])}
    org_key = headers.get(CA_ORG_HEADER)
    date_key = headers.get(CA_DATE_HEADER)
    if not org_key:
        raise ValueError(
            "CA export has no organization column; headers="
            + repr(sorted(headers)))
    if not date_key:
        raise ValueError(
            "CA export has no reported-date column; headers="
            + repr(sorted(headers)))
    rows = []
    for rec in reader:
        org = _clean(rec.get(org_key))
        if not org:
            continue
        rows.append(BreachRow("CA", org, _iso_date(rec.get(date_key))))
    return rows


def parse_wa_dataset(text):
    """WA AG Socrata JSON -> BreachRow list, same two-field allowlist.

    The org/date keys are chosen from the allowlists above by inspecting the
    records. When EITHER is unmatched this raises with the observed keys.
    Both halves must fail loud, and the date half is the one that bites: a
    missing organization column yields no rows and is obvious, while a missing
    date column yields rows that are all undated, all out of window, and a hit
    count of zero — under a report that still says ``status=ok``. That is a
    silent zero wearing a measured zero's clothes, and it is exactly the
    failure this unit exists to prevent. (Observed live: the dataset publishes
    ``datesubmitted``, and an allowlist without it turned 1,629 real rows into
    ``in_window=0 undated=1629``.)"""
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
    if not date_key:
        raise ValueError("WA dataset has no recognised reported-date field; "
                         "keys=" + repr(sorted(keys)))
    rows = []
    for rec in data:
        if not isinstance(rec, dict):
            continue
        lowered = {k.casefold(): v for k, v in rec.items()}
        org = _clean(lowered.get(org_key))
        if not org:
            continue
        rows.append(BreachRow("WA", org, _iso_date(lowered.get(date_key))))
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


def _distinctive_tokens(text):
    return {t for t in normalize(text).split()
            if len(t) >= MIN_TOKEN_CHARS and t not in GENERIC_TOKENS}


def token_overlap(conn, organizations, names):
    """Rank the resolver's ``none`` bucket for the adjudication pass.

    DISPLAY ONLY — this decides nothing and resolves nothing; it is a reading
    order over names the resolver already declined (KTD5's "do not add a second
    matching path" governs matching, and no match is made here). It exists
    because ``none`` is the one bucket with no handle on it: ``matched`` and
    ``review`` names are listed with entity ids and scores, but a name below
    the fuzzy cutoff comes back with no candidates at all, so listing it
    alphabetically buries it among off-list third parties. The shape KTD5
    predicts — an operating subsidiary ("Southern California Edison Company")
    against a watchlist parent with no alias ("Edison International") — lands
    exactly here and shares exactly one distinctive token.

    Returns one entry per organization sharing a non-generic token of >=
    MIN_TOKEN_CHARS with an active entity's name or alias, most shared tokens
    first."""
    active, index = set(), {}
    for r in conn.execute("SELECT entity_id, name FROM watchlist_entities "
                          "WHERE active = 1"):
        active.add(r["entity_id"])
        for token in _distinctive_tokens(r["name"]):
            index.setdefault(token, set()).add(r["entity_id"])
    for r in conn.execute("SELECT entity_id, alias FROM entity_aliases"):
        if r["entity_id"] not in active:
            continue
        for token in _distinctive_tokens(r["alias"]):
            index.setdefault(token, set()).add(r["entity_id"])

    out = []
    for org in organizations:
        shared = sorted(_distinctive_tokens(org) & set(index))
        if not shared:
            continue
        eids = sorted({eid for token in shared for eid in index[token]})
        out.append({"organization": org, "tokens": shared,
                    "entities": [(eid, names.get(eid, "")) for eid in eids]})
    return sorted(out, key=lambda e: (-len(e["tokens"]), e["organization"]))


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

    # A present column whose VALUES do not parse is the same silent zero as a
    # missing column, one layer down: the allowlist proves the field exists,
    # never that _iso_date understood it. A registry that switches to
    # "March 4, 2026" (or to DD/MM/YYYY, which the M/D branch mis-reads rather
    # than rejects) leaves every row undated and the hit count at 0 while the
    # status still reads ok. Degrade it so recommendation() refuses to call
    # that a measured zero.
    for state, st in states.items():
        if st["status"] == "ok" and st["rows"] and st["undated"] == st["rows"]:
            st["status"] = (f"DATES UNPARSEABLE (all {st['rows']} rows undated "
                            "-- the reported-date column parsed to nothing)")

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

    # fuzzy_below_threshold only: it is the one review reason whose confidence
    # is a computed similarity ratio. collision_term / ambiguous_name /
    # ambiguous_context all carry the constant REVIEW_THRESHOLD, so ranking
    # them here would print 0.75 under a "score desc" heading as if it measured
    # resemblance. They are still listed in full under review-queued names.
    near_misses = sorted(
        (r for r in review
         if r["reason"] == "fuzzy_below_threshold" and r["candidates"]),
        key=lambda r: (-max(c[2] for c in r["candidates"]), r["organization"])
    )[:NEAR_MISS_LIMIT]

    overlap = token_overlap(conn, sorted(unmatched), names)
    listed = {e["organization"] for e in overlap}

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
        "unmatched_token_overlap": overlap,
        # the REMAINDER: a name already listed above does not need listing
        # twice, and the sample exists to reach the names nothing else reaches.
        "unmatched_sample": [o for o in sorted(unmatched)
                             if o not in listed][:max(0, unmatched_sample)],
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
           f"  with a reported date in the trailing {WINDOW_MONTHS} months; an "
           "entity named in both states",
           "  counts once. The counts below are context, not the threshold "
           "quantity.",
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

    overlap = result["unmatched_token_overlap"]
    out += ["", f"unmatched organizations: {result['unmatched_count']}",
            f"  ADJUDICATE THESE TOO: {len(overlap)} sharing a distinctive "
            "token with a watchlist name",
            "  or alias, listed below. A resolver 'none' carries no score, so "
            "token",
            "  overlap is the only handle on this bucket, and it is where an "
            "operating subsidiary",
            "  lands when the parent has no alias (KTD5). Expect noise: a "
            "shared token is not a",
            "  match, it is a name worth one second of a human's attention. "
            "The remainder are",
            "  counted only; --unmatched-sample N dumps them raw."]
    for e in overlap:
        ents = "; ".join(f"{eid} {name}" for eid, name in e["entities"])
        out.append(f"  ? \"{e['organization']}\"  "
                   f"shares={','.join(e['tokens'])}  ~ {ents}")
    for org in result["unmatched_sample"]:
        out.append(f"  . \"{org}\"")

    out += ["", "RECOMMENDATION (provisional -- re-band after the adjudication "
            "pass)", "  " + result["recommendation"], ""]
    return "\n".join(out)


def read_only_connect(db_path):
    """Open the store ``mode=ro`` — the containment the ``#74`` precedent asks
    for, and the reason this harness cannot write even by accident.

    The path goes through ``Path.as_uri()`` rather than being pasted into an
    f-string. In a SQLite URI a ``#`` starts the fragment, so a db path
    containing one truncates the URI and takes ``?mode=ro`` with it — the
    connection then opens READ-WRITE with no error, and the one guarantee this
    module makes about the store is silently gone. ``as_uri()`` percent-encodes
    it."""
    return sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro",
                           uri=True)


def report(db_path, rows, state_status=None, now=None, out=sys.stdout,
           unmatched_sample=0):
    """Analyze ``rows`` against the store and print the report."""
    conn = read_only_connect(db_path)
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
    for state, url, accept, parse, cap in (
            ("CA", ca_url, "text/csv", parse_ca_export, None),
            ("WA", wa_url, "application/json", parse_wa_dataset, WA_ROW_CAP)):
        if status:
            time.sleep(sleep)
        try:
            parsed = parse(fetch(url, accept))
        # The whole point of a per-state status is that one dead source cannot
        # cost the other its measurement, so this catches everything a fetch or
        # a parse can raise rather than an enumerated few: URLError and
        # TimeoutError are OSError subclasses, http.client covers a truncated
        # or malformed response, and JSONDecodeError/UnicodeError are
        # ValueError subclasses. An escaping exception would abort main()
        # before one report line printed and throw away a good CA fetch.
        except (OSError, http.client.HTTPException, ValueError,
                csv.Error) as exc:
            status[state] = f"FAILED ({type(exc).__name__}: {exc})"
            continue
        rows.extend(parsed)
        # Socrata returns exactly $limit rows when the dataset is larger, with
        # no error and no marker, so a truncated page is indistinguishable from
        # a complete one -- "trust the array you got, not the count the API
        # advertised" (docs/solutions/store-only-fetcher-dedup-and-quirks.md).
        # A truncated fetch is a partial measurement, never a measured zero.
        if cap is not None and len(parsed) >= cap:
            status[state] = (f"TRUNCATED (hit the {cap}-row page limit; refetch "
                             "with a higher $limit before trusting the count)")
        else:
            status[state] = "ok"
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
    # Prove the store opens read-only BEFORE spending the network run: a typo
    # in --db discovered after the fetch costs the one live pass this spike
    # gets, and the run is sequenced behind the ingestion lock.
    read_only_connect(args.db).close()
    rows, status = collect(args.ca_url, args.wa_url)
    report(args.db, rows, state_status=status,
           unmatched_sample=args.unmatched_sample)
    return 0


if __name__ == "__main__":
    sys.exit(main())
