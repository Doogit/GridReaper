"""Federal Register fetcher: FERC + TSA documents (R5.5, R3.7, R10.4).

Pulls documents.json filtered to the Federal Energy Regulatory Commission
and the Transportation Security Administration, window-bounded on
publication_date, following next_page_url pagination. The payload stores
the document record exactly as the API returned it (R3.7: enough raw
material to reprocess later); an explicit fields[] list keeps that record
substantive (abstract, docket ids, CFR refs, dates). Dedupe is by
document_number via the runner (R10.4). Run as:

  python -m app.ingest.federal_register [--window-days N] [--limit N] [--force]
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from app.ingest import runner

SOURCE_ID = "federal_register"
PARSER_VERSION = "federal_register/1.0"
API_URL = "https://www.federalregister.gov/api/v1/documents.json"
USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"
AGENCIES = ["federal-energy-regulatory-commission",
            "transportation-security-administration"]
FIELDS = ["document_number", "title", "type", "abstract", "publication_date",
          "agencies", "agency_names", "docket_ids", "regulation_id_numbers",
          "cfr_references", "effective_on", "comments_close_on",
          "significant", "html_url", "pdf_url", "action"]
PER_PAGE = 100
MAX_PAGES = 100         # pagination runaway guard
PAGE_SLEEP_S = 0.3      # be polite between pages


def _get_json(url, retries=2):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))


def _first_page_url(window_days):
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).date()
    params = [("conditions[agencies][]", a) for a in AGENCIES]
    params += [("fields[]", f) for f in FIELDS]
    params += [("conditions[publication_date][gte]", since.isoformat()),
               ("per_page", str(PER_PAGE)),
               ("order", "newest")]
    return f"{API_URL}?{urllib.parse.urlencode(params)}"


def fetch_events(conn, window_days, limit):
    """Yield one event per Federal Register document (runner contract).
    HTTP failures propagate; the runner records them as status 'error'
    (R10.3)."""
    if limit is not None and limit <= 0:
        return
    url = _first_page_url(window_days)
    yielded = 0
    for _ in range(MAX_PAGES):
        data = _get_json(url)
        for doc in data.get("results", []):
            yield {
                "source_native_id": doc.get("document_number", ""),
                "event_date": doc.get("publication_date", ""),
                "payload": json.dumps(doc),
                "url": doc.get("html_url", ""),
                "canonical_url": doc.get("html_url", ""),
            }
            yielded += 1
            if limit is not None and yielded >= limit:
                return
        url = data.get("next_page_url")
        if not url:
            return
        time.sleep(PAGE_SLEEP_S)


if __name__ == "__main__":
    sys.exit(runner.cli(SOURCE_ID, fetch_events, PARSER_VERSION,
                        "Fetch FERC + TSA Federal Register documents"))
