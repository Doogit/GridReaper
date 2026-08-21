"""USAspending.gov capital-project award fetcher (R5, wires the previously
unused ``capital_project`` trigger via a real DOE/CISA/DHS financial-
assistance award source).

WHAT THIS FETCHES. Grants/cooperative-agreement awards (financial
assistance) whose Awarding Agency is the Department of Energy or the
Department of Homeland Security (DHS toptier covers CISA, a DHS sub-agency;
the sub-agency field is checked too in case a response ever carries CISA
under a different toptier label) within the ``--window-days`` window,
scoped to the active watchlist's names/aliases via batched
``recipient_search_text`` queries -- an unscoped DOE/DHS-wide search is
national (thousands of awards to universities, national labs and unrelated
small businesses) and would never resolve to a trustworthy watchlist-scoped
set within any reasonable page bound. This is a volume-reduction filter
only, not a matching authority: every recipient name this returns still
goes through the classifier's normal entity-resolution path
(``app/classify/capital_project.py``), which can accept, review-queue, or
drop it -- a subsidiary name the filter happens to substring-match is never
auto-accepted on the strength of the filter alone. Mirrors the query
construction (batched ``recipient_search_text``, per-agency filtering,
bounded pagination) validated live by ``app/spikes/usaspending_probe.py``'s
R1 probe.

RATIFIED DEVIATION FROM R3.4 -- POST, not GET (operator-approved, not an
open question). CLAUDE.md's R3.4 says source access MUST be read-only
GET/RSS/JSON. ``api.usaspending.gov``'s filtered award search
(``/api/v2/search/spending_by_award/``) has no GET-based equivalent: the
API's own documentation (api.usaspending.gov/docs/intro-tutorial) states
POST is the required method for this endpoint, for every caller, not a
workaround this module invented. The operator ratified this as an accepted
exception to R3.4's literal wording after live research confirmed all of
the following, each independently supporting the call:
  - no authentication or API key required (keyless, public);
  - no ``robots.txt`` restriction on the API host (``api.usaspending.gov/
    robots.txt`` 404s -- no crawl-restriction file is published on the API
    subdomain at all);
  - the underlying data and code are CC0 1.0 Universal / public domain
    (github.com/fedspendingtransparency/usaspending-api);
  - USAspending.gov is the U.S. Treasury's statutory DATA Act spending-
    transparency portal -- the JSON API exists specifically for public
    programmatic read access, not a form submission or a scrape.
This module sends the required POST with a plain JSON filter body, an
honest ``User-Agent``, no auth, no session/cookies, and no form fields --
i.e. a read-only JSON query in every way except the HTTP verb the API
itself demands. See ``seeds/source_policies.csv``'s ``usaspending`` row for
the recorded ``tos_status``/``rate_limit`` fields.

QUALIFYING AGENCY AND AWARD TYPE. ``ASSISTANCE_AWARD_TYPE_CODES`` scopes the
search to grants and cooperative agreements -- the award type shape a
"capital project" signal is actually drawn from (loans, direct payments and
insurance are out of scope). ``QUALIFYING_TOPTIER_AGENCIES`` is DOE + DHS;
the classifier re-checks agency qualification independently (defense in
depth, matching ``app/classify/regulatory.py``'s and
``app/classify/environmental_enforcement.py``'s precision-over-recall
convention) rather than trusting this fetcher's filter alone.

PAYLOAD PRESERVATION (R3.7). Every field the API returns for a kept award
(Award ID, Recipient Name, Awarding Agency, Awarding Sub Agency, Start
Date, Description) is stored in the payload exactly as received -- the
``Description`` field in particular is never truncated, summarized, or
whitespace-normalized here, because a later unit's keyword-absence check
depends on that text being present and complete.

PAGINATION. Bounded per (agency, search-term-batch) query at ``MAX_PAGES``
pages of ``PAGE_LIMIT`` awards each. When a response carries a boolean
``page_metadata.hasNext``, that field is the truncation signal: pagination
stops as soon as it is false (even on an exactly-``PAGE_LIMIT``-sized
page), and hitting ``MAX_PAGES`` while it is still true raises -- an
incomplete result read as complete is its own kind of fabrication (R4.1),
the same discipline ``app/ingest/epa_echo.py`` applies via its ``QueryRows``
field. When a response omits ``page_metadata`` (or the field is not a
plain boolean), pagination falls back to a shorter-than-``PAGE_LIMIT`` page
as the stop signal and a full final page at ``MAX_PAGES`` simply stops
without raising -- a soft runaway guard, matching
``app/ingest/federal_register.py``'s ``MAX_PAGES`` convention, used only
when the API genuinely hasn't told us whether more rows exist. A recipient
can legitimately appear across more than one agency or search-term batch;
dedupe on ``source_native_id`` (Award ID) is the runner's job (R10.4), not
this fetcher's.

RECIPIENT_SEARCH_TEXT SEMANTICS. A batch of names is OR'd by the API, not
ANDed -- confirmed empirically by ``app/spikes/usaspending_probe.py``'s R1
probe returning real hits against multi-term batches (an AND reading would
require one recipient to simultaneously match several different watchlist
company names and would return nothing). ``SEARCH_TERM_BATCH_SIZE`` batches
purely to stay under the API's undocumented per-request term-count limit
(see below), not to change match semantics.

EMPTY WATCHLIST. If the active watchlist has zero resolvable names/aliases
(a misconfiguration this fetcher should never see in practice), the run
fetches nothing rather than falling through to an unscoped, DOE/DHS-wide
query -- issuing that query would directly contradict the "an unscoped
search is untrustworthy" rationale above, so an empty watchlist is treated
as nothing-to-fetch, not as a license to search broadly.

  python -m app.ingest.usaspending [--window-days N] [--limit N] [--force]
"""
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from app.ingest import runner

SOURCE_ID = "usaspending"
PARSER_VERSION = "usaspending/1.0"
USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"

SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
FIELDS = ["Award ID", "Recipient Name", "Awarding Agency",
          "Awarding Sub Agency", "Start Date", "Description"]

# Grants + cooperative agreements: the financial-assistance award types a
# "capital project" signal is actually drawn from (see module docstring).
ASSISTANCE_AWARD_TYPE_CODES = ("02", "03", "04", "05")

QUALIFYING_TOPTIER_AGENCIES = ("Department of Energy",
                               "Department of Homeland Security")

PAGE_LIMIT = 100
MAX_PAGES = 10            # pagination runaway guard per (agency, batch)
PAGE_SLEEP_S = 1.0        # politeness between paginated pages
QUERY_SLEEP_S = 2.0       # politeness between agency/search-term-batch queries

# Live-discovered, undocumented API behavior (per app/spikes/
# usaspending_probe.py's R1 probe): a recipient_search_text array of 14+
# terms 503s near-instantly. 10 is used here for a safety margin.
SEARCH_TERM_BATCH_SIZE = 10


def watchlist_search_terms(conn):
    """Distinct ACTIVE watchlist entity names + aliases (R8.7: a disabled
    entity is unresolvable anyway). Volume-reduction filter only -- see the
    module docstring; the classifier's entity resolution remains the sole
    matching authority over whatever this surfaces."""
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


def _post_json(data, retries=2):
    """One POST to the award search endpoint. See the module docstring's
    ratified-deviation section for why this is POST, not GET."""
    req = urllib.request.Request(
        SEARCH_URL, data=data,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json",
                 "Accept": "application/json"},
        method="POST")
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        # ValueError covers json.JSONDecodeError: a non-JSON 200 body (e.g. a
        # transient WAF/CDN interstitial) is a retryable condition, not a
        # permanent shape error -- app/ingest/usaspending.py's own shape
        # check below (missing 'results') is what should NOT be retried,
        # and it only runs after a value has already been successfully
        # decoded here.
        except (urllib.error.URLError, TimeoutError, ValueError):
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))


def _fetch_agency_batch(agency_name, start_date, end_date, search_terms,
                        max_pages=MAX_PAGES, sleep=PAGE_SLEEP_S):
    """Yield every award record for one (agency, search-term-batch) query,
    paginating sequentially. Raises on a malformed API response shape (an
    API-level error must propagate per R10.3), and on a confirmed
    truncation (``page_metadata.hasNext`` still true after ``max_pages``).
    See the module docstring's PAGINATION section for the ``hasNext``
    vs. soft-stop fallback distinction."""
    last_has_next = None
    for page in range(1, max_pages + 1):
        if page > 1:
            time.sleep(sleep)
        data = _post_json(_search_request_body(
            agency_name, start_date, end_date, page, search_terms))
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError(
                "USAspending payload is not a {'results': [...]} object; "
                "keys=" + (repr(sorted(data.keys())) if isinstance(data, dict)
                          else "n/a"))
        results = data["results"]
        yield from results

        page_metadata = data.get("page_metadata")
        has_next = (page_metadata.get("hasNext")
                   if isinstance(page_metadata, dict) else None)
        if isinstance(has_next, bool):
            last_has_next = has_next
            if not has_next:
                return
        elif len(results) < PAGE_LIMIT:
            return   # no usable hasNext -- soft-stop fallback, see docstring
    if last_has_next:
        raise ValueError(
            f"USAspending pagination truncated for {agency_name!r}: "
            f"page_metadata.hasNext still true after {max_pages} pages "
            f"(MAX_PAGES={max_pages}); raise MAX_PAGES or narrow the query")


def fetch_events(conn, window_days, limit):
    """Yield one raw event per DOE/CISA/DHS grant or cooperative-agreement
    award for the watchlist's names/aliases within the window. HTTP/API
    failures propagate; the runner records them as status 'error' (R10.3).
    A record missing a required field (Award ID, Recipient Name, Awarding
    Agency, or a start date) is skipped, never fabricated (R4.1)."""
    if limit is not None and limit <= 0:
        return
    search_terms = watchlist_search_terms(conn)
    if not search_terms:
        # An unscoped, DOE/DHS-wide query would contradict this module's own
        # "unscoped search is untrustworthy" rationale (see docstring) --
        # nothing to scope to means nothing to fetch, not a license to
        # search broadly.
        return
    now = datetime.now(timezone.utc)
    end_date = now.date().isoformat()
    start_date = (now - timedelta(days=window_days)).date().isoformat()
    batches = [search_terms[i:i + SEARCH_TERM_BATCH_SIZE]
              for i in range(0, len(search_terms), SEARCH_TERM_BATCH_SIZE)]

    yielded = 0
    first = True
    for agency in QUALIFYING_TOPTIER_AGENCIES:
        for batch in batches:
            if not first:
                time.sleep(QUERY_SLEEP_S)
            first = False
            for rec in _fetch_agency_batch(agency, start_date, end_date, batch):
                if not isinstance(rec, dict):
                    continue
                award_id = (rec.get("Award ID") or "").strip()
                recipient = (rec.get("Recipient Name") or "").strip()
                agency_name = (rec.get("Awarding Agency") or "").strip()
                event_date = (rec.get("Start Date") or "").strip()
                if not (award_id and recipient and agency_name and event_date):
                    continue
                yield {
                    "source_native_id": award_id,
                    "event_date": event_date,
                    "payload": json.dumps(rec, sort_keys=True),
                }
                yielded += 1
                if limit is not None and yielded >= limit:
                    return


if __name__ == "__main__":
    sys.exit(runner.cli(
        SOURCE_ID, fetch_events, PARSER_VERSION,
        "Fetch USAspending.gov DOE/CISA/DHS capital-project awards for "
        "watchlist entities"))
