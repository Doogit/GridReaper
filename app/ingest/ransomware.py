"""ransomware.live recent-victims fetcher (R5.4, R10.4) — store-only, not classified.

One GET of the ransomware.live v2 recent-victims API; every victim object in
the returned JSON array becomes one raw_event, stored as-is (R3.7: keep the
source's own record, don't pre-digest). This is an unconfirmed early-warning
feed (evidence_rank 3): raw_events only, NO classification, NO cards here.

Data credit: victim data from ransomware.live (https://www.ransomware.live),
licensed CC-BY-4.0 (R10.4 attribution).

HISTORY IS AVAILABLE, and this endpoint is a CHOICE, not a limit. Spiked with
read-only GETs on 2026-08-16: /v2/recentvictims returns a fixed 100-record slice
of the newest listings (5 days of coverage), while GET /v2/victims/{year}/{month}
returns a whole month — ~914 records for 2026/07, same record shape. A
month-walking backfill is therefore possible and is what would turn the Explore
panel's baseline delta into a real number; it is not run from here because
backfill scope is the operator's call.

Two facts a backfill must plan around, both observed on that 2026/07 response:

  * NEITHER date field is confined to the requested month, so the month
    parameter cannot be trusted as a date filter. 'attackdate' ran
    2024-05-20 → 2026-07-31 (victims disclosed in July whose attack was years
    earlier) and 'discovered' ran 2026-07-01 → 2026-08-16 (spilling past the
    month end). Which field the API buckets on was NOT established — one
    response cannot settle it — so a backfill must bucket by whichever field it
    stores as event_date itself rather than assume the API did.
  * The API rate-limits harder than the recent endpoint suggests: a 4th GET
    within roughly a minute returned HTTP 429 TOO MANY REQUESTS. A 12-month
    walk needs a politeness sleep between months and must treat 429 as retry,
    not as end-of-data.

The API returns no stable per-victim id, so events omit source_native_id and
the runner keys them by sha256 of the payload; json.dumps(sort_keys=True)
makes that payload deterministic so unchanged records dedupe to 'seen'.

  python -m app.ingest.ransomware [--window-days N] [--limit N] [--force]
"""
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from app.ingest import runner

SOURCE_ID = "ransomware_live"
PARSER_VERSION = "ransomware_live/1.0"
USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"
API_URL = "https://api.ransomware.live/v2/recentvictims"


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


def _event_date(record):
    """ISO date the victim was posted: prefer 'discovered', fall back to
    'attackdate'. Both are ISO-8601 datetimes; keep the date portion."""
    raw = (record.get("discovered") or record.get("attackdate") or "").strip()
    return raw[:10]


def fetch_events(conn, window_days, limit):
    """Yield one event per victim discovered within the window.

    Records with no usable date are kept (never drop feed data silently).
    The API gives no stable id, so events omit source_native_id and rely on
    the runner's content-hash keying (R10.4); the payload is emitted with
    sort_keys=True so it hashes stably. HTTP failures propagate; the runner
    records status 'error' (R10.3).
    """
    if limit is not None and limit <= 0:
        return
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=window_days)).date().isoformat()
    data = _get_json(API_URL)
    yielded = 0
    for record in data:
        event_date = _event_date(record)
        if event_date and event_date < cutoff:   # YYYY-MM-DD sorts lexically
            continue
        yield {
            "event_date": event_date,
            "payload": json.dumps(record, sort_keys=True),
            "url": (record.get("url") or "").strip(),
        }
        yielded += 1
        if limit is not None and yielded >= limit:
            return


if __name__ == "__main__":
    sys.exit(runner.cli(
        SOURCE_ID, fetch_events, PARSER_VERSION,
        "Fetch ransomware.live recent victims into raw_events"))
