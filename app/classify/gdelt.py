"""GDELT silent-trial classifier (R9.4, R9.6, R6.3, R4.1, R7.2, R10.2).

DELIBERATELY UNWIRED. This module is NOT in ``deploy/ingest_pipeline.sh`` and
must not be added to it. GDELT is ``approved_store_only`` in
``seeds/source_policies.csv``, and R9.6 makes GDELT classification "contingent
on silent-trial precision" — so the rules below exist to be MEASURED, not to
mint cards. A future session should read this paragraph before "fixing" the
missing pipeline entry: the absence IS the feature. Three independent things
keep GDELT out of the feed, and all three must be undone on purpose:

  1. this module has no ``classify_runner.cli`` entry point, so there is no
     ``python -m app.classify.gdelt`` run that writes signals — only the
     read-only ``--report`` harness below;
  2. it is absent from ``deploy/ingest_pipeline.sh``, the real pipeline chain
     (pinned by ``tests/test_packaging.py``);
  3. ``ma_divestiture`` has no ``allowed_scopes`` in
     ``app.db.load_seeds.TRIGGER_SCOPES``, so even a hand-run through
     ``run_classifier`` drops every candidate as ``dropped_scope`` (R7.2).

WHAT THE STORE ACTUALLY CONTAINS. A GDELT DOC API article payload is
``{domain, language, seendate, socialimage, title, url, url_mobile}`` — a
headline and a link, and nothing else. There is no body, abstract or summary to
condition on, so every rule here is a TITLE-ONLY grammar. That ceiling is the
single most important fact about GDELT as a signal source: the classifiers over
primary sources (``regulatory``, ``incident``) get structured fields and
abstracts, this one gets a wire headline. Confidence is capped accordingly.

TRIGGERS. ``seeds/triggers.csv`` lists GDELT as a primary source for exactly
two triggers, ``ma_divestiture`` and ``peer_incident``. Only the first is
implemented:

  ma_divestiture (account)  corporate-action grammar over the title — a strict
                            conjunction of (a) a watchlist entity term and
                            (b) an M&A predicate. See below.
  peer_incident (sector)    NOT IMPLEMENTED. Measured over the 250 stored
                            articles, the corpus contains zero peer-breach
                            headlines (the closest is a security-newswire topic
                            index page). A rule with no positives cannot be
                            tested against real data and would be speculative
                            code, so it is left unwritten rather than written
                            blind.

ma_divestiture grammar (precision over recall, R9.4 — a strict conjunction):

  term       the title must contain a watchlist entity term. The terms come
             from ``app.ingest.gdelt.entity_terms`` — the SAME per-entity
             disambiguated set the fetcher queries with (R6.3), so a name whose
             every form collides ("Otter Tail") contributes nothing here for
             the same reason it contributes nothing there. One candidate per
             distinct matched term: in a merger both parties genuinely carry
             the trigger, and the framework rolls each up to its top-level
             account (R6.5). The matched term is the ``entity_name_hint``; the
             framework resolves it (R6.2) — this module never attributes.
  predicate  the title must contain an explicit corporate-action verb
             (merge / acquire / divest / take over / "to buy" / "combines
             with"). "Deal" alone is NOT a predicate: the stored corpus uses it
             for franchise renewals ("Clearwater ... as Duke Energy Deal
             Expires") and analyst commentary ("$67 Billion Deal Could Change
             the Revenue Script") far more often than for corporate actions.
  veto       market-commentary headlines are dropped whole. "to buy" is the
             highest-yield predicate in real M&A coverage AND the stock-picking
             listicle's favourite verb ("10 Best Low Priced Stocks to Buy",
             "3 Dividend Stocks to Buy and Hold"), so titles carrying
             stock/share/dividend/price-target/earnings chatter are refused.
             This knowingly costs true positives (a real merger reported as a
             stock move) — a recall loss taken to protect precision, exactly
             the trade ``app/classify/regulatory.py`` documents.

Evidence is the article title verbatim (R4.1: nothing surfaces unsourced), and
the headline quotes it, so every word a card would render traces to the raw
event (R10.6 provenance guard).

Silent-trial harness (read-only, writes nothing):

  python -m app.classify.gdelt --report [--db PATH] [--jsonl PATH]

opens the store ``mode=ro`` via a URI connection and prints one line per fired
candidate. It exists so the operator can reproduce the trial's fired set
without trusting a transcript.
"""
import argparse
import json
import re
import sqlite3
import sys

from app.db.connection import DEFAULT_DB_PATH
from app.ingest.gdelt import entity_terms

CLASSIFIER_ID = "gdelt"
PARSER_VERSION = "gdelt/0.1-trial"
SOURCE_ID = "gdelt"

# Title-only evidence, one wire headline and no body: below the 0.8 the
# primary-source classifiers use and below the 0.75 an EDGAR-named account
# gets. A GDELT card is a lead, not a filing.
GDELT_CONFIDENCE = 0.6
MAX_HEADLINE_CHARS = 140

_MA_RE = re.compile(
    r"(?<!\w)(?:mergers?|merged|merges|merging|acquisitions?|acquires?|"
    r"acquired|acquiring|divestitures?|divests?|divesting|takeovers?|"
    r"to\s+buy|combines\s+with)(?!\w)", re.IGNORECASE)

_MARKET_CHATTER_RE = re.compile(
    r"(?<!\w)(?:stocks?|shares?|price\s+targets?|dividends?|watchlist|"
    r"bullish|bearish|earnings|buy\s+and\s+hold|hand\s+over\s+fist|"
    r"underperforming|rallied)(?!\w)", re.IGNORECASE)


def _terms_pattern(terms):
    """One longest-first alternation over the watchlist terms.

    Longest first so "NextEra Energy Partners" wins over "NextEra Energy" at
    the same position; whitespace inside a term matches any run of whitespace
    because GDELT titles are space-padded around punctuation. Returns None for
    an empty watchlist (an empty alternation would match everywhere)."""
    if not terms:
        return None
    parts = sorted(terms, key=lambda t: (-len(t), t))
    alts = [r"\s+".join(re.escape(w) for w in t.split()) for t in parts]
    return re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)",
                      re.IGNORECASE)


def matched_terms(title, pattern):
    """Watchlist terms present in the title, in order of first appearance and
    deduped case-insensitively. ``re.finditer`` never overlaps, so a longer
    term consumes the shorter one nested inside it."""
    if pattern is None:
        return []
    out, seen = [], set()
    for m in pattern.finditer(title or ""):
        text = " ".join(m.group(0).split())
        key = text.casefold()
        if key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _headline(term, title):
    text = f"{term} in M&A coverage: {title}"
    if len(text) > MAX_HEADLINE_CHARS:
        text = text[:MAX_HEADLINE_CHARS - 1].rstrip() + "…"
    return text


def classify_gdelt(conn, raw, pattern=None):
    """GDELT article -> ma_divestiture account candidates (see the grammar in
    the module docstring). ``pattern`` is the compiled watchlist alternation;
    it is built from ``conn`` when not supplied, and callers classifying many
    events should build it once."""
    try:
        payload = json.loads(raw["payload"] or "")
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    title = " ".join((payload.get("title") or "").split())
    if not title:
        return []
    if _MARKET_CHATTER_RE.search(title):
        return []
    if not _MA_RE.search(title):
        return []
    if pattern is None:
        pattern = _terms_pattern(entity_terms(conn))

    event_date = raw["event_date"] or ""
    return [{
        "trigger_id": "ma_divestiture", "signal_scope": "account",
        "entity_id": None, "entity_name_hint": term,
        "event_date": event_date,
        "headline": _headline(term, title),
        "evidence": [{"text": title, "locator": "title"}],
        "confidence": GDELT_CONFIDENCE,
    } for term in matched_terms(title, pattern)]


SOURCES = {SOURCE_ID: classify_gdelt}


def report(db_path, out=sys.stdout, jsonl_path=None):
    """Silent-trial harness (R9.4/R9.6): run the grammar over every stored
    GDELT raw_event and report the fired candidates. READ-ONLY — the store is
    opened ``mode=ro`` and nothing is written back, which is why this is not a
    ``classify_runner.cli``: a trial that could mint signals is not silent."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        pattern = _terms_pattern(entity_terms(conn))
        rows = conn.execute(
            "SELECT * FROM raw_events WHERE source_id = ? "
            "ORDER BY first_seen_at, raw_event_id", (SOURCE_ID,)).fetchall()
        fired = []
        for raw in rows:
            for cand in classify_gdelt(conn, raw, pattern):
                fired.append({"raw_event_id": raw["raw_event_id"],
                              "url": raw["url"],
                              "event_date": cand["event_date"],
                              "entity_name_hint": cand["entity_name_hint"],
                              "trigger_id": cand["trigger_id"],
                              "headline": cand["headline"],
                              "evidence": cand["evidence"][0]["text"]})
    finally:
        conn.close()
    events = len({f["raw_event_id"] for f in fired})
    print(f"gdelt silent trial: raw_events={len(rows)} events_fired={events} "
          f"candidates={len(fired)}", file=out)
    for f in fired:
        print(f"{f['raw_event_id']}\t{f['entity_name_hint']}\t{f['evidence']}",
              file=out)
    if jsonl_path:
        with open(jsonl_path, "w", encoding="utf-8") as fh:
            for f in fired:
                fh.write(json.dumps(f, ensure_ascii=False, sort_keys=True)
                         + "\n")
    return fired


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="GDELT silent trial (R9.6): report what the store-only "
                    "GDELT corpus WOULD classify. Read-only; mints nothing.")
    parser.add_argument("--report", action="store_true", required=True,
                        help="the only mode: print fired candidates")
    parser.add_argument("--db", default=DEFAULT_DB_PATH,
                        help=f"store to read (default {DEFAULT_DB_PATH})")
    parser.add_argument("--jsonl", default=None,
                        help="also write the fired candidates as JSONL")
    args = parser.parse_args()
    report(args.db, jsonl_path=args.jsonl)
