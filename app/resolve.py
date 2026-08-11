"""Entity resolution (PRD section 6).

Deterministic match first (R6.1): CIK, ticker, LEI, then the curated alias
table. Fuzzy name match is fallback only (R6.2); results below the match
threshold become review_queue candidates and MUST NOT auto-fire cards.
Collision terms (R6.3) are negative terms: an input equal to one never
auto-matches on its own - it needs corroborating context or a reviewer.
Every auto/reviewed match is logged to entity_match_decisions (R6.4).
"""
import difflib
import json
import re
from collections import namedtuple
from datetime import datetime, timezone

PARSER_VERSION = "entity-resolve/1.0"

MATCH_THRESHOLD = 0.90   # fuzzy ratio >= this and unambiguous -> auto-match
REVIEW_THRESHOLD = 0.75  # fuzzy ratio >= this -> review candidate; below -> none

# Trailing corporate suffix tokens dropped during normalization.
_SUFFIXES = {"inc", "incorporated", "corp", "corporation", "co", "company",
             "llc", "llp", "lp", "plc", "ltd", "limited", "holdings",
             "holding", "group"}

_PUNCT = re.compile(r"[^\w\s]")

Resolution = namedtuple(
    "Resolution",
    # status: 'matched' | 'review' | 'none'
    # candidates: list of (entity_id, confidence), best first
    ["status", "entity_id", "method", "confidence",
     "matched_terms", "rejected_terms", "candidates"])

_NO_MATCH = Resolution("none", None, None, 0.0, [], [], [])


def normalize(name):
    """Lowercase, unify '&' -> 'and', strip punctuation, drop trailing
    corporate suffixes, collapse whitespace."""
    s = (name or "").lower().replace("&", " and ")
    s = _PUNCT.sub(" ", s)
    tokens = s.split()
    while tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


class EntityResolver:
    """Loads the watchlist + alias/collision tables once; resolve() is pure
    lookup after that. Rebuild the resolver after watchlist edits."""

    def __init__(self, conn):
        self._by_cik = {}
        self._by_ticker = {}
        self._by_lei = {}
        self._by_name = {}      # normalized name/alias -> set(entity_id)
        self._collisions = {}   # normalized term -> set(entity_id)
        self._terms = {}        # entity_id -> set of normalized positive terms

        for r in conn.execute(
                "SELECT entity_id, name, cik, lei, ticker FROM watchlist_entities"):
            eid = r["entity_id"]
            if (r["cik"] or "").strip():
                self._by_cik[r["cik"].strip().lstrip("0")] = eid
            if (r["ticker"] or "").strip():
                self._by_ticker[r["ticker"].strip().upper()] = eid
            if (r["lei"] or "").strip():
                self._by_lei[r["lei"].strip()] = eid
            n = normalize(r["name"])
            if n:
                self._by_name.setdefault(n, set()).add(eid)
                self._terms.setdefault(eid, set()).add(n)

        for r in conn.execute("SELECT entity_id, alias FROM entity_aliases"):
            a = normalize(r["alias"])
            if a:
                self._by_name.setdefault(a, set()).add(r["entity_id"])
                self._terms.setdefault(r["entity_id"], set()).add(a)

        for r in conn.execute("SELECT entity_id, term FROM entity_collision_terms"):
            t = normalize(r["term"])
            if t:
                self._collisions.setdefault(t, set()).add(r["entity_id"])

    # -- helpers ------------------------------------------------------------

    def _context_confirms(self, eid, used_term, context_norm):
        """A different positive term of the entity appearing in the context
        text corroborates a collision-term or ambiguous hit (R6.2/R6.3)."""
        if not context_norm:
            return None
        for term in self._terms.get(eid, ()):
            if term != used_term and term in context_norm:
                return term
        return None

    def _fuzzy(self, n):
        """Best fuzzy candidates: entity_id -> max ratio across its terms."""
        scores = {}
        for key in difflib.get_close_matches(
                n, self._by_name.keys(), n=8, cutoff=REVIEW_THRESHOLD):
            ratio = difflib.SequenceMatcher(None, n, key).ratio()
            for eid in self._by_name[key]:
                if ratio > scores.get(eid, 0.0):
                    scores[eid] = ratio
        return sorted(scores.items(), key=lambda kv: -kv[1])

    # -- public -------------------------------------------------------------

    def resolve(self, cik=None, ticker=None, lei=None, name=None,
                context_text=""):
        # R6.1 deterministic identifiers first
        if cik and str(cik).strip():
            eid = self._by_cik.get(str(cik).strip().lstrip("0"))
            if eid:
                return Resolution("matched", eid, "cik", 1.0,
                                  [f"cik:{cik}"], [], [(eid, 1.0)])
        if ticker and ticker.strip():
            eid = self._by_ticker.get(ticker.strip().upper())
            if eid:
                return Resolution("matched", eid, "ticker", 1.0,
                                  [f"ticker:{ticker}"], [], [(eid, 1.0)])
        if lei and lei.strip():
            eid = self._by_lei.get(lei.strip())
            if eid:
                return Resolution("matched", eid, "lei", 1.0,
                                  [f"lei:{lei}"], [], [(eid, 1.0)])

        if not name:
            return _NO_MATCH
        n = normalize(name)
        if not n:
            return _NO_MATCH
        context_norm = normalize(context_text) if context_text else ""

        # R6.3 collision guard: a bare collision term never auto-matches,
        # even when it equals an entity's full name.
        if n in self._collisions:
            cands = sorted(self._collisions[n])
            confirmed = []
            for eid in cands:
                confirm = self._context_confirms(eid, n, context_norm)
                if confirm:
                    confirmed.append((eid, confirm))
            if len(confirmed) == 1:
                eid, confirm = confirmed[0]
                return Resolution("matched", eid, "name+context", 0.95,
                                  [n, f"context:{confirm}"], [],
                                  [(eid, 0.95)])
            if len(confirmed) > 1:
                return Resolution(
                    "review", None, "ambiguous_context",
                    REVIEW_THRESHOLD,
                    [n] + [f"context:{confirm}" for _, confirm in confirmed],
                    [], [(eid, REVIEW_THRESHOLD) for eid, _ in confirmed])
            return Resolution("review", None, "collision_term",
                              REVIEW_THRESHOLD, [], [n],
                              [(eid, REVIEW_THRESHOLD) for eid in cands])

        # Curated exact name/alias
        ids = self._by_name.get(n)
        if ids:
            if len(ids) == 1:
                eid = next(iter(ids))
                return Resolution("matched", eid, "name", 1.0, [n], [],
                                  [(eid, 1.0)])
            confirmed = []
            for eid in sorted(ids):  # ambiguous exact: context may break tie
                confirm = self._context_confirms(eid, n, context_norm)
                if confirm:
                    confirmed.append((eid, confirm))
            if len(confirmed) == 1:
                eid, confirm = confirmed[0]
                return Resolution("matched", eid, "name+context", 0.95,
                                  [n, f"context:{confirm}"], [],
                                  [(eid, 0.95)])
            if len(confirmed) > 1:
                return Resolution(
                    "review", None, "ambiguous_context",
                    REVIEW_THRESHOLD,
                    [n] + [f"context:{confirm}" for _, confirm in confirmed],
                    [], [(eid, REVIEW_THRESHOLD) for eid, _ in confirmed])
            return Resolution("review", None, "ambiguous_name",
                              REVIEW_THRESHOLD, [n], [],
                              [(eid, REVIEW_THRESHOLD) for eid in sorted(ids)])

        # R6.2 fuzzy fallback
        scored = self._fuzzy(n)
        if not scored:
            return _NO_MATCH
        top_eid, top_ratio = scored[0]
        runner_up_strong = len(scored) > 1 and scored[1][1] >= MATCH_THRESHOLD
        if top_ratio >= MATCH_THRESHOLD and not runner_up_strong:
            return Resolution("matched", top_eid, "fuzzy", round(top_ratio, 4),
                              [n], [], [(top_eid, round(top_ratio, 4))])
        return Resolution("review", None, "fuzzy_below_threshold",
                          round(top_ratio, 4), [n], [],
                          [(e, round(r, 4)) for e, r in scored])


def record_decision(conn, raw_event_id, resolution, decision="auto",
                    decided_by="ingestion"):
    """R6.4: log a match decision with terms and parser version."""
    conn.execute(
        "INSERT INTO entity_match_decisions "
        "(raw_event_id, entity_id, method, confidence, matched_terms, "
        " rejected_terms, decision, decided_by, ts, parser_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (raw_event_id, resolution.entity_id, resolution.method,
         resolution.confidence, json.dumps(resolution.matched_terms),
         json.dumps(resolution.rejected_terms), decision, decided_by,
         datetime.now(timezone.utc).isoformat(), PARSER_VERSION))


def enqueue_review(conn, raw_event_id, resolution):
    """R6.2: below-threshold/ambiguous results go to review_queue,
    one row per candidate."""
    now = datetime.now(timezone.utc).isoformat()
    for eid, conf in resolution.candidates:
        conn.execute(
            "INSERT INTO review_queue "
            "(raw_event_id, candidate_entity_id, reason, confidence, "
            " created_at, disposition) VALUES (?, ?, ?, ?, ?, 'pending')",
            (raw_event_id, eid, resolution.method, conf, now))
