"""EIA API v2 plant/facility fetcher (R5.4, R10.4) — store-only at MVP.

Paginated typed parsing (R5.4) of EIA's v2 electricity
``operating-generator-capacity`` route: a monthly, plant/generator-level
dataset carrying plantid + plantName + stateid + latitude/longitude +
nameplate/summer/winter capacity. Every record becomes one raw_event keyed
by ``plantid:generatorid:period`` with the full record stored as-is (R3.7:
keep the source's own record, don't pre-digest). ``plantid`` alone is NOT
unique on this route — one plant has many rows (per generator, per monthly
period), so a plantid-only key would collapse distinct records into one and
silently discard most of the dataset. When any id component is missing on a
row, we omit the native id so the runner falls back to its content-hash key
(never let two distinct records collide). This chunk is store-only into
``raw_events``; the typed ``facility_assets`` projection is deferred to a
later chunk.

EIA v2 wraps results in ``{"response": {"total": N, "data": [ ... ]}}`` and
pages via ``offset``/``length`` (length max 5000); we loop until offset >=
response.total. The API key is read from ``EIA_API_KEY`` (env only, R10.8);
it is never logged and never stored in the payload or url. No classification
here.

  EIA_API_KEY=... python -m app.ingest.eia [--window-days N] [--limit N] [--force]
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from app.ingest import runner

SOURCE_ID = "eia"
PARSER_VERSION = "eia/1.0"
USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"
API_KEY_ENV = "EIA_API_KEY"
BASE_URL = ("https://api.eia.gov/v2/electricity/"
            "operating-generator-capacity/data/")
# human-readable browser view for the route (no per-plant deep link exists)
BROWSER_URL = ("https://www.eia.gov/opendata/browser/electricity/"
               "operating-generator-capacity")
PAGE_LENGTH = 5000          # EIA v2 JSON max rows per request


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


def _api_key():
    """The EIA key from env only (R10.8). Absent -> clear error (never hit
    the network keyless); the runner records the run as 'error' (R10.3)."""
    key = (os.environ.get(API_KEY_ENV) or "").strip()
    if not key:
        raise RuntimeError(
            f"{API_KEY_ENV} is not set. Export the EIA API key before "
            f"running the eia fetcher (secrets live outside the repo, R10.8).")
    return key


def _page_url(api_key, offset):
    """Build one page request. The key rides in the query string EIA
    requires but is never stored — the stored ``url`` is BROWSER_URL."""
    params = [
        ("api_key", api_key),
        ("frequency", "monthly"),
        ("data[]", "nameplate-capacity-mw"),
        ("data[]", "net-summer-capacity-mw"),
        ("data[]", "net-winter-capacity-mw"),
        ("data[]", "latitude"),
        ("data[]", "longitude"),
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "desc"),
        ("offset", str(offset)),
        ("length", str(PAGE_LENGTH)),
    ]
    return BASE_URL + "?" + urllib.parse.urlencode(params)


def _native_id(record):
    """Unique per source record: plantid:generatorid:period. plantid alone is
    not unique on this monthly, generator-level route. If any component is
    missing, return '' so the runner falls back to its content-hash key rather
    than colliding two distinct records."""
    plantid = str(record.get("plantid") or "").strip()
    generatorid = str(record.get("generatorid") or "").strip()
    period = str(record.get("period") or "").strip()
    if not (plantid and generatorid and period):
        return ""
    return f"{plantid}:{generatorid}:{period}"


def _event_date(period):
    """EIA periods are annual ("2026") or monthly ("2026-03"); normalize to a
    UTC ISO date. Missing/odd periods yield '' — never silently drop a record."""
    period = (period or "").strip()
    if not period:
        return ""
    parts = period.split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        return datetime(year, month, day, tzinfo=timezone.utc).date().isoformat()
    except (ValueError, IndexError):
        return ""


def fetch_events(conn, window_days, limit):
    """Yield one event per generator record whose period is inside the window.

    Records missing/unparseable period are kept with event_date '' (never
    drop facility data silently). Pages through offset/length until
    offset >= response.total. HTTP/parse failures propagate; the runner
    records status 'error' (R10.3).
    """
    if limit is not None and limit <= 0:
        return
    api_key = _api_key()
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=window_days)).date().isoformat()
    yielded = 0
    offset = 0
    while True:
        envelope = _get_json(_page_url(api_key, offset))
        response = envelope.get("response") or {}
        rows = response.get("data") or []
        total = response.get("total")
        try:
            total = int(total)
        except (TypeError, ValueError):
            total = None
        for record in rows:
            event_date = _event_date(record.get("period"))
            # YYYY-MM-DD sorts lexically; skip only when we have a date to compare
            if event_date and event_date < cutoff:
                continue
            yield {
                "source_native_id": _native_id(record),
                "event_date": event_date,
                "payload": json.dumps(record, sort_keys=True),
                "url": BROWSER_URL,
            }
            yielded += 1
            if limit is not None and yielded >= limit:
                return
        offset += len(rows)
        if not rows or total is None or offset >= total:
            return


if __name__ == "__main__":
    sys.exit(runner.cli(
        SOURCE_ID, fetch_events, PARSER_VERSION,
        "Fetch EIA operating-generator-capacity records into raw_events"))
