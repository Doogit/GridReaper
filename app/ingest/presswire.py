"""Press-wire RSS ingestion (R3.7, R10.4).

Two sources share one RSS 2.0 parsing core:

  presswire_prnewswire      PR Newswire energy + utilities list feeds
  presswire_globenewswire   GlobeNewswire energy + utilities industry feeds

Feed URLs are module constants (operator-tunable; verified in live runs):
PR Newswire publishes per-category list feeds under /rss/, GlobeNewswire
under /RssFeed/. Per item we store the item's own raw fields (title,
description, link, guid, pubDate, categories) as the payload — enough raw
material for later reprocessing (R3.7), not a digest.

RSS has no archive: window_days only filters what the feed currently
serves. Items with an unparseable/absent pubDate are kept (cannot be
window-filtered) with event_date = ''. Dedupe (R10.4) keys on the item
guid, falling back to the link. Run one source per invocation:

  python -m app.ingest.presswire --source prnewswire [--window-days N]
  python -m app.ingest.presswire --source globenewswire [--limit N] [--force]
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from app.db.connection import get_connection
from app.ingest import runner

USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"
PARSER_VERSION = "presswire/1.0"

PRNEWSWIRE_FEEDS = [
    "https://www.prnewswire.com/rss/energy-latest-news/energy-latest-news-list.rss",
    "https://www.prnewswire.com/rss/utilities-latest-news/utilities-latest-news-list.rss",
]
# GlobeNewswire serves per-subjectcode feeds (last ~20 releases each); the
# /RssFeed/industry/... form 400s and unknown subjectcodes hang rather than
# error, so only the live-verified Energy feed ships. Add further confirmed
# subjectcode feeds here as they are identified.
GLOBENEWSWIRE_FEEDS = [
    "https://www.globenewswire.com/RssFeed/subjectcode/2-Energy",
]


def _get_text(url, retries=2):
    """GET a feed and return its decoded body (read-only access, R3.4)."""
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
    """RFC-822 pubDate -> aware UTC datetime, or None when unparseable."""
    if not (raw or "").strip():
        return None
    try:
        dt = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fetch_feed_events(feed_urls, window_days, limit):
    """Yield fetcher-contract events for every <item> across feed_urls.
    A feed's HTTP failure propagates: the runner records the run as
    'error' without blocking other sources (R10.3)."""
    if limit is not None and limit <= 0:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    count = 0
    for feed_url in feed_urls:
        root = ET.fromstring(_get_text(feed_url))
        for item in root.iter("item"):
            title = item.findtext("title", default="")
            description = item.findtext("description", default="")
            link = (item.findtext("link", default="") or "").strip()
            guid = (item.findtext("guid", default="") or "").strip()
            pubdate_raw = item.findtext("pubDate", default="")
            categories = [c.text.strip() for c in item.findall("category")
                          if (c.text or "").strip()]
            dt = _parse_pubdate(pubdate_raw)
            if dt is not None and dt < cutoff:
                continue
            payload = json.dumps({
                "title": title, "description": description, "link": link,
                "guid": guid, "pubDate": pubdate_raw,
                "categories": categories}, sort_keys=True)
            yield {
                "source_native_id": guid or link,
                "event_date": dt.isoformat() if dt else "",
                "payload": payload,
                "url": link,
            }
            count += 1
            if limit is not None and count >= limit:
                return


def fetch_events_prnewswire(conn, window_days, limit):
    return _fetch_feed_events(PRNEWSWIRE_FEEDS, window_days, limit)


def fetch_events_globenewswire(conn, window_days, limit):
    return _fetch_feed_events(GLOBENEWSWIRE_FEEDS, window_days, limit)


SOURCES = {
    "prnewswire": ("presswire_prnewswire", fetch_events_prnewswire),
    "globenewswire": ("presswire_globenewswire", fetch_events_globenewswire),
}


def main(argv=None):
    """Like runner.cli but with a --source selector (one source per run)."""
    parser = argparse.ArgumentParser(
        description="Ingest press-wire RSS feeds into raw_events.")
    parser.add_argument("--source", required=True, choices=sorted(SOURCES),
                        help="which press-wire source to run")
    parser.add_argument("--window-days", type=int, default=365,
                        help="event window in days (default 365, R5.5)")
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after N events (debugging)")
    parser.add_argument("--force", action="store_true",
                        help="ignore the source TTL")
    args = parser.parse_args(argv)
    source_id, fetcher = SOURCES[args.source]

    with runner.ingest_lock():
        conn = get_connection()
        try:
            summary = runner.run_source(conn, source_id, fetcher,
                                        PARSER_VERSION, force=args.force,
                                        window_days=args.window_days,
                                        limit=args.limit)
        finally:
            conn.close()
    line = (f"{source_id}: {summary['status']} "
            f"seen={summary['records_seen']} new={summary['records_new']}")
    if summary["error_state"]:
        line += f" error_state={summary['error_state']}"
    print(line)
    return 1 if summary["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
