"""Signal scoring engine (R7.3, R7.5).

score = base_strength * 0.5^(age_days / half_life) * account_fit * scope_fit

base_strength and decay_half_life_days come from the signal's trigger row;
combo_multiplier is fixed at 1.0 for the MVP. All fit factors are operator
tunables in scoring_weights (seeded from seeds/scoring_weights.csv, Admin
sliders later per R8.7); any unknown/missing key falls back to a neutral 1.0
so scoring never KeyErrors on new subsectors or blank entity fields.

account_fit (R7.5):
  * entity-scoped signals: subsector_w * richness_w * coverage_w of the
    signal's entity (coverage 'dark' is discounted, never zeroed - R6.6).
    A regulatory_calendar-scoped signal that carries an entity uses the
    applicability weight (keyed by the entity's subsector, then 'default')
    INSTEAD of richness.
  * entity-less signals: sector scope is neutral (1.0) - there is no account
    to fit; regulatory_calendar uses applicability['default'], expressing how
    directly the rule applies absent any account evidence.

scope_fit comes from scoring_weights rows (weight_kind='scope'): account 1.0,
sector 0.55, regulatory_calendar 0.45 - seeded so broad cards can never
outrank a strong account card (max sector score 5*0.55=2.75 < a fresh
account card at base 4 with decent fit).

Decay: rescore() flips status active -> 'decayed' when score drops below
DECAY_THRESHOLD = 1.0. Rationale: MVP base strengths are 4-5, so a
perfect-fit leadership signal (base 4, 90d half-life) decays just past 2
half-lives (~180 days - a CISO's stack review window is long over), while a
sector CIP revision (5 * 0.55, 600d half-life) lives ~2.4 years. Decayed and
dismissed rows are never touched again (decayed stays decayed at MVP; a
re-activation story is post-MVP), so their last score is frozen.

age_days runs from signals.event_date (date-only strings are the norm; a
full ISO timestamp's date part is used; empty/unparseable dates score at
age 0 rather than silently decaying) to an injectable clock:
rescore(conn, now=None) defaults to current UTC; tests inject a fixed now
for determinism - same DB + same now -> identical scores.

Score explainability (R8.1): compute_score returns the four multiplicative
components (base_strength, decay, account_fit, scope_fit) alongside the score,
and rescore() persists them to signals.score_base / score_decay /
score_account_fit / score_scope_fit plus scored_at (the injected-clock time,
UTC ISO-8601). The components always multiply to the stored score, so the card
can render "2.34 = 5 x 0.85 x 1.00 x 0.55". Only status='active' rows are
rescored; superseded/decayed/dismissed rows keep their frozen score and
components.

CLI: python -m app.scoring - takes the single-writer ingestion lock (R3.2),
rescores data/gridsignals.db, prints a one-line summary.
"""
import sys
from collections import namedtuple
from datetime import date, datetime, timezone

from app.db.connection import get_connection

DECAY_THRESHOLD = 1.0
NEUTRAL_WEIGHT = 1.0

# The four multiplicative factors behind a score (R8.1 explainability):
# score == base * decay * account_fit * scope_fit.
Score = namedtuple("Score", "score base decay account_fit scope_fit")


def load_weights(conn):
    """scoring_weights -> {(weight_kind, key): weight} lookup dict."""
    return {(r["weight_kind"], r["key"]): r["weight"] for r in conn.execute(
        "SELECT weight_kind, key, weight FROM scoring_weights")}


def _weight(weights, kind, key):
    if key is None or key == "":
        return NEUTRAL_WEIGHT
    return weights.get((kind, key), NEUTRAL_WEIGHT)


def _age_days(event_date, now):
    """Whole days from event_date (ISO date or timestamp string) to now.
    Unparseable/empty dates and future dates clamp to 0 (full strength)."""
    try:
        event = date.fromisoformat((event_date or "")[:10])
    except ValueError:
        return 0
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    return max((now.date() - event).days, 0)


def account_fit(weights, scope, entity_id, subsector, richness, coverage_flag):
    """R7.5 account fit. See module docstring for the entity-less cases."""
    if entity_id is None:
        if scope == "regulatory_calendar":
            return _weight(weights, "applicability", "default")
        return NEUTRAL_WEIGHT
    if scope == "regulatory_calendar":
        mid = weights.get(("applicability", subsector or ""),
                          _weight(weights, "applicability", "default"))
    else:
        mid = _weight(weights, "richness", richness)
    return (_weight(weights, "subsector", subsector)
            * mid
            * _weight(weights, "coverage", coverage_flag))


def compute_score(weights, row, now):
    """Score one signal row (joined with its trigger and entity columns).
    Returns a Score namedtuple carrying the score and its four components
    (base, decay, account_fit, scope_fit) - they multiply to the score."""
    age = _age_days(row["event_date"], now)
    half_life = row["decay_half_life_days"] or 1
    decay = 0.5 ** (age / half_life)
    fit = account_fit(weights, row["signal_scope"], row["entity_id"],
                      row["subsector"], row["richness"], row["coverage_flag"])
    scope_fit = _weight(weights, "scope", row["signal_scope"])
    base = row["base_strength"]
    return Score(base * decay * fit * scope_fit, base, decay, fit, scope_fit)


def rescore(conn, now=None):
    """Recompute score for every status='active' signal; flip to 'decayed'
    below DECAY_THRESHOLD. Dismissed/decayed rows are untouched. Returns
    {'scored': n, 'decayed': n}."""
    if now is None:
        now = datetime.now(timezone.utc)
    scored_at = now.astimezone(timezone.utc).isoformat() if now.tzinfo \
        else now.replace(tzinfo=timezone.utc).isoformat()
    weights = load_weights(conn)
    rows = conn.execute(
        "SELECT s.signal_id, s.signal_scope, s.entity_id, s.event_date, "
        " t.base_strength, t.decay_half_life_days, "
        " e.subsector, e.richness, e.coverage_flag "
        "FROM signals s "
        "JOIN triggers t ON t.trigger_id = s.trigger_id "
        "LEFT JOIN watchlist_entities e ON e.entity_id = s.entity_id "
        "WHERE s.status = 'active'").fetchall()
    scored = decayed = 0
    for row in rows:
        sc = compute_score(weights, row, now)
        status = "decayed" if sc.score < DECAY_THRESHOLD else "active"
        conn.execute(
            "UPDATE signals SET score = ?, status = ?, score_base = ?, "
            " score_decay = ?, score_account_fit = ?, score_scope_fit = ?, "
            " scored_at = ? WHERE signal_id = ?",
            (sc.score, status, sc.base, sc.decay, sc.account_fit,
             sc.scope_fit, scored_at, row["signal_id"]))
        scored += 1
        if status == "decayed":
            decayed += 1
    conn.commit()
    return {"scored": scored, "decayed": decayed}


def main():
    from app.ingest.runner import ingest_lock
    with ingest_lock():
        conn = get_connection()
        try:
            summary = rescore(conn)
        finally:
            conn.close()
    print(f"scoring: success scored={summary['scored']} "
          f"decayed={summary['decayed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
