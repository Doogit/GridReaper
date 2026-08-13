"""GDELT 2.0 DOC API fetcher (R5.x, R10.4) — store-only at MVP.

One GET of the GDELT DOC API in ArtList/JSON mode for a single energy/utility
sector query; every article in ``articles`` becomes one raw_event keyed by its
``url``, with the article dict stored as-is (R3.7: keep the source's own
record, don't pre-digest). No classification here.

GDELT quirks encoded here (verified against the DOC 2.0 API docs):
  - ``seendate`` is a compact ``YYYYMMDDTHHMMSSZ`` timestamp (e.g.
    ``20260812T120000Z``), not ISO-8601; ``_seendate_to_iso`` parses it (and
    the bare ``YYYYMMDDHHMMSS`` variant) to a UTC ISO date. Missing/malformed
    seendate keeps the record with event_date '' — never silently dropped.
  - a no-results response can be ``{}`` or ``{"articles": []}``; ``.get`` on
    both yields nothing without error.
  - ``maxrecords`` caps at 250 (the API's documented ceiling).
  - the DOC API covers only a rolling ~3-month window, so ``window_days`` is
    used to derive the request timespan (capped) *and* to filter returned
    articles by seendate; a large ``--window-days`` cannot reach older news.
  - payloads are non-ASCII-heavy (foreign-language titles/domains); stored as
    UTF-8 as-is — no encoding "fix" (console mojibake is display-only).

  python -m app.ingest.gdelt [--window-days N] [--limit N] [--force]
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from app.ingest import runner

SOURCE_ID = "gdelt"
PARSER_VERSION = "gdelt/1.0"
USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"

API_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_RECORDS = 250                 # DOC API documented ceiling
MAX_TIMESPAN_DAYS = 90            # rolling ~3-month coverage window

# single default energy/utility sector query (keyword-based). GDELT ORs bare
# terms; quoted phrases are matched verbatim. sourcecountry:US narrows to US
# coverage of the domestic energy sector.
QUERY = ('("electric utility" OR "power grid" OR "power company" OR '
         '"electric grid" OR "nuclear plant" OR "energy company") '
         'sourcecountry:US')


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


def _seendate_to_iso(seendate):
    """Parse GDELT's compact seendate to a UTC ISO date, or '' if unparseable.

    Accepts ``YYYYMMDDTHHMMSSZ`` (the documented form) and the bare
    ``YYYYMMDDHHMMSS`` variant. Malformed values return '' (record kept)."""
    s = (seendate or "").strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _build_url(window_days):
    timespan_days = max(1, min(window_days, MAX_TIMESPAN_DAYS))
    params = urllib.parse.urlencode({
        "query": QUERY,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": MAX_RECORDS,
        "timespan": f"{timespan_days}d",
    })
    return f"{API_BASE}?{params}"


def fetch_events(conn, window_days, limit):
    """Yield one event per GDELT article with seendate inside the window.

    Articles missing/malformed seendate are kept (event_date ''); they cannot
    be window-filtered, so they always pass. HTTP failures propagate; the
    runner records status 'error' (R10.3).
    """
    if limit is not None and limit <= 0:
        return
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=window_days)).date().isoformat()
    data = _get_json(_build_url(window_days))
    yielded = 0
    for article in data.get("articles", []):
        event_date = _seendate_to_iso(article.get("seendate"))
        # YYYY-MM-DD sorts lexically; keep records we can't date
        if event_date and event_date < cutoff:
            continue
        yield {
            "source_native_id": article.get("url", ""),
            "event_date": event_date,
            "payload": json.dumps(article, sort_keys=True),
            "url": article.get("url", ""),
        }
        yielded += 1
        if limit is not None and yielded >= limit:
            return


if __name__ == "__main__":
    sys.exit(runner.cli(SOURCE_ID, fetch_events, PARSER_VERSION,
                        "Fetch GDELT DOC API energy-sector articles "
                        "into raw_events"))
