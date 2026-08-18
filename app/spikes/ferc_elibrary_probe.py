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
     never signs up for a key and never hardcodes one for the ORIGINAL,
     unauthenticated probes below -- an unauthenticated GET is the only read
     ``probe_data_ferc_gov``/``probe_elibrary``/``collect`` will ever
     perform. (Superseded for the catalog-enumeration path added by U8c --
     see below and ``probe_data_ferc_gov_catalog``, which reads
     ``FERC_API_KEY`` from the environment, never hardcoded, R10.8.)
  2. Even disregarding the key, this unit's original claim was that
     data.ferc.gov's own dataset catalog does not include FERC eLibrary
     documents/filings at all -- sourced from its home page
     (``__NEXT_DATA__.props.pageProps.datasets``, fetched live 2026-08-17):
     Active Hydropower Projects, FERC Form 556, the NEPA Schedule for
     Pending Infrastructure Projects, and Company Registration. U8C
     CORRECTION (2026-08-18): that was the homepage's 4-item preview, not
     the real API catalog. An authenticated enumeration of
     ``dataset/{id}/details/`` for id 0..25 (``probe_data_ferc_gov_catalog``,
     live run in the PR body) found 26 real datasets -- and never hit a
     single 404 across the whole bound, so the true catalog may extend past
     id 25 too (this is a bounded spike, not an exhaustive inventory). None
     of the 26 found are eLibrary/docket/filing-shaped: they are structured
     administrative/statistical tables (Form 552 transactions, oil/electric/
     gas annual-charge assessments, MBR authorizations, hydropower
     administrative charges). Two (`MBR Authorizations`, `MBR Operating
     Reserves`) cite a docket number or an Order as an identifier/legal-basis
     field, which a naive keyword match first mis-flagged as eLibrary-
     adjacent -- ``is_elibrary_adjacent`` was tightened after that false
     positive was inspected by hand (see its own docstring). So the "no
     compliant read path" conclusion below stands, now checked against the
     real catalog rather than the homepage.
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
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
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

# -- U8c: authenticated catalog re-verification (follow-up on U8a/#92) ------
# ``FERC_API_KEY`` (env only, R10.8; never hardcoded, never logged) unlocks
# the same dataset/{id}/details/ endpoint probe_data_ferc_gov proved is
# closed anonymously. This is a bounded, polite enumeration -- NOT a
# fetcher -- to answer whether the real catalog (as opposed to the
# marketing homepage's 4-item preview U8a originally relied on) contains
# anything eLibrary/docket/filing-shaped. See ``probe_data_ferc_gov_catalog``.
FERC_API_KEY_ENV = "FERC_API_KEY"
CATALOG_DETAIL_URL_TEMPLATE = "https://api.data.ferc.gov/v1/dataset/{id}/details/"
CATALOG_MAX_ID = 25
CATALOG_MAX_CONSECUTIVE_404S = 3
# Keyword stems suggesting a dataset indexes individual regulatory
# filings/documents (filings, eLibrary, correspondence, adjudications,
# pleadings) rather than a structured administrative/statistical table
# (project lists, transaction tables, registration rosters). A heuristic,
# not a certainty -- see ``is_elibrary_adjacent``. Deliberately EXCLUDES bare
# "docket" and "order": the real catalog (see the live run in the PR body)
# has structured administrative datasets ("MBR Authorizations", "MBR
# Operating Reserves") that cite a docket number as one identifier field or
# an Order as their legal basis without indexing any filings/documents --
# those two words alone are citation-prone, not filing-index-shaped.
ELIBRARY_ADJACENT_KEYWORDS = (
    "filing", "elibrary", "e-library", "correspondence", "adjudicat",
    "pleading", "protest", "complaint", "intervention")

# The minimal record shape a future compliant fetcher would need to produce
# for analyze() to score it. Speculative -- see the module docstring: no real
# live payload was ever reachable to verify this shape against.
FercDocument = namedtuple("FercDocument", ["party_name", "filed_date", "text"])

NEGATIVE_FINDING = (
    "No free, key-less, non-JS read path to FERC eLibrary document content "
    "exists: api.data.ferc.gov requires an API key for every request, and "
    "its real dataset catalog -- verified authenticated in U8c across ids "
    "0-25, not just the marketing homepage's 4-item preview U8a originally "
    "relied on -- still contains no eLibrary/docket/filing-shaped dataset "
    "(structured administrative/statistical tables only: Form 552 "
    "transactions, annual-charge assessments, MBR seller rosters, "
    "hydropower administrative charges); elibrary.ferc.gov's General "
    "Search is a client-rendered Angular SPA whose plain GET returns an "
    "empty <app-root> shell with no server-rendered document rows. This "
    "confirms "
    "the NERC-docket ruling for the FERC-eLibrary half of the source "
    "specifically -- NERC.gov Enforcement still covers CIP notices; only "
    "its eLibrary companion is closed.")


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


# -- U8c: authenticated catalog re-verification ------------------------------

def is_elibrary_adjacent(title, description):
    """Heuristic keyword classification: does this dataset's title/
    description suggest it indexes individual regulatory filings/documents
    (filings, eLibrary, correspondence, adjudications, pleadings) rather
    than a structured administrative/statistical table? Matches on keyword
    stems so plurals count (``filing``/``filings``). Deliberately does NOT
    match bare ``docket``/``order`` -- see ELIBRARY_ADJACENT_KEYWORDS."""
    text = f"{title} {description}".lower()
    return any(re.search(rf"\b{kw}", text) for kw in ELIBRARY_ADJACENT_KEYWORDS)


def _ferc_api_key():
    """The FERC data.ferc.gov key from env only (R10.8). Absent -> clear
    error; callers (main()) catch this to skip the authenticated section
    rather than crashing the whole CLI, since the rest of this probe is
    still useful without a key."""
    key = (os.environ.get(FERC_API_KEY_ENV) or "").strip()
    if not key:
        raise RuntimeError(
            f"{FERC_API_KEY_ENV} is not set. Export the FERC data.ferc.gov "
            f"API key before running the authenticated catalog check "
            f"(secrets live outside the repo, R10.8).")
    return key


def _catalog_detail_request_url(dataset_id, api_key):
    """Build the real request URL for one dataset id -- the key rides in the
    query string the API requires, same as ``eia._page_url``. Callers must
    NEVER log/print/return this; report
    ``CATALOG_DETAIL_URL_TEMPLATE.format(id=dataset_id)`` (bare, no query
    string) instead -- see ``_fetch_dataset_details``."""
    return (CATALOG_DETAIL_URL_TEMPLATE.format(id=dataset_id) + "?" +
            urllib.parse.urlencode([("api_key", api_key)]))


def _fetch_dataset_details(dataset_id, api_key):
    """One authenticated GET against one dataset id. Returns
    ``(status, dataset_or_None)`` where status is ``"ok"``, ``"404"``, or a
    ``"FAILED (...)"`` string. The returned dataset's ``url`` is always the
    bare, key-less form -- the keyed request URL never leaves this
    function."""
    url = _catalog_detail_request_url(dataset_id, api_key)
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        with exc:
            exc.read()
        if exc.code == 404:
            return "404", None
        return f"FAILED ({exc.code} {exc.reason})", None
    except (OSError, http.client.HTTPException) as exc:
        return f"FAILED ({type(exc).__name__}: {exc})", None
    try:
        meta = (json.loads(body).get("metadata") or [{}])[0]
    except (ValueError, AttributeError, IndexError):
        return "FAILED (unparseable response body)", None
    title = " ".join((meta.get("title") or "").split())
    description = " ".join((meta.get("description") or "").split())
    dataset = {
        "id": meta.get("id", dataset_id),
        "title": title,
        "description": description,
        "url": CATALOG_DETAIL_URL_TEMPLATE.format(id=dataset_id),
        "elibrary_adjacent": is_elibrary_adjacent(title, description),
    }
    return "ok", dataset


def probe_data_ferc_gov_catalog(api_key, max_id=CATALOG_MAX_ID,
                                max_consecutive_404s=CATALOG_MAX_CONSECUTIVE_404S):
    """Bounded, polite, sequential authenticated GET across dataset ids
    ``0..max_id``, stopping early once ``max_consecutive_404s`` consecutive
    404s are seen (any non-404 response resets the counter). One GET per id,
    ``POLITENESS_SECONDS`` between requests, honest ``USER_AGENT``. Returns
    ``(datasets, summary)`` -- ``datasets`` is the list of every
    successfully-fetched dataset dict (see ``_fetch_dataset_details``);
    ``summary`` reports how many ids were probed, how many hit, whether
    enumeration stopped early on 404s, and the eLibrary-adjacent count."""
    datasets = []
    consecutive_404s = 0
    probed = 0
    stopped_early = False
    for dataset_id in range(max_id + 1):
        probed += 1
        status, dataset = _fetch_dataset_details(dataset_id, api_key)
        if status == "404":
            consecutive_404s += 1
        else:
            consecutive_404s = 0
            if status == "ok":
                datasets.append(dataset)
        if consecutive_404s >= max_consecutive_404s:
            stopped_early = True
            break
        if dataset_id < max_id:
            time.sleep(POLITENESS_SECONDS)
    return datasets, {
        "ids_probed": probed,
        "hit_count": len(datasets),
        "stopped_early": stopped_early,
        "elibrary_adjacent_count": sum(1 for d in datasets
                                       if d["elibrary_adjacent"]),
    }


def catalog_recommendation(datasets):
    """U8c's own bands (see module docstring): 0 eLibrary-adjacent hits
    STRENGTHENS U8a's existing STOP (the catalog was verified beyond the
    homepage's 4 items and still has nothing eLibrary-adjacent); 1+ hits
    means a new probe is warranted before any U8b build discussion -- never
    an automatic green light."""
    adjacent = [d for d in datasets if d["elibrary_adjacent"]]
    if not adjacent:
        return ("0 eLibrary/docket/filing-shaped datasets in the real "
                "catalog -> STRENGTHENS U8a's existing STOP: the catalog "
                "was verified authenticated, beyond the homepage's 4-item "
                "preview, and still contains nothing eLibrary-adjacent.")
    names = "; ".join(f"id={d['id']} \"{d['title']}\"" for d in adjacent)
    return (f"{len(adjacent)} eLibrary/docket/filing-shaped dataset(s) found "
            f"({names}) -> a NEW PROBE against that dataset is warranted "
            "before any U8b build discussion. This is NOT an automatic "
            "green light.")


def format_catalog_report(datasets, summary, key_status):
    """Plain text, pasteable into a PR body. ``key_status`` is ``"ok"`` or a
    ``"SKIPPED (...)"``/``"FAILED (...)"`` string -- when not ``"ok"`` the
    authenticated section did not run and no dataset list follows."""
    out = ["", "AUTHENTICATED CATALOG ENUMERATION (U8c, R9.6, R5.5) -- "
           "data.ferc.gov, real FERC_API_KEY, bounded sequential GET, "
           "writes nothing", f"  key_status: {key_status}"]
    if key_status != "ok":
        out.append("  Authenticated section skipped -- see key_status above.")
        return "\n".join(out)
    out.append(f"  ids_probed={summary['ids_probed']}  "
               f"hit_count={summary['hit_count']}  "
               f"stopped_early={summary['stopped_early']}  "
               f"elibrary_adjacent_count={summary['elibrary_adjacent_count']}")
    out.append("  datasets found:")
    for d in datasets:
        tag = ("ELIBRARY-ADJACENT" if d["elibrary_adjacent"]
               else "administrative/statistical")
        out.append(f"    id={d['id']}  [{tag}]  {d['title']}")
        out.append(f"      {d['description']}")
        out.append(f"      {d['url']}")
    out += ["", "  RECOMMENDATION: " + catalog_recommendation(datasets)]
    return "\n".join(out)


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

    # U8c: the authenticated catalog check is additive and optional -- an
    # absent FERC_API_KEY skips just this section (clearly, in the report)
    # rather than crashing the whole CLI; the rest of the probe above still
    # ran and reported something useful.
    try:
        key = _ferc_api_key()
    except RuntimeError as exc:
        print(format_catalog_report([], {}, f"SKIPPED ({exc})"))
    else:
        datasets, summary = probe_data_ferc_gov_catalog(key)
        print(format_catalog_report(datasets, summary, "ok"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
