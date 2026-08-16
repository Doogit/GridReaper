"""SEC EDGAR full-text search fetcher (R5.2, R5.5, R3.4, R3.7, R10.4).

The submissions fetcher (``app.ingest.edgar``) sees only filing *metadata*
for entities we already hold a CIK for. This one searches filing **bodies**
— including the exhibits (EX-99.x press releases) where disclosures actually
live — for each active watchlist entity's name, so a filing that merely
*mentions* a watchlist company is captured and attributed to it. That is the
account-scoped recall lever; classification of these events is a later chunk
(nothing here mints signals).

Endpoint (undocumented but public, read-only GET/JSON per R3.4):

  GET https://efts.sec.gov/LATEST/search-index
      ?q="<entity name>"&forms=8-K,10-K
      &dateRange=custom&startdt=YYYY-MM-DD&enddt=YYYY-MM-DD&from=N

It answers with an Elasticsearch envelope: ``hits.total.value`` (int) and
``hits.hits[]``, each hit carrying ``_id`` = ``"{accession}:{filename}"`` and
a ``_source`` of ``adsh``, ``ciks[]``, ``display_names[]``, ``form``,
``root_forms[]``, ``file_date``, ``items[]``, ``period_ending``, ``sics[]``,
``biz_states[]``. Page size is server-fixed (no ``size`` parameter), so
pagination advances ``from`` by the number of hits actually returned rather
than by an assumed constant — correct whichever page size SEC serves.

Only ``_id`` and ``file_date`` are load-bearing here; every other ``_source``
key is stored verbatim (R3.7) so a renamed field degrades to an empty string
instead of dropping the event.

R5.2: EDGAR's 10 req/s ceiling is shared across *all* EDGAR hosts, so this
module deliberately reuses ``app.ingest.edgar``'s throttle and User-Agent
rather than starting a second budget — one sleep constant, one UA, and live
runs are serialized by the single-writer ingestion lock (R3.2).

Dedupe key is ``{entity_id}:{_id}`` (R10.4): one filing body can match two
watchlist names, and keying on ``_id`` alone would silently drop the second
entity's attribution.

  python -m app.ingest.edgar_fulltext [--window-days N] [--limit N] [--force]
"""
import json
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from app.ingest import edgar, runner

SOURCE_ID = "sec_edgar_fulltext"
PARSER_VERSION = "edgar_fulltext/1.0"
SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
FORMS = "8-K,10-K"          # root forms; amendments roll up under these
MAX_PAGES = 5               # per-entity pagination runaway guard
# EDGAR's 10 req/s budget is shared with app.ingest.edgar (R5.2)
SLEEP_S = edgar.SLEEP_S
USER_AGENT = edgar.USER_AGENT


def _get_page(url):
    """One throttled EDGAR request, on the shared <=10 req/s budget (R5.2).
    Failures propagate; the runner records the run as 'error' (R10.3)."""
    time.sleep(SLEEP_S)
    return edgar._get_json(url)


def _search_url(name, start, end, offset):
    params = {"q": f'"{name}"', "forms": FORMS, "dateRange": "custom",
              "startdt": start, "enddt": end}
    if offset:
        params["from"] = offset
    return f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"


def _split_id(hit_id):
    """'0001326160-26-000012:d8k.htm' -> ('0001326160-26-000012', 'd8k.htm')."""
    accession, _, filename = (hit_id or "").partition(":")
    return accession, filename


def _doc_url(cik, accession, filename):
    """Archive URL of the matched document, or the filing index without one.

    SEC returns zero-padded CIKs but the Archives path wants them unpadded;
    strip rather than int() so a non-numeric CIK yields '' instead of raising
    and failing the whole run."""
    cik_num = (cik or "").strip().lstrip("0")
    if not cik_num.isdigit() or not accession:
        return ""
    base = (f"https://www.sec.gov/Archives/edgar/data/{cik_num}/"
            f"{accession.replace('-', '')}")
    return f"{base}/{filename}" if filename else f"{base}/"


def fetch_events(conn, window_days, limit):
    """Yield one event per (watchlist entity, matching filing document)."""
    if limit is not None and limit <= 0:
        return
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=window_days)).isoformat()
    end = today.isoformat()
    entities = conn.execute(
        "SELECT entity_id, name FROM watchlist_entities "
        "WHERE TRIM(COALESCE(name,'')) != '' AND active = 1 "
        "ORDER BY entity_id").fetchall()
    yielded = 0
    for row in entities:
        name = row["name"].strip()
        offset = 0
        for _ in range(MAX_PAGES):
            data = _get_page(_search_url(name, start, end, offset))
            hits_block = data.get("hits") or {}
            hits = hits_block.get("hits") or []
            if not hits:
                break
            for hit in hits:
                hit_id = hit.get("_id") or ""
                accession, filename = _split_id(hit_id)
                source = dict(hit.get("_source") or {})
                ciks = source.get("ciks") or []
                payload = dict(source)
                payload["_id"] = hit_id
                payload["entity_id"] = row["entity_id"]
                payload["matched_name"] = name
                yield {
                    "source_native_id": f"{row['entity_id']}:{hit_id}",
                    "event_date": source.get("file_date") or "",
                    "url": _doc_url(ciks[0] if ciks else "",
                                    accession or source.get("adsh") or "",
                                    filename),
                    "payload": json.dumps(payload, sort_keys=True),
                }
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            offset += len(hits)
            total = (hits_block.get("total") or {}).get("value") or 0
            if offset >= total:
                break


if __name__ == "__main__":
    sys.exit(runner.cli(
        SOURCE_ID, fetch_events, PARSER_VERSION,
        "Search SEC EDGAR filing bodies for watchlist entity names"))
