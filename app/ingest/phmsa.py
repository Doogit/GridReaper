"""PHMSA pipeline-enforcement fetcher (R6, U5 of the combo-engine plan).

Polls PHMSA's public tab-delimited "Pipeline Enforcement Raw Data" feed
(https://primis.phmsa.dot.gov/enforcement-data/), the same live-verified
endpoint app/spikes/phmsa_probe.py measured. One row per enforcement CASE
(not per order), covering every PHMSA-regulated pipeline operator
nationwide -- not just the watchlist -- so this fetcher stores every
qualifying-case-type row inside the window and leaves entity attribution and
midstream/LNG subsector scoping to app/classify/phmsa_enforcement.py.

CPF_Number IS A GENUINE STABLE KEY, UNLIKE THE PROBE'S NARROWER READ. The
probe (app/spikes/phmsa_probe.py) only reads Operator_Name/Case_Type/
Opened_Date, which are not unique per case (two same-day same-type cases
against one operator would collapse to the same probe-analysis bucket, which
is fine for a measurement). This fetcher reads the feed's actual header
(confirmed live 2026-08-18: CPF_Number, Operator_ID, Operator_Name,
Operator_Searchable_Name, Region, Pipeline_Type, How_Discovered?, Case_Type,
Notice_Actions, Cited_Regulations, Violation_Category, Proposed_Penalties,
Assessed_Penalties, Collected_Penalties, Case_Status, Opened_Date, and about
three dozen further order/date/indicator columns) and confirmed CPF_Number is
present and unique on every one of the feed's 5,073 live rows -- so it is
used as source_native_id, giving exact per-case dedupe rather than the
probe's coarser measurement-only grouping.

QUALIFYING CASE TYPES, per R6: Corrective Action Order (CAO), Notice of
Probable Violation (NOPV), Warning Letter (WL) -- the same three the probe
measured against. Notice of Amendment and Safety Order are fetch-time
filtered out; they are not qualifying enforcement severities for this
trigger and keeping them would only grow the store with rows the classifier
would drop anyway.

R10.6 (field allowlist): PHMSA's enforcement-data schema (inspected live
2026-08-18 via the feed's own header row) carries no individual
attorney/officer/signatory field -- every column is case-, operator-, or
order-level (CPF_Number, Operator_Name, Case_Type, Cited_Regulations,
penalty amounts, dates, ...), never an individual person. The classifier
(app/classify/phmsa_enforcement.py) additionally only ever quotes a fixed,
named subset of those columns into headline/evidence text, so no column this
fetcher stores could reach a card even if that determination changes later.

  python -m app.ingest.phmsa [--window-days N] [--limit N] [--force]
"""
import csv
import io
import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

from app.ingest import runner

SOURCE_ID = "phmsa_enforcement"
PARSER_VERSION = "phmsa/1.0"
USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"
HTTP_TIMEOUT = 60

# Live-verified 2026-08-18 (see module docstring, and app/spikes/
# phmsa_probe.py's own verification of the same URL): the tab-delimited
# "Raw Data" download linked from
# https://primis.phmsa.dot.gov/enforcement-data/. Hardcoded rather than
# scraped at runtime -- a future session that finds this 404ing should
# re-extract the link from the landing page by hand and revise this
# constant, matching RAW_DATA_URL's own precedent in phmsa_probe.py.
RAW_DATA_URL = ("https://primis.phmsa.dot.gov/enforcement-documents/"
                 "PHMSA%20Pipeline%20Enforcement%20Raw%20Data.txt")

QUALIFYING_CASE_TYPES = frozenset({
    "Corrective Action Order", "Notice of Probable Violation",
    "Warning Letter"})

# Below this, an empty parse is plausibly just a small/canned response (test
# fixtures, an edge-case near-empty feed); above it, a substantial response
# yielding 0 rows is almost certainly a shape mismatch, not real data -- see
# the PARSE ANOMALY guard in fetch_events.
MIN_RESPONSE_BYTES_FOR_ANOMALY_CHECK = 1000

_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")


def _clean(text):
    return " ".join((text or "").split())


def _iso_date(text):
    """PHMSA's own ``M/D/YY`` format (2-digit year) -> ISO date, or "" when
    missing or unparseable -- never guessed at. Matches
    app.spikes.phmsa_probe._iso_date's pivot rule (00-49 -> 2000s,
    50-99 -> 1900s); the live feed's observed range (1990s-2026) never
    approaches that boundary."""
    t = "" if text is None else str(text).strip()
    m = _DATE_RE.match(t)
    if not m:
        return ""
    month, day, year_raw = m.groups()
    year = int(year_raw)
    if len(year_raw) == 2:
        year += 2000 if year < 50 else 1900
    try:
        return date(year, int(month), int(day)).isoformat()
    except ValueError:
        return ""


def _fetch_feed_text():
    """One GET against the PHMSA enforcement-data raw feed. Unlike the
    probe's containment-first probe_enforcement_feed, a fetch failure here
    PROPAGATES so the runner records the run as 'error' (R10.3) rather than
    silently yielding nothing."""
    req = urllib.request.Request(
        RAW_DATA_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "text/plain"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_records(text):
    """Tab-delimited PHMSA enforcement feed text -> list of row dicts (full
    column set preserved, R3.7). A row missing CPF_Number, Operator_Name, or
    Case_Type is dropped as malformed, never guessed at."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")), delimiter="\t")
    records = []
    for rec in reader:
        cpf_number = _clean(rec.get("CPF_Number"))
        org = _clean(rec.get("Operator_Name"))
        case_type = _clean(rec.get("Case_Type"))
        if not cpf_number or not org or not case_type:
            continue
        row = dict(rec)
        row["CPF_Number"] = cpf_number
        row["Operator_Name"] = org
        row["Case_Type"] = case_type
        records.append(row)
    return records


def fetch_events(conn, window_days, limit):
    """Yield one raw event per qualifying-case-type PHMSA enforcement case
    inside the window. Fetch/parse failures propagate (R10.3)."""
    if limit is not None and limit <= 0:
        return
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=window_days)).date().isoformat()
    text = _fetch_feed_text()
    records = parse_records(text)
    # PARSE ANOMALY guard (found in code review): a WAF challenge, a
    # maintenance interstitial, or a reshaped feed can still return HTTP 200
    # with a substantial body that simply doesn't tab-delimit into the
    # expected columns -- every row would then lack CPF_Number/Operator_Name/
    # Case_Type and parse_records silently returns []. Without this,
    # run_source would record a clean "success, 0 new" run indistinguishable
    # from a genuine feed with nothing new to report. Mirrors app/spikes/
    # phmsa_probe.py's own PARSE ANOMALY check (same failure mode, a
    # measured zero vs. a broken fetch) by raising instead, so R10.3 records
    # the run as 'error'. A short/empty body (e.g. a canned test fixture)
    # legitimately parsing to zero rows is not flagged.
    if not records and len(text) > MIN_RESPONSE_BYTES_FOR_ANOMALY_CHECK:
        raise ValueError(
            f"PHMSA feed fetch returned a {len(text)}-byte response but "
            "parse_records() extracted 0 rows -- likely a maintenance/"
            "interstitial page or a reshaped feed, not a genuine empty "
            "dataset")
    yielded = 0
    for row in records:
        if row["Case_Type"] not in QUALIFYING_CASE_TYPES:
            continue
        opened_date = _iso_date(row.get("Opened_Date"))
        if not opened_date or opened_date < cutoff:
            continue
        yield {
            "source_native_id": row["CPF_Number"],
            "event_date": opened_date,
            "payload": json.dumps(row, sort_keys=True),
        }
        yielded += 1
        if limit is not None and yielded >= limit:
            return


if __name__ == "__main__":
    sys.exit(runner.cli(
        SOURCE_ID, fetch_events, PARSER_VERSION,
        "Fetch PHMSA pipeline enforcement cases (CAO/NOPV/Warning Letter)"))
