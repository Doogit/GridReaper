"""USAspending.gov spike (R1, R4 of the combo-engine/account-signal plan).

MEASUREMENT ONLY -- this module is NOT a fetcher. It produces a distinct-
account count, a description-richness sample, and a recommendation, nothing
else. It writes nothing to the store, mints no signals, has no
``runner.cli`` entry point, and is deliberately absent from
``deploy/ingest_pipeline.sh`` (same containment discipline as
``app/spikes/ferc_elibrary_probe.py`` / ``app/spikes/breach_registry_probe.py``,
the U5/U8 precedent this module follows in shape).

WHAT THIS MEASURES. Distinct watchlist accounts with a qualifying DOE/CISA/
DHS financial-assistance award (grant or cooperative agreement) in the
trailing 24 months, gating whether a future unit (U4, not built here) wires
a ``capital_project`` trigger off USAspending.gov. Per the unit brief, this
probe ALSO samples the award description text of the qualifying, matched
awards and reports whether that text carries enough free content to support
a later keyword-absence check (U11) -- a probe that only counted awards
could clear the account threshold while every description was a two-word
boilerplate program label, and that risk gets measured here, not assumed.

THE ENDPOINT. ``api.usaspending.gov``'s award search
(``/api/v2/search/spending_by_award/``) is keyless and public -- confirmed
live: an anonymous POST with no ``Authorization`` header and no API key
returns real award rows (verified against DOE awards to General Atomics, US
SFR Owner LLC, X Energy). ``api.usaspending.gov/robots.txt`` itself 404s
(no crawl-restriction file is published on the API subdomain at all); the
main ``www.usaspending.gov/robots.txt`` disallows only query-string and
``.php`` site-search pages, which this probe never touches. USAspending.gov
is the official U.S. Treasury/DATA Act spending-transparency portal --
federal award data published there is U.S. government work (public domain)
and the JSON API exists specifically for public programmatic read access;
no separate ToS page describing the API could be reached to quote, but that
combination (keyless, no crawl restriction on the API host, statutorily
mandated public transparency data) is the standard signal this project's
other spikes treat as clear for a read-only, non-scraping GET-shaped
measurement.

JUDGMENT CALL -- POST, not GET (flag for the operator). CLAUDE.md's R3.4
says source access MUST be read-only GET/RSS/JSON/bulk-download. USAspending
has no GET-based filtered search: ``/api/v2/search/spending_by_award/`` only
accepts POST with a JSON filter body (this is true of the whole public
search API, not a workaround this module invented -- there is no
query-string equivalent for agency+date filtering). This module sends that
POST with a plain JSON body, an honest ``User-Agent``, no auth, no
session/cookies, and no form fields -- i.e. it is a read-only JSON query in
every way except the HTTP verb the API demands. That is judged here to be
within R3.4's spirit (a JSON API read, not a form submission or scrape) but
NOT a literal GET, so it is called out explicitly rather than silently
assumed compliant: if U4 is scoped to build a real ``capital_project``
fetcher against this same endpoint, the operator should rule on this
verb question before that unit ships, the same way U5/U6/U7's robots.txt/
ToS check is a standing step before a permanent fetcher, not just a probe.

QUALIFYING AGENCY. An award qualifies when its ``Awarding Agency`` (toptier)
is Department of Energy or Department of Homeland Security -- DHS as
toptier already covers CISA awards (CISA is a DHS sub-agency), and the
``Awarding Sub Agency`` field is checked too in case a future response ever
carries CISA under a different toptier label. ``ASSISTANCE_AWARD_TYPE_CODES``
scopes the search to grants/cooperative agreements (project-grant shaped
financial assistance), the award type a capital-project signal is actually
drawn from -- loans, direct payments, and insurance are out of scope for
this probe's "capital project award" definition, a simplifying choice
documented here rather than left implicit.

THE HIT DEFINITION. A hit is a distinct watchlist entity whose recipient
name, on at least one in-window qualifying-agency award, resolves (matched)
via ``EntityResolver`` -- the same matched/review/unmatched adjudication
discipline as ``app/spikes/ferc_elibrary_probe.py``'s ``analyze()``: a
below-threshold or ambiguous match is review-queued for the operator, never
silently counted or silently dropped.

QUERY SCOPING (live-run correction). An unscoped DOE/DHS-wide search over
the assistance award types above is NATIONAL: it returns thousands of
awards to research universities, national labs, and small businesses with
no watchlist relationship at all (observed live: 1000+ awards after 5 pages
per agency, only 2 recognisable watchlist names, still truncated -- the
bounded pagination could never catch up). ``collect()`` therefore sends the
watchlist's active entity names and aliases as ``recipient_search_text``, a
server-side substring filter -- a volume-reduction measure only, not a
matching authority: ``EntityResolver`` still adjudicates every recipient
name this returns (matched/review/unmatched), so a subsidiary name the
filter happens to substring-match (e.g. "Georgia Power Company" surfacing
under "Southern Company") is never auto-accepted on the strength of the
search filter alone.

THRESHOLD, per the unit brief:
  >= 5 distinct accounts   BUILD the capital_project trigger (U4).
  <  5 distinct accounts   DO NOT BUILD U4.
The richness characterization is reported alongside, never folded silently
into the same yes/no: a count that clears 5 over boilerplate-only
descriptions is not the same finding as a count that clears 5 over
substantive free text, and U11's keyword-absence check needs the latter.

  python -m app.spikes.usaspending_probe --report [--db PATH]

Live network: sequential, read-only POST (see judgment call above), UA
``GridSignals/0.1 (+https://github.com/Doogit/GridSignals)``, politeness
sleep between requests, bounded pagination (``MAX_PAGES`` per agency -- a
bounded spike, not an exhaustive inventory, same discipline as
``ferc_elibrary_probe.py``'s ``CATALOG_MAX_ID`` bound).
"""
import argparse
import http.client
import json
import statistics
import sqlite3
import sys
import time
import urllib.request
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

from app.db.connection import DEFAULT_DB_PATH
from app.resolve import EntityResolver

USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"
POLITENESS_SECONDS = 2.0
HTTP_TIMEOUT = 30

WINDOW_MONTHS = 24
BUILD_THRESHOLD = 5

SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
PAGE_LIMIT = 100
MAX_PAGES = 5   # bounded spike: up to 500 awards/agency, not an exhaustive pull

FIELDS = ["Award ID", "Recipient Name", "Awarding Agency",
          "Awarding Sub Agency", "Start Date", "Description"]

# Grants + cooperative agreements: the financial-assistance award types a
# "capital project" signal is actually drawn from. Loans/direct payments/
# insurance are out of scope for this probe's definition (see docstring).
ASSISTANCE_AWARD_TYPE_CODES = ("02", "03", "04", "05")

# Live-discovered, undocumented API behavior: a ``recipient_search_text``
# array of 14+ terms 503s near-instantly (0.3-0.4s -- too fast to be a slow
# query timing out; this reads as an edge/gateway rejection on array size,
# not an origin computation limit). 12 terms succeeded in testing; 10 is
# used here for a safety margin, at the cost of more, smaller requests.
SEARCH_TERM_BATCH_SIZE = 10

# DHS toptier already covers CISA (a DHS sub-agency); the sub-agency name is
# also checked defensively in ``is_qualifying_agency`` in case a future
# response ever surfaces CISA under a different toptier label.
QUALIFYING_TOPTIER_AGENCIES = frozenset({
    "Department of Energy", "Department of Homeland Security"})
# Reviewer-caught inconsistency: the CISA sub-agency check below is already
# casefold()-insensitive; the toptier check now matches that discipline
# rather than requiring an exact-case match against live-observed casing.
_QUALIFYING_TOPTIER_AGENCIES_CASEFOLD = frozenset(
    a.casefold() for a in QUALIFYING_TOPTIER_AGENCIES)
CISA_SUBAGENCY_MARKER = "Cybersecurity and Infrastructure Security Agency"

BOILERPLATE_WORD_THRESHOLD = 6   # "Pipeline Safety Grant" is 3 words
RICHNESS_SAMPLE_SIZE = 5

# award_id, recipient_name, awarding_agency, awarding_subagency, award_date
# (ISO, from "Start Date"), description -- nothing else survives parsing.
AwardRecord = namedtuple(
    "AwardRecord", ["award_id", "recipient_name", "awarding_agency",
                    "awarding_subagency", "award_date", "description"])


# -- parsing (no network) ----------------------------------------------------

def _clean(text):
    """Whitespace-normalized string, or "" for anything that isn't real
    text (None, or -- reviewer-caught: a non-string value like a nested
    object/list/number, which a future USAspending schema tweak could send
    for any field). A malformed field is dropped, never fabricated into a
    string via ``str(text)`` and never allowed to raise -- consistent with
    ``parse_awards``'s "row missing a required field is dropped" discipline
    one level up."""
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())


def parse_awards(text):
    """One page of USAspending ``spending_by_award`` JSON response ->
    AwardRecord list. The real shape (verified live) is
    ``{"results": [...], ...}``; a row missing ``Recipient Name`` or
    ``Awarding Agency`` is dropped, never guessed at."""
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError(
            "USAspending payload is not a {'results': [...]} object; "
            "keys=" + repr(sorted(data.keys())) if isinstance(data, dict)
            else "USAspending payload is not a JSON object")
    records = []
    for rec in data["results"]:
        if not isinstance(rec, dict):
            continue
        recipient = _clean(rec.get("Recipient Name"))
        agency = _clean(rec.get("Awarding Agency"))
        if not recipient or not agency:
            continue
        records.append(AwardRecord(
            award_id=_clean(rec.get("Award ID")),
            recipient_name=recipient,
            awarding_agency=agency,
            awarding_subagency=_clean(rec.get("Awarding Sub Agency")),
            award_date=_clean(rec.get("Start Date")),
            description=_clean(rec.get("Description"))))
    return records


def is_qualifying_agency(award):
    if award.awarding_agency.casefold() in _QUALIFYING_TOPTIER_AGENCIES_CASEFOLD:
        return True
    return CISA_SUBAGENCY_MARKER.casefold() in award.awarding_subagency.casefold()


# -- analysis (no network) ---------------------------------------------------

def window_cutoff(now):
    """Earliest award start date inside the trailing 24-month window
    (inclusive). The Feb-29 anniversary falls back to Feb 28 rather than
    raising (same derivation as the other spikes' ``window_cutoff``)."""
    year = now.year - WINDOW_MONTHS // 12
    try:
        return now.replace(year=year).date().isoformat()
    except ValueError:
        return now.replace(year=year, day=28).date().isoformat()


def richness_sample(awards):
    """Characterize how much free text the qualifying, matched awards'
    descriptions carry -- U11's keyword-absence check depends on this being
    substantive, not a two-word program label. ``awards`` should be the
    qualifying, in-window, MATCHED population (the one a future
    ``capital_project`` classifier would actually emit signals from)."""
    with_desc = [(a, len(a.description.split())) for a in awards if a.description]
    missing = len(awards) - len(with_desc)
    if not with_desc:
        return {"sampled": 0, "missing_description": missing,
                "avg_words": 0.0, "median_words": 0.0, "boilerplate_count": 0,
                "characterization": (
                    "NO DESCRIPTIONS -- every qualifying, matched award record "
                    "is missing free text; U11's keyword-absence check would "
                    "have nothing to evaluate."),
                "examples": []}
    word_counts = [wc for _, wc in with_desc]
    boilerplate_count = sum(1 for wc in word_counts
                            if wc < BOILERPLATE_WORD_THRESHOLD)
    boilerplate_fraction = boilerplate_count / len(word_counts)
    if boilerplate_fraction >= 0.5:
        characterization = (
            f"BOILERPLATE-DOMINATED -- {boilerplate_count}/{len(word_counts)} "
            f"sampled descriptions are under {BOILERPLATE_WORD_THRESHOLD} "
            "words; U11's keyword-absence check would not be meaningful over "
            "this population as-is.")
    else:
        characterization = (
            f"SUBSTANTIVE -- only {boilerplate_count}/{len(word_counts)} "
            f"sampled descriptions are under {BOILERPLATE_WORD_THRESHOLD} "
            "words; free text is available for U11's keyword-absence check.")
    examples = sorted(with_desc, key=lambda t: t[1])[:RICHNESS_SAMPLE_SIZE]
    return {
        "sampled": len(word_counts), "missing_description": missing,
        "avg_words": round(sum(word_counts) / len(word_counts), 1),
        "median_words": statistics.median(word_counts),
        "boilerplate_count": boilerplate_count,
        "characterization": characterization,
        "examples": [{"award_id": a.award_id, "recipient_name": a.recipient_name,
                      "word_count": wc, "description": a.description[:200]}
                     for a, wc in examples],
    }


def analyze(conn, awards, source_status=None, now=None):
    """Run every in-window, qualifying-agency award through the
    EntityResolver. ``conn`` is read-only; nothing is written."""
    now = now or datetime.now(timezone.utc)
    cutoff = window_cutoff(now)

    in_window = [a for a in awards if a.award_date and a.award_date >= cutoff]
    undated = sum(1 for a in awards if not a.award_date)
    qualifying = [a for a in in_window if is_qualifying_agency(a)]
    excluded_agency_count = len(in_window) - len(qualifying)

    names = {r["entity_id"]: r["name"] for r in
             conn.execute("SELECT entity_id, name FROM watchlist_entities")}
    resolver = EntityResolver(conn)

    # Reviewer-caught: resolve once per DISTINCT recipient name, not once per
    # award. A recipient name is deterministic under the resolver regardless
    # of which of its awards asks -- resolving per-award duplicated
    # review-queued/unmatched entries (and inflated their printed counts) for
    # any recipient with more than one qualifying award, without changing
    # the entity-matched count (which was already per-entity via
    # hits.setdefault). The first award's description is used as
    # context_text; later awards for the same name never get a second,
    # independent resolution pass.
    by_recipient = {}
    for a in qualifying:
        by_recipient.setdefault(a.recipient_name, []).append(a)

    hits, review, unmatched = {}, [], []
    for recipient_name in sorted(by_recipient):
        recipient_awards = by_recipient[recipient_name]
        res = resolver.resolve(name=recipient_name,
                               context_text=recipient_awards[0].description)
        if res.status == "matched":
            hit = hits.setdefault(res.entity_id, {
                "entity_id": res.entity_id,
                "entity_name": names.get(res.entity_id, ""),
                "recipient_names": set(), "awards": [], "method": res.method})
            hit["recipient_names"].add(recipient_name)
            hit["awards"].extend(recipient_awards)
        elif res.status == "review":
            review.append({
                "recipient_name": recipient_name, "reason": res.method,
                "candidates": [(eid, names.get(eid, ""), score)
                               for eid, score in res.candidates]})
        else:
            unmatched.append(recipient_name)

    hit_list = [{"entity_id": h["entity_id"], "entity_name": h["entity_name"],
                 "recipient_names": sorted(h["recipient_names"]),
                 "award_count": len(h["awards"]), "method": h["method"]}
                for h in sorted(hits.values(), key=lambda h: h["entity_id"])]
    matched_awards = [a for h in hits.values() for a in h["awards"]]
    richness = richness_sample(matched_awards)

    status = source_status or {}
    return {
        "as_of": now.isoformat(),
        "cutoff": cutoff,
        "source_status": status,
        "total_awards": len(awards),
        "in_window_awards": len(in_window),
        "undated_awards": undated,
        "qualifying_agency_awards": len(qualifying),
        "excluded_agency_awards": excluded_agency_count,
        "distinct_account_count": len(hit_list),
        "hits": hit_list,
        "review": sorted(review, key=lambda r: r["recipient_name"]),
        "unmatched_count": len(unmatched),
        "richness": richness,
        "recommendation": recommendation(len(hit_list), richness, status),
    }


def recommendation(distinct_account_count, richness, source_status):
    """The band text, plus the richness caveat and the blocked-agency
    caveat. A blocked/failed agency fetch is never a measured zero."""
    failed = sorted(s for s, v in source_status.items()
                    if not v.startswith("ok"))
    if distinct_account_count >= BUILD_THRESHOLD:
        text = (f"{distinct_account_count} distinct watchlist accounts "
                f"(>= {BUILD_THRESHOLD}) -> BUILD the capital_project "
                "trigger (U4).")
    else:
        text = (f"{distinct_account_count} distinct watchlist accounts "
                f"(< {BUILD_THRESHOLD}) -> DO NOT BUILD the capital_project "
                "trigger (U4).")
    text += " " + richness["characterization"]
    if failed:
        text = ("PARTIAL MEASUREMENT -- " +
                "; ".join(f"{s}: {source_status[s]}" for s in failed) +
                ". This is not a measured count for that agency; re-run "
                "before recording any recommendation. " + text)
    return text


def format_report(result):
    """Plain text, pasteable into a PR body."""
    out = ["USAspending.gov probe (R1, R4) -- read-only, writes nothing",
           f"as_of={result['as_of']}  window=trailing {WINDOW_MONTHS} months  "
           f"award start date >= {result['cutoff']}", ""]

    status = result["source_status"]
    if status:
        out.append("source status")
        for src in sorted(status):
            out.append(f"  {src}: {status[src]}")
        out.append("")

    out.append(f"context: total awards={result['total_awards']}  "
               f"in-window={result['in_window_awards']}  "
               f"undated={result['undated_awards']}  "
               f"qualifying-agency (DOE/CISA/DHS)={result['qualifying_agency_awards']}  "
               f"excluded (other agency)={result['excluded_agency_awards']}")
    out.append("")

    out.append(f"DISTINCT ACCOUNT COUNT: {result['distinct_account_count']}")
    out.append("  a hit is a distinct watchlist entity whose recipient name, "
               "on at least one in-window")
    out.append("  qualifying-agency award, resolves (matched) via the "
               "EntityResolver.")
    out.append("")
    for h in result["hits"]:
        out.append(f"  {h['entity_id']}  {h['entity_name']}  "
                   f"awards={h['award_count']}  method={h['method']}  as "
                   f"{'; '.join(h['recipient_names'])}")

    out += ["", f"review-queued recipient names: {len(result['review'])}",
            "  ADJUDICATE THESE -- a hit is operator-confirmed regardless of "
            "how the resolver classified it."]
    for r in result["review"]:
        cands = ", ".join(f"{eid} {name} ({score})"
                          for eid, name, score in r["candidates"]) or "none"
        out.append(f"  \"{r['recipient_name']}\"  reason={r['reason']}  "
                   f"candidates={cands}")
    out.append(f"  unmatched recipient names (off-list): "
               f"{result['unmatched_count']}")

    rich = result["richness"]
    out += ["", "DESCRIPTION RICHNESS (qualifying, matched awards only)",
            f"  sampled={rich['sampled']}  missing_description="
            f"{rich['missing_description']}  avg_words={rich['avg_words']}  "
            f"median_words={rich['median_words']}  "
            f"boilerplate_count={rich['boilerplate_count']}",
            "  " + rich["characterization"]]
    if rich["examples"]:
        out.append("  shortest-description examples:")
        for ex in rich["examples"]:
            out.append(f"    [{ex['word_count']}w] {ex['award_id']} "
                       f"{ex['recipient_name']}: {ex['description']}")

    out += ["", "RECOMMENDATION", "  " + result["recommendation"], ""]
    return "\n".join(out)


def read_only_connect(db_path):
    """Open the store ``mode=ro`` (same containment as the other spikes,
    including the ``as_uri()`` fragment-truncation guard)."""
    return sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro",
                           uri=True)


def report(db_path, awards, source_status=None, now=None, out=sys.stdout):
    """Analyze ``awards`` against the store and print the report."""
    conn = read_only_connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = analyze(conn, awards, source_status=source_status, now=now)
    finally:
        conn.close()
    print(format_report(result), file=out)
    return result


# -- fetch (live network; sequential, read-only JSON POST; see the module
#    docstring's judgment-call note on GET vs POST) --------------------------

def watchlist_search_terms(conn):
    """Distinct ACTIVE watchlist entity names + aliases (R8.7: a disabled
    entity is unresolvable anyway, matching ``EntityResolver``'s own
    active-only scope). This is a volume-reduction filter only, sent as
    ``recipient_search_text`` -- without it, a DOE/DHS-wide award search
    returns thousands of awards to research universities, national labs and
    unrelated small businesses nationwide, most of them never a watchlist
    entity, and the bounded pagination below truncates before reaching a
    trustworthy count (observed live: 1000+ awards, only 2 recognisable
    watchlist names, still truncated). ``EntityResolver`` (in ``analyze()``)
    remains the SOLE matching authority -- this filter can surface a
    subsidiary name it happens to substring-match (e.g. "Georgia Power
    Company" under "Southern Company"), and that name still goes through
    the normal matched/review/unmatched adjudication, never an auto-accept
    on the strength of this filter alone."""
    terms = set()
    for r in conn.execute(
            "SELECT name FROM watchlist_entities WHERE active = 1"):
        if r["name"]:
            terms.add(r["name"])
    for r in conn.execute(
            "SELECT entity_aliases.alias FROM entity_aliases "
            "JOIN watchlist_entities USING (entity_id) "
            "WHERE watchlist_entities.active = 1"):
        if r["alias"]:
            terms.add(r["alias"])
    return sorted(terms)


def _search_request_body(agency_name, start_date, end_date, page,
                         search_terms=None):
    filters = {
        "award_type_codes": list(ASSISTANCE_AWARD_TYPE_CODES),
        "time_period": [{"start_date": start_date, "end_date": end_date}],
        "agencies": [{"type": "awarding", "tier": "toptier",
                     "name": agency_name}],
    }
    if search_terms:
        filters["recipient_search_text"] = list(search_terms)
    return json.dumps({
        "filters": filters,
        "fields": FIELDS,
        "page": page,
        "limit": PAGE_LIMIT,
        "sort": "Start Date",   # must be one of FIELDS -- API 400s otherwise
        "order": "desc",
    }).encode("utf-8")


def fetch_page(agency_name, start_date, end_date, page, search_terms=None):
    """One POST for one page of one agency's award search. Returns the raw
    JSON text (parsed by ``parse_awards`` at the caller)."""
    req = urllib.request.Request(
        SEARCH_URL, data=_search_request_body(agency_name, start_date,
                                              end_date, page, search_terms),
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json",
                 "Accept": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_agency(agency_name, start_date, end_date, search_terms=None,
                 max_pages=MAX_PAGES, sleep=POLITENESS_SECONDS):
    """Sequential, bounded pagination for one agency. Returns
    ``(awards, status)`` -- ``status`` is ``"ok"``, ``"TRUNCATED (...)"`` if
    ``max_pages`` was exhausted without reaching a short page, or a
    ``"FAILED (...)"``/``"PARTIAL (...)"`` string. A page-level failure
    never silently drops earlier pages: whatever was collected before the
    failure is still returned, tagged as partial rather than as a clean
    count. ``sleep`` is a seam for hermetic tests (pass 0), not a live knob."""
    awards = []
    for page in range(1, max_pages + 1):
        if page > 1:
            time.sleep(sleep)
        try:
            text = fetch_page(agency_name, start_date, end_date, page,
                              search_terms)
            page_awards = parse_awards(text)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            if awards:
                return awards, (f"PARTIAL (FAILED at page {page}: "
                                f"{type(exc).__name__}: {exc})")
            return awards, f"FAILED ({type(exc).__name__}: {exc})"
        awards.extend(page_awards)
        if len(page_awards) < PAGE_LIMIT:
            return awards, "ok"
    return awards, (f"TRUNCATED (hit the {max_pages}-page bound; refetch "
                    "with a higher MAX_PAGES before trusting the count)")


def _combine_batch_statuses(statuses):
    """Roll up one status string per search-term batch into one status for
    the agency. A batch-count breakdown is kept in the text so a partial
    failure is never misread as a clean "ok". Reviewer-caught: a batch that
    hit MAX_PAGES (``TRUNCATED``) is a different condition from one whose
    request actually failed -- both are "not ok", but calling a truncated
    batch "failed" is misleading, so the breakdown names each kind
    separately rather than collapsing everything non-"ok" into "failed"."""
    bad = [s for s in statuses if s != "ok"]
    if not bad:
        return "ok"
    truncated = sum(1 for s in bad if s.startswith("TRUNCATED"))
    other = len(bad) - truncated
    parts = []
    if truncated:
        parts.append(f"{truncated} truncated")
    if other:
        parts.append(f"{other} failed")
    breakdown = ", ".join(parts)
    if len(bad) == len(statuses):
        return (f"FAILED (all {len(statuses)} recipient-search-text "
                f"batches did not report ok -- {breakdown}; "
                f"first: {bad[0]})")
    return (f"PARTIAL ({len(bad)}/{len(statuses)} recipient-search-text "
            f"batches did not report ok -- {breakdown}; first: {bad[0]})")


def fetch_agency_batched(agency_name, start_date, end_date, search_terms,
                         max_pages=MAX_PAGES, sleep=POLITENESS_SECONDS,
                         batch_size=SEARCH_TERM_BATCH_SIZE):
    """``fetch_agency`` chunked over ``search_terms`` in groups of
    ``batch_size`` (see ``SEARCH_TERM_BATCH_SIZE``'s live-discovered API
    limit). Sequential across batches too -- one POST at a time, politeness
    sleep between every request, batches included. Returns
    ``(awards, status)`` for the whole agency, deduplicated on
    ``award_id`` (a recipient can legitimately appear in more than one
    batch's search terms only if their name substring-matches two different
    watchlist terms, which would otherwise double-count one real award)."""
    if not search_terms:
        return fetch_agency(agency_name, start_date, end_date,
                            max_pages=max_pages, sleep=sleep)
    batches = [search_terms[i:i + batch_size]
               for i in range(0, len(search_terms), batch_size)]
    seen_ids, awards, statuses = set(), [], []
    for i, batch in enumerate(batches):
        if i > 0:
            time.sleep(sleep)
        batch_awards, batch_status = fetch_agency(
            agency_name, start_date, end_date, search_terms=batch,
            max_pages=max_pages, sleep=sleep)
        statuses.append(batch_status)
        for a in batch_awards:
            key = a.award_id or (a.recipient_name, a.award_date, a.description)
            if key not in seen_ids:
                seen_ids.add(key)
                awards.append(a)
    return awards, _combine_batch_statuses(statuses)


def collect(conn, now=None, sleep=POLITENESS_SECONDS):
    """Fetch every qualifying agency in sequence, scoped to the watchlist's
    names/aliases via batched ``recipient_search_text`` queries. Returns
    ``(awards, source_status)``; an agency that fails contributes whatever it
    already collected and a non-"ok" status, never a silent zero. ``sleep``
    is a seam for hermetic tests (pass 0), not a live knob."""
    now = now or datetime.now(timezone.utc)
    end_date = now.date().isoformat()
    start_date = window_cutoff(now)
    search_terms = watchlist_search_terms(conn)

    awards, status = [], {}
    for agency in sorted(QUALIFYING_TOPTIER_AGENCIES):
        if status:
            time.sleep(sleep)
        agency_awards, agency_status = fetch_agency_batched(
            agency, start_date, end_date, search_terms, sleep=sleep)
        awards.extend(agency_awards)
        status[agency] = agency_status
    return awards, status


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Measure distinct watchlist accounts with a qualifying "
                    "DOE/CISA/DHS award in the last 24 months, and sample "
                    "description richness. Read-only; writes nothing, mints "
                    "nothing, is not a fetcher.")
    parser.add_argument("--report", action="store_true", required=True,
                        help="the only mode: fetch, measure, print")
    parser.add_argument("--db", default=DEFAULT_DB_PATH,
                        help=f"store to read (default {DEFAULT_DB_PATH})")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")
    # Reviewer-caught: one ``now`` for the whole run. collect() and
    # report()/analyze() previously each defaulted to their own
    # datetime.now() call, so the cutoff a multi-minute live run actually
    # queried against could drift from the cutoff printed in the report
    # (only observable across a UTC midnight boundary, but free to fix).
    now = datetime.now(timezone.utc)
    conn = read_only_connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        awards, status = collect(conn, now=now)
    finally:
        conn.close()
    result = report(args.db, awards, source_status=status, now=now)
    # Reviewer-caught: a fully- or partially-failed source_status previously
    # still exited 0, indistinguishable from a clean run to any automated
    # caller checking $? instead of parsing the report text.
    return 0 if all(v.startswith("ok") for v in result["source_status"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
