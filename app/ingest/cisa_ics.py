"""CISA ICS advisories RSS fetcher (R5.3, R9.6, R10.4) — store-only.

One GET of the ICS Advisories RSS feed; every ``<item>`` becomes one
raw_event keyed by its advisory id (the last path segment of its link, e.g.
"icsa-26-225-05"), with the item's own fields (title, link, guid, pubDate,
description) stored as-is (R3.7: keep the source's own record, don't
pre-digest). The description carries the advisory body CISA's own way —
vendor, affected products, CVEs, and the "Critical Infrastructure Sectors"
list a later classifier reads for an energy-sector match
(app/classify/cisa_ics.py) — as escaped HTML, unparsed here.

The feed is a shallow rolling window (recent ~30 advisories, no archive),
mirroring app/ingest/security_rss.py; --window-days only filters what the
feed currently serves. No classification here.

  python -m app.ingest.cisa_ics [--window-days N] [--limit N] [--force]
"""
import json
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from app.ingest import runner

SOURCE_ID = "cisa_ics"
PARSER_VERSION = "cisa_ics/1.0"
USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"
ICS_ADVISORIES_URL = ("https://www.cisa.gov/cybersecurity-advisories/"
                       "ics-advisories.xml")


def _get_text(url, retries=2):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/xml, text/xml"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))


def _parse_pubdate(raw):
    """RFC-822 pubDate (CISA's feed uses a 2-digit year, e.g. "Thu, 13 Aug
    26 12:00:00 +0000") -> aware UTC datetime, or None when unparseable."""
    if not (raw or "").strip():
        return None
    try:
        dt = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _advisory_id(link, guid):
    """The advisory slug from the link's PATH (e.g. "icsa-26-225-05"), so a
    query string or fragment CISA's feed might one day add (tracking params,
    an anchor) can't mint a second raw_event for the same advisory (R10.4).
    Falls back to the item guid when the link has no usable path (missing,
    or a bare domain)."""
    path = urlsplit((link or "").strip()).path.rstrip("/")
    if path:
        return path.rsplit("/", 1)[-1]
    return (guid or "").strip()


def fetch_events(conn, window_days, limit):
    """Yield one event per ICS advisory <item> with pubDate inside the
    window. Items with an unparseable/absent pubDate are kept (never drop
    catalog data silently) with event_date=''. HTTP failures propagate; the
    runner records status 'error' (R10.3)."""
    if limit is not None and limit <= 0:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    root = ET.fromstring(_get_text(ICS_ADVISORIES_URL))
    yielded = 0
    for item in root.iter("item"):
        title = item.findtext("title", default="") or ""
        link = (item.findtext("link", default="") or "").strip()
        guid = (item.findtext("guid", default="") or "").strip()
        description = item.findtext("description", default="") or ""
        pubdate_raw = item.findtext("pubDate", default="") or ""
        dt = _parse_pubdate(pubdate_raw)
        if dt is not None and dt < cutoff:
            continue
        payload = json.dumps({
            "title": title, "link": link, "guid": guid,
            "description": description, "pubDate": pubdate_raw},
            sort_keys=True)
        yield {
            "source_native_id": _advisory_id(link, guid),
            "event_date": dt.isoformat() if dt else "",
            "payload": payload,
            "url": link,
        }
        yielded += 1
        if limit is not None and yielded >= limit:
            return


if __name__ == "__main__":
    sys.exit(runner.cli(SOURCE_ID, fetch_events, PARSER_VERSION,
                        "Fetch CISA ICS advisories into raw_events"))
