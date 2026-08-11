"""NERC public page snapshots (R3.7, R10.4).

Fetches the NERC standards pages and stores each page's visible text as a
raw_events snapshot. Page URLs are module constants (operator-tunable;
verified in live runs):

  CurrentStandardsUnderDevelopment.aspx   standards under development
  CIPStandards.aspx                       CIP standards status

Pages have no native event id, so dedupe rides the runner's content-hash
path (R10.4): the hash is computed here over (page_url, visible text) —
deliberately excluding the fetch date, so an unchanged page dedupes to
'seen' on every re-fetch and a changed page becomes a new raw_event.
Diffing what changed is the classifier chunk's job, not ingestion's.
Tag stripping skips <script>/<style> and collapses whitespace so cosmetic
markup noise (viewstate, indentation) does not destabilize the hash.

window_days is inapplicable (pages are current-state snapshots, not an
event archive); it is accepted and ignored.

  python -m app.ingest.nerc_pages [--limit N] [--force]
"""
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

from app.ingest import runner

USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"
PARSER_VERSION = "nerc_pages/1.0"

PAGE_URLS = [
    "https://www.nerc.com/pa/Stand/Pages/Standards-Under-Development.aspx",
    "https://www.nerc.com/pa/Stand/Pages/CIPStandards.aspx",
]

_SKIP_TAGS = {"script", "style"}


class _TextExtractor(HTMLParser):
    """Visible-text extractor: drops <script>/<style> content entirely;
    entities are unescaped by HTMLParser (convert_charrefs)."""

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self._chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self):
        return " ".join("".join(self._chunks).split())


def strip_html(html):
    """Return the page's visible text with whitespace runs collapsed."""
    extractor = _TextExtractor()
    extractor.feed(html)
    return extractor.text()


def _get_text(url, retries=2):
    """GET a page and return its decoded body (read-only access, R3.4)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "text/html"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def page_event(page_url, html):
    """Build the fetcher-contract event for one page snapshot. The
    content_hash covers only (page_url, text) so re-fetches of an
    unchanged page dedupe regardless of fetch date."""
    text = strip_html(html)
    fetched_date = _today()
    content_hash = hashlib.sha256(
        f"{page_url}\n{text}".encode("utf-8")).hexdigest()
    return {
        "event_date": fetched_date,
        "payload": json.dumps({"page_url": page_url,
                               "fetched_date": fetched_date,
                               "text": text}, sort_keys=True),
        "url": page_url,
        "content_hash": content_hash,
    }


def fetch_events(conn, window_days, limit):
    """Yield one snapshot event per configured page. window_days ignored
    (snapshots have no archive to window over)."""
    if limit is not None and limit <= 0:
        return
    for count, page_url in enumerate(PAGE_URLS):
        if limit is not None and count >= limit:
            return
        yield page_event(page_url, _get_text(page_url))


if __name__ == "__main__":
    sys.exit(runner.cli(
        "nerc_pages", fetch_events, PARSER_VERSION,
        "Snapshot NERC standards pages into raw_events."))
