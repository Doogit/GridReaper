"""SEC EDGAR submissions fetcher (R5.2, R5.5, R3.7, R10.4).

Polls https://data.sec.gov/submissions/CIK{10-digit}.json for every
watchlist entity with a CIK and yields 8-K / 10-K filings (and their
amendments) inside the event window as raw events. The payload is the
filing's own zipped ``filings.recent`` record (R3.7: raw material for later
reprocessing) plus ``entity_id``, ``cik``, and the 8-K ``items`` codes
parsed into a list — Item 1.05 presence is stored but not acted on here
(R10.5 evidence-tier handling is a later chunk).

``filings.recent`` covers an entity's ~1000 most recent filings; the paged
history under ``filings.files`` is out of scope for the 12-month window
(R5.5). Requests sleep 0.2s apart, well under the 10 req/s EDGAR limit
(R5.2). A fetch failure propagates so the runner records the run as
'error' (R10.3) rather than silently skipping an entity.

  python -m app.ingest.edgar [--window-days N] [--limit N] [--force]
"""
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from app.ingest import runner

SOURCE_ID = "sec_edgar_submissions"
PARSER_VERSION = "edgar/1.0"
# SEC requires a "name (contact email)" User-Agent: data.sec.gov returns 403
# both without a contact address and when the UA contains a URL, so this
# deviates from the repo-wide UA form. The GitHub noreply alias is the contact
# of record; it routes to the maintainer without publishing a personal address.
USER_AGENT = "GridSignals/0.1 (contact: 78006305+Doogit@users.noreply.github.com)"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
KEEP_FORMS = {"8-K", "8-K/A", "10-K", "10-K/A"}
SLEEP_S = 0.2


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


def _zip_recent(submissions):
    """filings.recent holds parallel arrays; zip them into per-filing dicts."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    if not recent:
        return []
    n = min(len(v) for v in recent.values())
    return [{k: recent[k][i] for k in recent} for i in range(n)]


def _parse_items(items_cell):
    """'5.02,9.01' -> ['5.02', '9.01']; empty -> []."""
    return [p.strip() for p in (items_cell or "").split(",") if p.strip()]


def _primary_doc_url(cik, filing):
    accession = (filing.get("accessionNumber") or "").replace("-", "")
    doc = filing.get("primaryDocument") or ""
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}"
    return f"{base}/{doc}" if doc else f"{base}/"


def fetch_events(conn, window_days, limit):
    """Yield 8-K/10-K raw events for all watchlist CIKs within the window."""
    if limit is not None and limit <= 0:
        return
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=window_days)).date().isoformat()
    entities = conn.execute(
        "SELECT entity_id, cik FROM watchlist_entities "
        "WHERE TRIM(COALESCE(cik,'')) != '' AND active = 1 "
        "ORDER BY entity_id").fetchall()
    yielded = 0
    for i, row in enumerate(entities):
        if i:
            time.sleep(SLEEP_S)
        cik = row["cik"].strip()
        data = _get_json(SUBMISSIONS_URL.format(cik10=cik.zfill(10)))
        for filing in _zip_recent(data):
            if filing.get("form") not in KEEP_FORMS:
                continue
            filing_date = filing.get("filingDate") or ""
            if filing_date < cutoff:
                continue
            payload = dict(filing)
            payload["entity_id"] = row["entity_id"]
            payload["cik"] = cik
            payload["items"] = _parse_items(filing.get("items"))
            yield {
                "source_native_id": filing.get("accessionNumber") or "",
                "event_date": filing_date,
                "url": _primary_doc_url(cik, filing),
                "payload": json.dumps(payload, sort_keys=True),
            }
            yielded += 1
            if limit is not None and yielded >= limit:
                return


if __name__ == "__main__":
    sys.exit(runner.cli(
        SOURCE_ID, fetch_events, PARSER_VERSION,
        "Fetch SEC EDGAR 8-K/10-K submissions for watchlist CIKs"))
