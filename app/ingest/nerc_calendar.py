"""NERC regulatory calendar fetcher (R5.5, R3.4, R3.7, R10.4).

Closes R5.5's regulatory-calendar-*event* clause. ``app/ingest/nerc_pages``
snapshots current-state standards pages; this fetcher pulls the dated
milestones themselves - standards drafting-team meetings, ballot and comment
webinars, Standards Committee / RSTC / Security Working Group sessions -
from the calendar the nerc.com events page itself calls:

  GET https://www.nerc.com/api/search/events?year=YYYY&month=M

The page is a JS app; the JSON above is the endpoint its own bundle calls
(verified live). ``allItems`` is the flat, complete list for the month
(``itemsByDay`` is the same items grouped for display), so one request per
month is one full month of the calendar.

window_days is read *forward*: a calendar is upcoming milestones, not an
archive. Months from the current month through the month containing
today+window_days are fetched in order, capped at MAX_MONTHS as a runaway
guard, with a politeness sleep between requests (sequential only, R3.2).

event_date is the published calendar date, taken from the Eastern
(``isSelected``) entry of ``dateInfoByTimeZone``. Note the API's
``eventStartDate`` carries a 'Z' suffix but is a local wall-clock time per
timezone entry, not a UTC instant - so only the date component is used and
no timezone conversion is implied. The whole ``dateInfoByTimeZone`` array
is preserved in the payload (R3.7), so the exact instant stays recoverable.

Dedupe rides the event's own page path (``/events/09-2-26-rtos-meeting``)
as source_native_id, so a re-run over the same month adds 0 rows (R10.4).

  python -m app.ingest.nerc_calendar [--window-days N] [--limit N] [--force]
"""
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from app.ingest import runner

SOURCE_ID = "nerc_calendar"
PARSER_VERSION = "nerc_calendar/1.0"
SITE_ROOT = "https://www.nerc.com"
EVENTS_API = f"{SITE_ROOT}/api/search/events"
USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"
MAX_MONTHS = 18          # runaway guard on the forward horizon
MONTH_SLEEP_S = 0.5      # be polite between month requests

_ISO_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _get_json(url, retries=2):
    """GET a JSON document (read-only access, R3.4)."""
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


def _today():
    return datetime.now(timezone.utc).date()


def months_ahead(window_days):
    """(year, month) pairs from the current month through the month holding
    today+window_days, capped at MAX_MONTHS."""
    today = _today()
    last = today + timedelta(days=max(window_days or 0, 0))
    year, month = today.year, today.month
    out = []
    while (year, month) <= (last.year, last.month) and len(out) < MAX_MONTHS:
        out.append((year, month))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return out


def event_date(item, year, month):
    """Published calendar date of one event. Prefers the Eastern
    (isSelected) timezone entry, then any entry, then the grid day."""
    infos = item.get("dateInfoByTimeZone") or []
    for candidates in ([i for i in infos if i.get("isSelected")], infos):
        for info in candidates:
            m = _ISO_DATE_RE.match(str(info.get("eventStartDate") or ""))
            if m:
                return m.group(1)
    day = str(item.get("day") or "").strip()
    if day.isdigit():
        return f"{year:04d}-{month:02d}-{int(day):02d}"
    return ""


def calendar_event(year, month, item):
    """Build the fetcher-contract event for one calendar item."""
    path = str(item.get("url") or "").strip()
    absolute = f"{SITE_ROOT}{path}" if path.startswith("/") else path
    return {
        "source_native_id": path,
        "event_date": event_date(item, year, month),
        "payload": json.dumps({"calendar_year": year,
                               "calendar_month": month,
                               "event": item}, sort_keys=True),
        "url": absolute,
        "canonical_url": absolute,
    }


def fetch_events(conn, window_days, limit):
    """Yield one event per NERC calendar item across the forward window.
    HTTP failures propagate; the runner records status 'error' (R10.3)."""
    if limit is not None and limit <= 0:
        return
    yielded = 0
    today = _today().isoformat()
    for index, (year, month) in enumerate(months_ahead(window_days)):
        if index:
            time.sleep(MONTH_SLEEP_S)
        query = urllib.parse.urlencode({"year": year, "month": month})
        data = _get_json(f"{EVENTS_API}?{query}")
        for item in data.get("allItems") or []:
            event = calendar_event(year, month, item)
            if event["event_date"] and event["event_date"] < today:
                continue
            yield event
            yielded += 1
            if limit is not None and yielded >= limit:
                return


if __name__ == "__main__":
    sys.exit(runner.cli(
        SOURCE_ID, fetch_events, PARSER_VERSION,
        "Fetch NERC regulatory calendar events into raw_events."))
