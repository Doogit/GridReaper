"""NVD CVE API 2.0 fetcher (R5.3, R10.4) — store-only at MVP.

Pulls published CVEs from the NIST National Vulnerability Database CVE API 2.0
(https://services.nvd.nist.gov/rest/json/cves/2.0). Each record nests under
``vulnerabilities[].cve``; that inner object is stored as-is (R3.7: keep the
source's own record, don't pre-digest), keyed by ``cve.id`` with the event
date from ``cve.published``. No classification here — raw_events only.

Three NVD API 2.0 quirks are encoded here:

  * **120-day window cap.** NVD rejects any pubStartDate/pubEndDate span wider
    than 120 days, so a window is split into <=120-day sub-ranges, each paged
    independently. Dates are extended ISO-8601 with time (``...T00:00:00.000``).
    pubStart/pubEnd are inclusive, so a CVE exactly on a sub-range boundary is
    fetched by both neighbours; dedup-by-cve.id makes the duplicate 'seen'
    (records_new stays correct; records_seen may cosmetically over-count by 1).
  * **startIndex/resultsPerPage paging.** Page size is capped at 2000; each
    sub-range loops advancing startIndex by the number of records actually
    returned (not the advertised resultsPerPage — NVD can echo a wider page
    size than the array it sent) until startIndex reaches totalResults.
  * **keyed rolling rate window (R5.3).** With an ``NVD_API_KEY`` (read from
    env, never logged — R10.8) NVD allows ~50 requests / 30s; unauthenticated
    is ~5 / 30s. We sleep politely between requests either way; the key is
    optional and unauthenticated runs still work.

HTTP/parse failures propagate so the runner records status 'error' (R10.3).

  python -m app.ingest.nvd [--window-days N] [--limit N] [--force]
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

SOURCE_ID = "nvd"
PARSER_VERSION = "nvd/1.0"
USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"
API_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
DETAIL_URL = "https://nvd.nist.gov/vuln/detail/{cve_id}"

MAX_WINDOW_DAYS = 120       # NVD's hard cap on a pubStart/pubEnd span
PAGE_SIZE = 2000            # resultsPerPage max
# Polite delay between requests. Sized for the unauthenticated allowance
# (~5 req / 30s); a key raises NVD's ceiling but the delay stays safe.
SLEEP_S = 6.0
KEYED_SLEEP_S = 0.6


def _nvd_datetime(dt):
    """NVD's extended ISO-8601 with millis, no offset: 2026-01-01T00:00:00.000."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000")


def _get_json(url, retries=2):
    api_key = os.environ.get("NVD_API_KEY")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if api_key:
        headers["apiKey"] = api_key   # sent, never logged (R10.8)
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))


def _windows(start, end, max_days):
    """Split [start, end] into consecutive <=max_days sub-ranges."""
    span = timedelta(days=max_days)
    cur = start
    while cur < end:
        nxt = min(cur + span, end)
        yield cur, nxt
        cur = nxt


def fetch_events(conn, window_days, limit):
    """Yield one event per CVE published within the window.

    The window is split into <=120-day sub-ranges (NVD's cap); each is paged
    via startIndex/resultsPerPage. ``limit`` stops the walk across all pages
    and sub-ranges; ``limit <= 0`` yields nothing and makes no HTTP call.
    """
    if limit is not None and limit <= 0:
        return

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    window_start = now - timedelta(days=window_days)
    sleep_s = KEYED_SLEEP_S if os.environ.get("NVD_API_KEY") else SLEEP_S

    yielded = 0
    first_request = True
    for start, end in _windows(window_start, now, MAX_WINDOW_DAYS):
        start_index = 0
        while True:
            if not first_request:
                time.sleep(sleep_s)
            first_request = False
            params = urllib.parse.urlencode({
                "pubStartDate": _nvd_datetime(start),
                "pubEndDate": _nvd_datetime(end),
                "startIndex": start_index,
                "resultsPerPage": PAGE_SIZE,
            })
            data = _get_json(f"{API_URL}?{params}")
            records = data.get("vulnerabilities", [])
            for record in records:
                cve = record.get("cve") or {}
                published = (cve.get("published") or "").strip()
                event_date = published[:10]   # ISO date portion of published
                cve_id = cve.get("id", "")
                yield {
                    "source_native_id": cve_id,
                    "event_date": event_date,
                    "payload": json.dumps(cve, sort_keys=True),
                    "url": DETAIL_URL.format(cve_id=cve_id),
                }
                yielded += 1
                if limit is not None and yielded >= limit:
                    return

            total = data.get("totalResults", 0)
            # Advance by records ACTUALLY consumed, not the advertised
            # resultsPerPage: if NVD echoes a page size wider than the array it
            # returned (partial response under load), trusting the advertised
            # count would skip the gap of CVEs and still report 'success'.
            start_index += len(records)
            # Empty page while more remain: no progress possible, so stop this
            # sub-range rather than loop forever.
            if not records or start_index >= total:
                break


if __name__ == "__main__":
    sys.exit(runner.cli(
        SOURCE_ID, fetch_events, PARSER_VERSION,
        "Fetch NVD CVE API 2.0 published CVEs into raw_events"))
