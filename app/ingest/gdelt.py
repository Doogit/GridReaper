"""GDELT 2.0 DOC API fetcher (R5.x, R6.3, R10.4) — store-only at MVP.

Queries the GDELT DOC API in ArtList/JSON mode for the **watchlist's own
entity terms** (R6.3), not a static sector keyword list; every article in
``articles`` becomes one raw_event keyed by its ``url``, with the article
dict stored as-is (R3.7: keep the source's own record, don't pre-digest).
No classification here.

R6.3 wiring — an entity contributes its name plus its curated aliases as
quoted phrases, minus any term registered against it in
``entity_collision_terms``. Those are *negative* terms (``app/resolve.py``),
so they are dropped from the positive set rather than negated in the query:
every seeded collision term is a prefix of the name it collides with
(``Dominion`` / ``Dominion Energy``), so a GDELT ``-"Dominion"`` clause would
exclude the very coverage we want. Concretely this drops the bare name
``Otter Tail`` (also a Minnesota county and river) while its disambiguated
aliases ``Otter Tail Power`` / ``Otter Tail Corporation`` still query. An
entity whose every term collides contributes nothing rather than an
ambiguous query, and an empty watchlist issues no request at all.

Terms are chunked ``MAX_TERMS_PER_QUERY`` to a request: GDELT caps *every*
response at ``MAX_RECORDS``, so one OR block over all 171 entities would let
a single busy name crowd the rest out of one 250-row response. On the
current watchlist that is 8 requests per run (was 1), spaced ``SLEEP_S``
apart (R3.4 politeness); the whole run stays inside the single-writer
ingestion lock (R3.2). An article matching two chunks is yielded once.

GDELT quirks encoded here (verified against the DOC 2.0 API docs):
  - ``seendate`` is a compact ``YYYYMMDDTHHMMSSZ`` timestamp (e.g.
    ``20260812T120000Z``), not ISO-8601; ``_seendate_to_iso`` parses it (and
    the bare ``YYYYMMDDHHMMSS`` variant) to a UTC ISO date. Missing/malformed
    seendate keeps the record with event_date '' — never silently dropped.
  - a no-results response can be ``{}`` or ``{"articles": []}``; ``.get`` on
    both yields nothing without error.
  - ``maxrecords`` caps at 250 (the API's documented ceiling).
  - ``(`` ``)`` ``"`` delimit query syntax, so a phrase is sanitized before
    quoting: a trailing parenthetical is dropped (``TXNM Energy (PNM)`` ->
    ``TXNM Energy``) and stray metacharacters are stripped.
  - Boolean OR blocks cannot be nested, so a chunk is one flat OR block
    ANDed with the ``sourcecountry:US`` context operator.
  - the DOC API covers only a rolling ~3-month window, so ``window_days`` is
    used to derive the request timespan (capped) *and* to filter returned
    articles by seendate; a large ``--window-days`` cannot reach older news.
  - payloads are non-ASCII-heavy (foreign-language titles/domains); stored as
    UTF-8 as-is — no encoding "fix" (console mojibake is display-only).

  python -m app.ingest.gdelt [--window-days N] [--limit N] [--force]
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

SOURCE_ID = "gdelt"
PARSER_VERSION = "gdelt/2.0"
USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"

API_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_RECORDS = 250                 # DOC API documented ceiling
MAX_TIMESPAN_DAYS = 90            # rolling ~3-month coverage window
MAX_TERMS_PER_QUERY = 25          # OR-block width; keeps each request's 250 shared by few names
SLEEP_S = 1.0                     # between chunk requests (R3.4)
SECTOR_CONTEXT = "sourcecountry:US"   # narrows to US coverage of US entities

_PARENTHETICAL = re.compile(r"\s*\([^)]*\)")
_QUERY_META = re.compile(r'["()]')


def _get_json(url, retries=2):
    """GET url as JSON, retrying transient failures with backoff.

    Verified against the live DOC API under throttling (R9.6 re-trial):
    an overloaded backend sometimes answers 200 with an empty/non-JSON body
    instead of the documented 429, so json.JSONDecodeError is retried the
    same as a URLError/TimeoutError rather than failing the whole chunk on
    the first malformed response."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
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


def _phrase(text):
    """A quotable GDELT phrase: parenthetical dropped, query metacharacters
    stripped, whitespace collapsed. Returns '' when nothing usable is left."""
    s = _PARENTHETICAL.sub(" ", text or "")
    return " ".join(_QUERY_META.sub(" ", s).split())


def entity_terms(conn):
    """Positive query phrases for the active watchlist (R6.3).

    Name + curated aliases per entity, minus that entity's collision terms,
    deduped case-insensitively and ordered by entity_id then term so a run's
    chunking is deterministic.
    """
    collisions = {}
    for row in conn.execute(
            "SELECT entity_id, term FROM entity_collision_terms"):
        collisions.setdefault(row["entity_id"], set()).add(
            row["term"].strip().casefold())

    rows = conn.execute(
        "SELECT entity_id, name AS term FROM watchlist_entities "
        " WHERE active = 1 "
        "UNION ALL "
        "SELECT a.entity_id, a.alias FROM entity_aliases a "
        " JOIN watchlist_entities w ON w.entity_id = a.entity_id "
        " WHERE w.active = 1 "
        "ORDER BY entity_id, term").fetchall()

    phrases = []
    seen = set()
    for row in rows:
        term = row["term"] or ""
        if term.strip().casefold() in collisions.get(row["entity_id"], ()):
            continue
        phrase = _phrase(term)
        key = phrase.casefold()
        if phrase and key not in seen:
            seen.add(key)
            phrases.append(phrase)
    return phrases


def _build_url(phrases, window_days):
    """One request URL for a chunk of phrases (flat OR block + context)."""
    timespan_days = max(1, min(window_days, MAX_TIMESPAN_DAYS))
    block = " OR ".join(f'"{p}"' for p in phrases)
    if len(phrases) > 1:
        block = f"({block})"        # a 1-term OR block is a syntax error
    params = urllib.parse.urlencode({
        "query": f"{block} {SECTOR_CONTEXT}",
        "mode": "ArtList",
        "format": "json",
        "maxrecords": MAX_RECORDS,
        "timespan": f"{timespan_days}d",
    })
    return f"{API_BASE}?{params}"


def build_urls(conn, window_days):
    """The per-run request URLs: watchlist terms chunked MAX_TERMS_PER_QUERY."""
    phrases = entity_terms(conn)
    return [_build_url(phrases[i:i + MAX_TERMS_PER_QUERY], window_days)
            for i in range(0, len(phrases), MAX_TERMS_PER_QUERY)]


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
    yielded = 0
    seen_urls = set()
    for i, url in enumerate(build_urls(conn, window_days)):
        if i:
            time.sleep(SLEEP_S)
        data = _get_json(url)
        for article in data.get("articles", []):
            article_url = article.get("url", "")
            if article_url and article_url in seen_urls:
                continue        # same article matched an earlier chunk
            event_date = _seendate_to_iso(article.get("seendate"))
            # YYYY-MM-DD sorts lexically; keep records we can't date
            if event_date and event_date < cutoff:
                continue
            if article_url:
                seen_urls.add(article_url)
            yield {
                "source_native_id": article_url,
                "event_date": event_date,
                "payload": json.dumps(article, sort_keys=True),
                "url": article_url,
            }
            yielded += 1
            if limit is not None and yielded >= limit:
                return


if __name__ == "__main__":
    sys.exit(runner.cli(SOURCE_ID, fetch_events, PARSER_VERSION,
                        "Fetch GDELT DOC API articles for watchlist entity "
                        "terms into raw_events"))
