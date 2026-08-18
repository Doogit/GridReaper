"""EPA ECHO enforcement case fetcher: judicial civil consent-decree records
for the oil & gas watchlist subsectors (R7, wires the unwired
``audit_consent_decree`` trigger).

Polls echodata.epa.gov's Enforcement Case Search REST service
(``case_rest_services``), filtered by SIC/NAICS codes covering all 68 oil &
gas watchlist entities (og_ep 27 + og_major 2 + midstream 16 + ofs 12 +
refiner 8 + lng 3 = 68, per seeds/watchlist_entities.csv subsector counts).
Filtering by industry code reaches every oil & gas entity uniformly in one
query per code system -- no per-entity fetch loop, unlike app/ingest/edgar.py
-- which is exactly why this source skips the probe gate the plan's other
units need (its coverage does not depend on any entity-name measurement).

Base host is ``echodata.epa.gov``, not ``echo.epa.gov`` (the HTML UI host
documented at echo.epa.gov/tools/web-services) -- confirmed live 2026-08-18
against the working REST API and against the ``echor`` R package
(github.com/mps9506/echor), the one third-party client this session found
that actually calls the live host rather than a static mirror. See
seeds/source_policies.csv for the tos_status this distinction produced.

Fetch-time filter: ``p_case_category=JDC`` (Judicial Civil). This is a real
distinction ICIS-FE&C's own data model enforces, not content pre-digestion:
AFR (Administrative - Formal) cases are penalties/orders EPA settles under
its own authority and are never consent decrees; JDC cases are referred to
DOJ and filed in federal court, which -- per EPA's own "Proposed Consent
Decrees and Draft Settlement Agreements" practice -- is how a consent decree
comes to exist. Filtering to JDC here mirrors KEEP_FORMS in
app/ingest/edgar.py: a source-level "which record TYPE even matters" gate.
R3.7 payload preservation still holds -- every field ECHO returns for a kept
case is stored in the payload as-is; app/classify/environmental_enforcement.py
does the further narrowing (civil vs criminal, actually-settled vs still
litigating) that JDC alone cannot express.

Two fetches per run (one by NAICS code list, one by SIC code list -- ECHO has
no combined OR filter across code systems); a case matching both is deduped
automatically by the runner via CaseNumber as source_native_id. Live-verified
2026-08-18: national JDC oil&gas volume is small (a few hundred to ~1,000
cases across the full ICIS-FE&C history for these codes), so both fetches
complete in one or a handful of paginated pages; get_qid's page size follows
the ``responseset`` passed to the originating get_cases call (confirmed live:
a page size set at get_cases time is honored by every subsequent get_qid
page, and get_qid ignores an unrelated ``rows`` parameter).

get_qid's "Message" field: EPA's own "ECHO All Data Search Results Services"
service documentation (the sibling echo_rest_services family this API
mirrors) states get_qid's Message is either a specific error string or the
literal word "Working" -- that is get_qid's normal COMPLETE-response value,
not an async "still building, poll again" job-status flag (get_cases/
get_facilities use "Success" for that same complete-response meaning; get_qid
just uses a different word). Confirmed against the live case_rest_services
API 2026-08-18: a "Working" response already carries the full requested page
of Cases, so _results() below treats it as terminal, not a retry signal.

R10.6 (field allowlist / no unvetted PII): ICIS-FE&C's case-search response
schema (case_rest_services.metadata, inspected live 2026-08-18) carries no
attorney/officer/signatory field at all -- its fields are case- and
defendant-entity-level (CaseNumber, CaseName, CaseCategoryCode, PrimaryLaw,
dates, penalty amounts, ...), never an individual person. The classifier
(app/classify/environmental_enforcement.py) additionally only ever quotes a
fixed, named subset of those fields into headline/evidence text, so no
column this fetcher stores could reach a card even if that determination
changes later.

echodata.epa.gov robots.txt (the host this fetcher actually calls, fetched
2026-08-18): a two-line stub, "User-agent: *" / "Disallow: *", preceded only
by the standard generic robots.txt-explainer comment template (no site- or
API-specific business language) -- unlike echo.epa.gov's (the docs/UI host)
robots.txt, which is curated to specific HTML report paths. Treated as a
boilerplate blanket stub rather than a considered policy against REST
consumption, because echo.epa.gov/tools/web-services -- EPA's own published
documentation, hosted on the sister domain -- names and invites exactly this
REST usage ("organizations within and outside EPA to build interactive
applications with EPA's enforcement and compliance data"). Recorded verbatim
in seeds/source_policies.csv's tos_status/rate_limit fields for operator
review, not silently resolved.

  python -m app.ingest.epa_echo [--window-days N] [--limit N] [--force]
"""
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from app.ingest import runner

SOURCE_ID = "epa_echo"
PARSER_VERSION = "epa_echo/1.0"
USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"

BASE = "https://echodata.epa.gov/echo/case_rest_services"
CASES_URL = BASE + ".get_cases"
QID_URL = BASE + ".get_qid"

# Oil & gas SIC/NAICS codes covering every og_ep/og_major/midstream/ofs/
# refiner/lng watchlist entity: crude+gas extraction, drilling, oilfield
# support services, petroleum refining, crude/gas pipeline transportation.
NAICS_CODES = ["211120", "211130", "213111", "213112", "324110", "486110",
               "486210"]
SIC_CODES = ["1311", "1381", "1389", "2911", "4612", "4922"]

JUDICIAL_CIVIL_CATEGORY = "JDC"
RESPONSESET = 1000        # ECHO's documented max facility/case page size
# Pagination runaway guard. Live-verified national JDC oil&gas volume is a
# few hundred to ~1,000 rows across both queries (well under one page), so 5
# pages (5,000 rows) is generous headroom, not a tight fit -- kept small so a
# retry-storm against a degraded EPA endpoint (each of up to
# 2 queries x (1 + MAX_PAGES) calls x up to 3 attempts x 60s timeout) stays a
# small fraction of deploy/scheduled_run.sh's whole-pipeline TIMEOUT_MINUTES
# budget, rather than one degraded source risking every later pipeline step.
MAX_PAGES = 5
PAGE_SLEEP_S = 1.0        # politeness between paginated calls
QUERY_SLEEP_S = 1.0       # politeness between the NAICS and SIC queries


def _get_json(url, retries=2):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))


def _iso_date(text):
    """ECHO renders DateFiled/SettlementDate as US M/D/YYYY; "" when absent
    or unparseable. An unparseable date is treated as no anchor rather than
    guessed at, matching app.classify.regulatory's durable-clock convention
    and app.spikes.breach_registry_probe's date-parsing discipline."""
    t = (text or "").strip()
    parts = t.split("/")
    if len(parts) != 3:
        return ""
    month, day, year = parts
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return ""


def _results(data):
    """The 'Results' object, or raise with the source's own error text --
    an API-level error must propagate (R10.3 records the run as 'error'),
    never read as a quiet empty result."""
    results = (data or {}).get("Results") or {}
    error = results.get("Error")
    if error:
        msg = error.get("ErrorMessage") if isinstance(error, dict) else error
        raise ValueError(f"EPA ECHO error: {msg}")
    message = results.get("Message") or ""
    if message not in ("Success", "Working"):
        raise ValueError(f"EPA ECHO unexpected response: {message!r}")
    return results


def _fetch_cases(code_param, codes):
    """Yield every case record for one code-system query (NAICS or SIC),
    paginating via get_qid. codes is a list of SIC or NAICS code strings."""
    params = [("output", "JSON"), (code_param, ",".join(codes)),
              ("p_case_category", JUDICIAL_CIVIL_CATEGORY),
              ("responseset", str(RESPONSESET))]
    initial = _results(_get_json(CASES_URL + "?" + urllib.parse.urlencode(params)))
    qid = initial.get("QueryID")
    total_rows = int(initial.get("QueryRows") or 0)
    if not qid or total_rows <= 0:
        return

    wanted_pages = max(1, math.ceil(total_rows / RESPONSESET))
    pages = min(MAX_PAGES, wanted_pages)
    last_page_size = 0
    for pageno in range(1, pages + 1):
        if pageno > 1:
            time.sleep(PAGE_SLEEP_S)
        page_params = [("output", "JSON"), ("qid", str(qid)),
                       ("pageno", str(pageno))]
        page = _results(
            _get_json(QID_URL + "?" + urllib.parse.urlencode(page_params)))
        cases = page.get("Cases") or []
        last_page_size = len(cases)
        if not cases:
            return
        yield from cases
    # A full last page at the MAX_PAGES cap means more rows exist than this
    # run fetched: silently returning "success" would under-report forever
    # (R4.1 -- an incomplete result read as complete is its own kind of
    # fabrication). Raise so R10.3 records the run as 'error' instead.
    if wanted_pages > MAX_PAGES and last_page_size >= RESPONSESET:
        raise ValueError(
            f"EPA ECHO pagination truncated: {total_rows} rows reported but "
            f"only {pages} of {wanted_pages} pages fetched (MAX_PAGES="
            f"{MAX_PAGES}); raise MAX_PAGES or narrow the query")


def fetch_events(conn, window_days, limit):
    """Yield one raw event per genuinely-new-to-window EPA ECHO judicial
    civil case for the oil & gas SIC/NAICS codes above. HTTP/API failures
    propagate; the runner records them as status 'error' (R10.3)."""
    if limit is not None and limit <= 0:
        return
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=window_days)).date().isoformat()
    yielded = 0
    queries = [("p_naics", NAICS_CODES), ("p_sic", SIC_CODES)]
    for i, (code_param, codes) in enumerate(queries):
        if i:
            time.sleep(QUERY_SLEEP_S)
        for case in _fetch_cases(code_param, codes):
            if not isinstance(case, dict):
                continue
            case_number = (case.get("CaseNumber") or "").strip()
            if not case_number:
                continue
            event_date = (_iso_date(case.get("SettlementDate"))
                          or _iso_date(case.get("DateFiled")))
            # No parseable date is treated as out-of-window, not "always
            # keep": matches app.ingest.edgar's convention (an empty/missing
            # filing_date there is lexicographically < any real cutoff and so
            # is always skipped) rather than silently ignoring --window-days
            # for exactly the records whose age is unknown.
            if not event_date or event_date < cutoff:
                continue
            yield {
                "source_native_id": case_number,
                "event_date": event_date,
                "payload": json.dumps(case, sort_keys=True),
            }
            yielded += 1
            if limit is not None and yielded >= limit:
                return


if __name__ == "__main__":
    sys.exit(runner.cli(
        SOURCE_ID, fetch_events, PARSER_VERSION,
        "Fetch EPA ECHO judicial civil enforcement cases for oil & gas "
        "watchlist subsectors"))
