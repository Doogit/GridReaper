"""NERC enforcement docket fetcher (R5.5, R3.4, R3.7, R10.4).

The account-scoped ``nerc_enforcement`` trigger is sourced from
"NERC.gov Enforcement;FERC eLibrary" (seeds/triggers.csv) but the docket
itself was never ingested. This fetcher pulls NERC's own Enforcement
Dispositions tables - one page per filing year:

  https://www.nerc.com/programs/enforcement/enforcement-actions/<year>

Each year page is an Optimizely-rendered app whose full content is embedded
as a JSON blob after ``window._model``; the filings table lives at
``pageModel.content[].model.tableHtml`` as three columns (Docket Number,
Title/Summary, Date). One raw_event is emitted per docket row, carrying the
docket number, the row title, every filing date in the row (an original plus
any errata), and every linked document URL.

Dedupe (R10.4) rides the runner's content-hash path rather than a native id,
deliberately: the docket number alone would be stable across an errata added
to the same row weeks later, and ``store_raw_event`` preserves the payload of
an already-seen id - the amended row would be silently dropped. Hashing
(docket, title, dates, links) instead means an unchanged docket dedupes to
'seen' on every re-fetch while an amended one becomes a new raw_event, the
same semantics ``nerc_pages`` uses. The docket number stays in the payload.

window_days selects which filing-year pages to fetch (newest first), floored
at EARLIEST_YEAR - the oldest year NERC's own sidebar exposes. Pages are
fetched sequentially with a politeness sleep (R3.2/R3.4).

  python -m app.ingest.nerc_enforcement [--window-days N] [--limit N] [--force]
"""
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser

from app.ingest import runner

SOURCE_ID = "nerc_enforcement"
PARSER_VERSION = "nerc_enforcement/1.0"
SITE_ROOT = "https://www.nerc.com"
YEAR_URL = f"{SITE_ROOT}/programs/enforcement/enforcement-actions/{{year}}"
USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"
EARLIEST_YEAR = 2017     # oldest year page NERC's own sidebar links
MAX_YEARS = 15           # runaway guard on the window
PAGE_SLEEP_S = 0.5       # be polite between year pages

MODEL_MARKER = "window._model = "
_BLOCK_TAGS = {"p", "br", "div", "li", "tr", "td", "th", "table"}
_US_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


class _TableParser(HTMLParser):
    """Rows of cells from a table fragment. Each cell is
    {'text': str, 'links': [{'text': str, 'href': str}], 'header': bool}."""

    def __init__(self):
        super().__init__()
        self.rows = []
        self._row = None
        self._cell = None
        self._link = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"text": [], "links": [], "header": tag == "th"}
        elif tag == "a" and self._cell is not None:
            self._link = {"text": [], "href": dict(attrs).get("href", "")}
        elif tag in _BLOCK_TAGS and self._cell is not None:
            self._cell["text"].append(" ")

    def handle_endtag(self, tag):
        if tag == "a" and self._link is not None:
            self._link["text"] = _collapse(self._link["text"])
            self._cell["links"].append(self._link)
            self._link = None
        elif tag in ("td", "th") and self._cell is not None:
            self._cell["text"] = _collapse(self._cell["text"])
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._link is not None:
            self._link["text"].append(data)
        if self._cell is not None:
            self._cell["text"].append(data)


def _collapse(chunks):
    return " ".join("".join(chunks).split())


def parse_table(table_html):
    """Rows of cells from one tableHtml fragment."""
    parser = _TableParser()
    parser.feed(table_html)
    return parser.rows


def extract_model(html):
    """The page's embedded ``window._model`` object. Uses the stdlib JSON
    decoder's raw_decode so the object's own braces terminate it rather than
    a brittle regex."""
    start = html.find(MODEL_MARKER)
    if start < 0:
        raise ValueError("window._model not found: NERC page layout changed")
    model, _end = json.JSONDecoder().raw_decode(
        html, start + len(MODEL_MARKER))
    return model


def page_rows(html):
    """Every filings-table row on one year page."""
    page = extract_model(html).get("pageModel") or {}
    rows = []
    for block in page.get("content") or []:
        table_html = ((block or {}).get("model") or {}).get("tableHtml")
        if table_html:
            rows.extend(parse_table(table_html))
    return rows


def parse_dates(text):
    """ISO dates from a Date cell. Whitespace is stripped before matching
    because the published tables contain broken runs like '11 / 7 /20 19'."""
    out = []
    for month, day, year in _US_DATE_RE.findall(re.sub(r"\s+", "", text)):
        try:
            out.append(date(int(year), int(month), int(day)).isoformat())
        except ValueError:
            continue
    return out


def _absolute(href):
    return f"{SITE_ROOT}{href}" if href.startswith("/") else href


def docket_event(page_url, year, cells):
    """Build the fetcher-contract event for one docket row, or None when the
    row is a header or carries no docket and no title."""
    if len(cells) < 2 or any(c["header"] for c in cells):
        return None
    docket = cells[0]["text"]
    title = cells[1]["text"]
    if not docket and not title:
        return None
    if docket.lower().startswith("docket"):
        return None
    date_text = cells[2]["text"] if len(cells) > 2 else ""
    links = [{"text": lk["text"], "href": _absolute(lk["href"])}
             for lk in cells[1]["links"] if lk["href"]]
    filing_dates = parse_dates(date_text)
    payload = {"page_url": page_url, "filing_year": year, "docket": docket,
               "title": title, "date_text": date_text,
               "filing_dates": filing_dates,
               "links": links}
    content_hash = hashlib.sha256("\n".join(
        [docket, title, date_text] + [lk["href"] for lk in links]
    ).encode("utf-8")).hexdigest()
    return {
        "event_date": filing_dates[0] if filing_dates else "",
        "payload": json.dumps(payload, sort_keys=True),
        "url": links[0]["href"] if links else page_url,
        "canonical_url": page_url,
        "content_hash": content_hash,
    }


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


def filing_years(window_days):
    """Filing years covered by the window, newest first."""
    today = datetime.now(timezone.utc).date()
    since = today - timedelta(days=max(window_days or 0, 0))
    first = max(since.year, EARLIEST_YEAR)
    years = [y for y in range(today.year, first - 1, -1)]
    return years[:MAX_YEARS]


def fetch_events(conn, window_days, limit):
    """Yield one event per enforcement docket row across the window.
    HTTP failures propagate; the runner records status 'error' (R10.3)."""
    if limit is not None and limit <= 0:
        return
    yielded = 0
    for index, year in enumerate(filing_years(window_days)):
        if index:
            time.sleep(PAGE_SLEEP_S)
        page_url = YEAR_URL.format(year=year)
        for cells in page_rows(_get_text(page_url)):
            event = docket_event(page_url, year, cells)
            if event is None:
                continue
            yield event
            yielded += 1
            if limit is not None and yielded >= limit:
                return


if __name__ == "__main__":
    sys.exit(runner.cli(
        SOURCE_ID, fetch_events, PARSER_VERSION,
        "Fetch NERC enforcement docket filings into raw_events."))
