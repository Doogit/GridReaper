"""FERC eLibrary spike (R9.6, R5.5).

MEASUREMENT ONLY -- this module is NOT a fetcher. It produces two counts and a
recommendation, nothing else. It writes nothing to the store, mints no
signals, has no ``runner.cli`` entry point, and is deliberately absent from
``deploy/ingest_pipeline.sh`` (same containment discipline as
``app/spikes/breach_registry_probe.py``, the U5 precedent this module follows
in shape).

WHY A SPIKE. An earlier build (U8, superseded) shipped a FERC eLibrary
fetcher with no measurement gate; its stated fallback value would feed two
classifiers that are either unwired or deferred today. This unit measures
before any future commitment to a real fetcher.

WHAT THIS MODULE ACTUALLY FOUND (read before trusting the "approach" this
unit shipped with). The unit's brief assumed "data.ferc.gov exposes a
free-key API (no key needed for this probe's read)". Verified live against
the real host, both halves of that assumption are wrong:

  1. api.data.ferc.gov requires an API key on EVERY request, with no
     anonymous read tier at all. A bare GET to
     ``https://api.data.ferc.gov/v1/dataset/0/details/`` returns
     ``HTTP 403 {"error":{"code":"API_KEY_MISSING", ...}}`` (see
     ``probe_data_ferc_gov``, reproduced live in the PR body). This module
     never signs up for a key and never hardcodes one -- an unauthenticated
     GET is the only read this spike is willing to perform.
  2. Even disregarding the key, data.ferc.gov's own dataset catalog does not
     include FERC eLibrary documents/filings at all. Per its home page
     (``__NEXT_DATA__.props.pageProps.datasets``, fetched live 2026-08-17),
     the datasets on offer are Active Hydropower Projects, FERC Form 556,
     the NEPA Schedule for Pending Infrastructure Projects, and Company
     Registration -- structured administrative data, not the docket/filing
     full-text index a "who filed what, naming which party" measurement
     needs. A valid data.ferc.gov API key would not unlock eLibrary search.
  3. The actual eLibrary document repository lives at a separate legacy
     system, ``elibrary.ferc.gov``. Its General Search is a client-rendered
     Angular application: a plain GET of a search URL (even one that Google
     has indexed with real result content, meaning it *was* reachable to a
     full browser crawl at some point) returns only an ``<app-root></app-root>``
     shell -- no server-rendered document rows, no key required, nothing to
     parse (see ``probe_elibrary``). The real search results are fetched by
     client-side JS calling an internal API this project has no visibility
     into and no stdlib, non-JS way to reach. The only known alternative is
     an unofficial third-party scraper wrapper (github.com/4very/ferc-
     elibrary-api) -- unsupported, unverified, and outside the "read-only
     GET/RSS/JSON only" + stdlib-only rules this project runs under.

So the live run below is not "we searched the last 24 months and found
nothing" -- it is "no compliant read path into FERC eLibrary content exists
under this project's constraints at all". That is a *harder* stop signal
than a completed zero-count search would be: even a future session willing
to sign up for a data.ferc.gov key gains nothing (finding 2), and reaching
real eLibrary content would require reverse-engineering an undocumented
internal API or running a headless browser, both disqualified outright by
the stdlib-only / GET-only rules this project runs under -- not a "operator
ruling: is a fetcher worth it" question, but a "no fetcher is buildable
under these constraints" one.

THE TWO COUNTS (kept separate per the unit brief, never conflated):
  COUNT 1 (entity_hit_count): distinct watchlist entities whose name appears
    as a document's filer/party and resolves (matched) or is operator-
    confirmed from the review queue -- the same matched + review-queued
    adjudication discipline as U5 (KTD5: resolver-only auto-accept is not
    enough, and a hit is operator-confirmed regardless of how the resolver
    classified the name). This is account-recall-shaped evidence.
  COUNT 2 (cip_document_count): in-window documents whose text contains a
    ``CIP-###`` reference, regardless of party. Tests whether FERC eLibrary
    carries CIP-standard references that NERC.gov Enforcement (a separate,
    already-wired source, ``app/ingest/nerc_enforcement.py``) does not.

Because no document content was ever retrieved (see above), both counts are
0 in the live run not because a completed search came back empty, but
because the search could never run. ``analyze``/``parse_documents`` are
still fully exercised hermetically against canned fixtures shaped like a
hypothetical future eLibrary document record (``party_name``, ``filed_date``,
``text``) so the counting and adjudication logic is proven correct and ready
for the day a real, compliant read path exists.

THRESHOLDS, already agreed (see the unit brief; not re-litigated here):
  0 on both counts   stop, record the negative -- confirming the NERC-docket
                      ruling for the FERC-eLibrary half of the source
                      specifically (NERC.gov Enforcement, a separate wired
                      source, still covers CIP notices; only its eLibrary
                      companion is closed).
  1+ on either count  operator ruling: is the recall rate worth a permanent
                      credentialed-fetcher maintenance surface, given the
                      stored events would still feed no in-scope classifier
                      until a future unit consumes them?

  python -m app.spikes.ferc_elibrary_probe --report [--db PATH]

Live network: sequential, read-only GET, UA
``GridSignals/0.1 (+https://github.com/Doogit/GridSignals)``, politeness
sleep between the two probe requests. Both probes are expected to report a
BLOCKED status, and a report where either comes back "ok" (i.e. an
undocumented access path opened up) is the one outcome worth a second look
before trusting the printed counts.
"""
import argparse
import http.client
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import namedtuple
from datetime import date, datetime, timezone
from pathlib import Path

from app.db.connection import DEFAULT_DB_PATH
from app.resolve import EntityResolver

USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"
POLITENESS_SECONDS = 2.0
HTTP_TIMEOUT = 30

WINDOW_MONTHS = 24

# The endpoint the unit's own brief pointed at -- probed with NO api_key, ever.
# See the module docstring's finding 1: this always returns 403 API_KEY_MISSING.
DATA_FERC_PROBE_URL = "https://api.data.ferc.gov/v1/dataset/0/details/"
# eLibrary's public General Search, plain GET, no key. See finding 3: this
# returns only the Angular shell, never server-rendered document rows.
ELIBRARY_SEARCH_URL = (
    "https://elibrary.ferc.gov/eLibrary/search?q=searchText%3D*"
    "&searchFullText=false&searchDescription=true&dateType=filed_date"
    "&allDates=false")

CIP_RE = re.compile(r"CIP-\d{3}", re.IGNORECASE)

# The minimal record shape a future compliant fetcher would need to produce
# for analyze() to score it. Speculative -- see the module docstring: no real
# live payload was ever reachable to verify this shape against.
FercDocument = namedtuple("FercDocument", ["party_name", "filed_date", "text"])

NEGATIVE_FINDING = (
    "No free, key-less, non-JS read path to FERC eLibrary document content "
    "exists: api.data.ferc.gov requires an API key for every request and its "
    "dataset catalog does not include eLibrary filings anyway (hydropower "
    "licenses, Form 556, NEPA schedule, company registration only); "
    "elibrary.ferc.gov's General Search is a client-rendered Angular SPA "
    "whose plain GET returns an empty <app-root> shell with no server-"
    "rendered document rows. This confirms the NERC-docket ruling for the "
    "FERC-eLibrary half of the source specifically -- NERC.gov Enforcement "
    "still covers CIP notices; only its eLibrary companion is closed.")


# -- parsing (no network) ---------------------------------------------------

def parse_documents(text):
    """JSON array of ``{"party_name", "filed_date", "text"}`` -> FercDocument
    list. A row missing ``party_name`` or ``text`` is dropped, never guessed
    at; ``filed_date`` is optional and an undated row is simply excluded from
    the trailing-window filter in ``analyze`` rather than counted as in- or
    out-of-window."""
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("FERC eLibrary payload is not a JSON array of records")
    docs = []
    for rec in data:
        if not isinstance(rec, dict):
            continue
        party = " ".join((rec.get("party_name") or "").split())
        body = rec.get("text") or ""
        if not party or not body:
            continue
        filed = " ".join((rec.get("filed_date") or "").split())
        docs.append(FercDocument(party, filed, body))
    return docs


# -- analysis (no network) ---------------------------------------------------

def window_cutoff(now):
    """Earliest filed date inside the trailing 24-month window (inclusive).
    The Feb-29 anniversary falls back to Feb 28 rather than raising (same
    derivation as ``breach_registry_probe.window_cutoff``)."""
    year = now.year - WINDOW_MONTHS // 12
    try:
        return now.replace(year=year).date().isoformat()
    except ValueError:
        return now.replace(year=year, day=28).date().isoformat()


def analyze(conn, docs, source_status=None, now=None):
    """Run every in-window document through the EntityResolver (count 1) and
    the CIP-### regex (count 2), independently. ``conn`` is read-only;
    nothing is written."""
    now = now or datetime.now(timezone.utc)
    cutoff = window_cutoff(now)

    in_window = [d for d in docs if d.filed_date and d.filed_date >= cutoff]
    undated = sum(1 for d in docs if not d.filed_date)

    # COUNT 2: CIP-### reference, regardless of party or resolution.
    cip_docs = [d for d in in_window if CIP_RE.search(d.text)]
    cip_standards = sorted({m.upper() for d in cip_docs
                            for m in CIP_RE.findall(d.text)})

    # COUNT 1: filer/party name resolution, same discipline as U5.
    names = {r["entity_id"]: r["name"] for r in
             conn.execute("SELECT entity_id, name FROM watchlist_entities")}
    resolver = EntityResolver(conn)

    hits, review, unmatched = {}, [], []
    for d in in_window:
        res = resolver.resolve(name=d.party_name, context_text=d.text)
        if res.status == "matched":
            hit = hits.setdefault(res.entity_id, {
                "entity_id": res.entity_id,
                "entity_name": names.get(res.entity_id, ""),
                "party_names": set(), "method": res.method})
            hit["party_names"].add(d.party_name)
        elif res.status == "review":
            review.append({
                "party_name": d.party_name, "reason": res.method,
                "candidates": [(eid, names.get(eid, ""), score)
                               for eid, score in res.candidates]})
        else:
            unmatched.append(d.party_name)

    hit_list = [{"entity_id": h["entity_id"], "entity_name": h["entity_name"],
                 "party_names": sorted(h["party_names"]), "method": h["method"]}
                for h in sorted(hits.values(), key=lambda h: h["entity_id"])]

    status = source_status or {}
    return {
        "as_of": now.isoformat(),
        "cutoff": cutoff,
        "source_status": status,
        "total_documents": len(docs),
        "in_window_documents": len(in_window),
        "undated_documents": undated,
        "entity_hit_count": len(hit_list),
        "entity_hits": hit_list,
        "review": sorted(review, key=lambda r: r["party_name"]),
        "unmatched_count": len(unmatched),
        "cip_document_count": len(cip_docs),
        "cip_standards_referenced": cip_standards,
        "recommendation": recommendation(len(hit_list), len(cip_docs), status),
    }


def recommendation(entity_hit_count, cip_hit_count, source_status):
    """The band text, plus the blocked-access caveat when a source never
    yielded document content. A blocked source is never a measured zero."""
    blocked = sorted(s for s, v in source_status.items()
                     if not v.startswith("ok"))
    if entity_hit_count == 0 and cip_hit_count == 0:
        text = ("0 on both counts -> STOP, do not build the FERC-eLibrary "
                "fetcher. Record the negative, precisely: " + NEGATIVE_FINDING)
    else:
        text = (f"entity_hit_count={entity_hit_count} "
                f"cip_document_count={cip_hit_count} -> OPERATOR RULING "
                "required: is the recall rate worth a permanent "
                "credentialed-fetcher maintenance surface, given the stored "
                "events would still feed no in-scope classifier until a "
                "future unit consumes them?")
    if blocked:
        text = ("BLOCKED MEASUREMENT -- " +
                "; ".join(f"{s}: {source_status[s]}" for s in blocked) +
                ". Both counts are 0 because no document content was ever "
                "retrieved, not because a completed search found nothing. " +
                text)
    return text


def format_report(result):
    """Plain text, pasteable into a PR body."""
    out = ["FERC eLibrary probe (R9.6, R5.5) -- read-only, writes nothing",
           f"as_of={result['as_of']}  window=trailing {WINDOW_MONTHS} months  "
           f"filed_date >= {result['cutoff']}", ""]

    status = result["source_status"]
    if status:
        out.append("source status")
        for src in sorted(status):
            out.append(f"  {src}: {status[src]}")
        out.append("")

    out.append(f"context: total documents={result['total_documents']}  "
               f"in-window documents={result['in_window_documents']}  "
               f"undated={result['undated_documents']}")
    out.append("")

    out += [f"COUNT 1 (account-recall-shaped): distinct watchlist entities "
            f"named as filer/party: {result['entity_hit_count']}",
            "  a hit is a distinct watchlist entity whose name appears as a "
            "document's filer/party and",
            "  resolves (matched), or is operator-confirmed from the review "
            "queue below (KTD5 discipline).", ""]
    for h in result["entity_hits"]:
        out.append(f"  {h['entity_id']}  {h['entity_name']}  "
                   f"method={h['method']}  as "
                   f"{'; '.join(h['party_names'])}")

    out += ["", f"review-queued party names: {len(result['review'])}",
            "  ADJUDICATE THESE -- a hit is operator-confirmed regardless of "
            "how the resolver classified it."]
    for r in result["review"]:
        cands = ", ".join(f"{eid} {name} ({score})"
                          for eid, name, score in r["candidates"]) or "none"
        out.append(f"  \"{r['party_name']}\"  reason={r['reason']}  "
                   f"candidates={cands}")
    out.append(f"  unmatched party names (off-list): {result['unmatched_count']}")

    out += ["", f"COUNT 2 (independent of party): documents with a CIP-### "
            f"reference: {result['cip_document_count']}",
            "  a document counts here whether or not its party resolved -- "
            "never conflated with count 1.",
            "  standards referenced: " +
            (", ".join(result["cip_standards_referenced"]) or "none"), ""]

    out += ["RECOMMENDATION", "  " + result["recommendation"], ""]
    return "\n".join(out)


def read_only_connect(db_path):
    """Open the store ``mode=ro`` (same containment as
    ``breach_registry_probe.read_only_connect``, including the ``as_uri()``
    fragment-truncation guard)."""
    return sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro",
                           uri=True)


def report(db_path, docs, source_status=None, now=None, out=sys.stdout):
    """Analyze ``docs`` against the store and print the report."""
    conn = read_only_connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = analyze(conn, docs, source_status=source_status, now=now)
    finally:
        conn.close()
    print(format_report(result), file=out)
    return result


# -- fetch (live network; sequential, read-only GET; no API key, ever) ------

def probe_data_ferc_gov(url=DATA_FERC_PROBE_URL):
    """Anonymous GET against the endpoint the unit's brief pointed at. NEVER
    sends an api_key -- an unauthenticated read is the only kind this spike
    will perform. Returns a status string; ``ok`` only if data.ferc.gov ever
    starts allowing anonymous reads (it does not, as of this probe: see the
    module docstring's finding 1)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            resp.read()
            return "ok (unexpected: anonymous read succeeded -- investigate)"
    except urllib.error.HTTPError as exc:
        with exc:
            body = exc.read().decode("utf-8", errors="replace").strip()
        return f"BLOCKED ({exc.code} {exc.reason}: {body[:200]})"
    except (OSError, http.client.HTTPException) as exc:
        return f"FAILED ({type(exc).__name__}: {exc})"


def probe_elibrary(url=ELIBRARY_SEARCH_URL):
    """Plain GET of eLibrary's General Search. No key is ever required for
    this URL, but the response is a client-rendered Angular shell with no
    server-rendered document rows to parse (finding 3)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (OSError, http.client.HTTPException) as exc:
        return f"FAILED ({type(exc).__name__}: {exc})"
    if "<app-root" in body:
        return (f"BLOCKED (client-rendered Angular SPA shell only -- GET "
                f"returned {len(body)} bytes with no server-rendered "
                "document rows; General Search runs entirely client-side "
                "after page load and has no documented public API)")
    return f"ok (unexpected: {len(body)} bytes without an <app-root> shell -- investigate)"


def collect():
    """Sequential, read-only GET probes against both candidate access paths.
    Always returns ``([], status)`` under current project constraints: see
    the module docstring for why neither path can yield document content
    without either an API key (never supplied) or JS execution (never
    performed)."""
    status = {"data_ferc_gov": probe_data_ferc_gov()}
    time.sleep(POLITENESS_SECONDS)
    status["elibrary_general_search"] = probe_elibrary()
    return [], status


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Measure whether FERC eLibrary documents name a "
                    "watchlist entity or reference a CIP-### standard. "
                    "Read-only; writes nothing, mints nothing, is not a "
                    "fetcher.")
    parser.add_argument("--report", action="store_true", required=True,
                        help="the only mode: probe, measure, print")
    parser.add_argument("--db", default=DEFAULT_DB_PATH,
                        help=f"store to read (default {DEFAULT_DB_PATH})")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")
    # Prove the store opens read-only before spending the network run.
    read_only_connect(args.db).close()
    docs, status = collect()
    report(args.db, docs, source_status=status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
