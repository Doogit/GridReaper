"""CISA KEV catalog fetcher (R5.1, R10.4) — store-only at MVP.

One GET of the Known Exploited Vulnerabilities JSON feed; every record in
``vulnerabilities`` becomes one raw_event keyed by cveID, with the record
stored as-is (R3.7: keep the source's own record, don't pre-digest). The
initial backfill runs with a large ``--window-days`` to capture the full
catalog (~1300+ CVEs back to 2021); subsequent runs naturally dedupe to only
newly-added CVEs — delta behavior per R5.1/R10.4. No classification here.

  python -m app.ingest.cisa_kev [--window-days N] [--limit N] [--force]
"""
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from app.ingest import runner

SOURCE_ID = "cisa_kev"
PARSER_VERSION = "cisa_kev/1.0"
USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"
KEV_URL = ("https://www.cisa.gov/sites/default/files/feeds/"
           "known_exploited_vulnerabilities.json")
# the feed has no per-CVE URL; point at the human-readable catalog
CATALOG_URL = "https://www.cisa.gov/known-exploited-vulnerabilities-catalog"


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


def fetch_events(conn, window_days, limit):
    """Yield one event per KEV record with dateAdded inside the window.

    Records missing dateAdded are kept (never drop catalog data silently).
    HTTP failures propagate; the runner records status 'error' (R10.3).
    """
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=window_days)).date().isoformat()
    data = _get_json(KEV_URL)
    yielded = 0
    for vuln in data.get("vulnerabilities", []):
        date_added = (vuln.get("dateAdded") or "").strip()
        if date_added and date_added < cutoff:   # YYYY-MM-DD sorts lexically
            continue
        yield {
            "source_native_id": vuln.get("cveID", ""),
            "event_date": date_added,
            "payload": json.dumps(vuln),
            "url": CATALOG_URL,
        }
        yielded += 1
        if limit and yielded >= limit:
            return


if __name__ == "__main__":
    sys.exit(runner.cli(SOURCE_ID, fetch_events, PARSER_VERSION,
                        "Fetch the CISA KEV catalog into raw_events"))
