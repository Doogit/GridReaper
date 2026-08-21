"""Signal scoring engine (R7.3, R7.5).

score = base_strength * 0.5^(age_days / half_life) * account_fit * scope_fit
        [* combo_multiplier when a combo fires — R12]

base_strength and decay_half_life_days come from the signal's trigger row;
combo_multiplier is the product of the multipliers of the enabled combo_rules
that fire for the signal's entity (R12), or absent (score_combo NULL) when none
fire — see rescore(). All fit factors are operator
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

Score explainability (R8.1, R12): compute_score returns up to five multiplicative
components alongside the score and persists them to signals.score_base /
score_decay / score_account_fit / score_scope_fit / score_combo plus scored_at.
When no combo fires, score_combo is NULL and the four factors multiply to the
stored score exactly as before. When a combo fires, score_combo carries the
multiplier and all five factors multiply to the stored score. Only status='active' rows are
rescored; superseded/decayed/dismissed rows keep their frozen score and
components.

Score reproducibility (R3.7): rescore() also stamps signals.scoring_config_version
(0011) with scoring_config_version(conn) - a digest of the tuning in force. See
that function for why it hashes two tables, and 0011 for why the column is
nullable and never backfilled. Because rescore() only touches status='active'
rows, a decayed/dismissed/retracted signal keeps the token it had when it was
last active (or NULL if it was never scored under this build) - its frozen score
belongs to that frozen tuning, and re-stamping it would be a false claim.

CLI: python -m app.scoring - takes the single-writer ingestion lock (R3.2),
rescores data/gridsignals.db, prints a one-line summary.
"""
import sys
from collections import namedtuple
from datetime import date, datetime, timezone
from hashlib import sha256

from app.combos import ComboExprError
from app.combos import evaluate as _evaluate_combo
from app.db.connection import get_connection

DECAY_THRESHOLD = 1.0
NEUTRAL_WEIGHT = 1.0

# Length of the scoring-config token. 16 hex chars (64 bits) is the same digest
# width the UI's id helpers use and is collision-safe for a tuning history.
CONFIG_VERSION_LEN = 16

# The multiplicative factors behind a score (R8.1, R12 explainability):
# score == base * decay * account_fit * scope_fit [* score_combo when not NULL].
# score_combo carries the combo multiplier (product of all firing rules) or None
# when no combo was in force; a NULL score_combo means the four factors multiply
# to the stored score exactly.
Score = namedtuple("Score", "score base decay account_fit scope_fit score_combo")


def load_weights(conn):
    """scoring_weights -> {(weight_kind, key): weight} lookup dict."""
    return {(r["weight_kind"], r["key"]): r["weight"] for r in conn.execute(
        "SELECT weight_kind, key, weight FROM scoring_weights")}


def _num(value):
    """Canonical text for a numeric tuning value, so an INTEGER 4 and a REAL
    4.0 hash identically (SQLite affinity lets either land in these columns)."""
    if value is None:
        return ""
    try:
        return repr(float(value))
    except (TypeError, ValueError):
        return str(value)


def scoring_config_version(conn):
    """Digest of the operator tuning currently in force (R3.7).

    Covers all three tables rescore() reads for tuning:
      * scoring_weights(weight_kind, key, weight) - account_fit and scope_fit
      * triggers(trigger_id, base_strength, decay_half_life_days) - the base
        strength and the decay divisor
      * combo_rules(rule_id, logic_expr, multiplier, enabled_stage) - the combo
        multiplier layer added in R12/0014. All rows (enabled and disabled) are
        covered so the token moves when a rule is toggled or its multiplier
        changes. An empty combo_rules table hashes to the same prefix string it
        always did, keeping scores byte-identical before any rules exist.

    Rows are sorted by primary key and numbers canonicalised, so the token is a
    pure function of the tuning - same tuning, same token, on any machine.
    """
    lines = []
    for r in conn.execute(
            "SELECT weight_kind, key, weight FROM scoring_weights "
            "ORDER BY weight_kind, key"):
        lines.append(f"weight|{r['weight_kind']}|{r['key']}|{_num(r['weight'])}")
    for r in conn.execute(
            "SELECT trigger_id, base_strength, decay_half_life_days "
            "FROM triggers ORDER BY trigger_id"):
        lines.append(f"trigger|{r['trigger_id']}|{_num(r['base_strength'])}"
                     f"|{_num(r['decay_half_life_days'])}")
    for r in conn.execute(
            "SELECT rule_id, logic_expr, multiplier, enabled_stage "
            "FROM combo_rules ORDER BY rule_id"):
        lines.append(f"combo|{r['rule_id']}|{r['logic_expr'] or ''}"
                     f"|{_num(r['multiplier'])}|{_num(r['enabled_stage'])}")
    return sha256("\n".join(lines).encode("utf-8")).hexdigest()[:CONFIG_VERSION_LEN]


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


def compute_score(weights, row, now, combo_multiplier=None):
    """Score one signal row (joined with its trigger and entity columns).

    Returns a Score namedtuple. When combo_multiplier is None (no combo fired),
    score_combo is None and the four factors (base, decay, account_fit,
    scope_fit) multiply exactly to the stored score. When combo_multiplier is
    provided, score_combo carries the multiplier and all five factors multiply
    to the stored score.
    """
    age = _age_days(row["event_date"], now)
    half_life = row["decay_half_life_days"] or 1
    decay = 0.5 ** (age / half_life)
    fit = account_fit(weights, row["signal_scope"], row["entity_id"],
                      row["subsector"], row["richness"], row["coverage_flag"])
    scope_fit = _weight(weights, "scope", row["signal_scope"])
    base = row["base_strength"]
    score = base * decay * fit * scope_fit
    if combo_multiplier is not None:
        score *= combo_multiplier
    return Score(score, base, decay, fit, scope_fit, combo_multiplier)


def rescore(conn, now=None):
    """Recompute score for every status='active' signal; flip to 'decayed'
    below DECAY_THRESHOLD. Dismissed/decayed rows are untouched. Stamps
    scoring_config_version alongside the components so each stored score names
    the tuning that produced it (R3.7, R12). Returns {'scored': n, 'decayed': n}.

    Combo evaluation (R12): enabled combo_rules are evaluated once per
    entity_id and cached for the pass. The combo multiplier is the product of
    the multipliers of all rules that fire; score_combo is NULL when no rule
    fires, entity_id is NULL, or no enabled rules exist — in which case the
    score is byte-identical to a pre-combo rescore.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    scored_at = now.astimezone(timezone.utc).isoformat() if now.tzinfo \
        else now.replace(tzinfo=timezone.utc).isoformat()
    config_version = scoring_config_version(conn)
    weights = load_weights(conn)
    # Load enabled combo rules once per rescore pass.
    enabled_rules = conn.execute(
        "SELECT rule_id, logic_expr, multiplier FROM combo_rules "
        "WHERE enabled_stage IS NOT NULL").fetchall()
    rows = conn.execute(
        "SELECT s.signal_id, s.signal_scope, s.entity_id, s.event_date, "
        " t.base_strength, t.decay_half_life_days, "
        " e.subsector, e.richness, e.coverage_flag "
        "FROM signals s "
        "JOIN triggers t ON t.trigger_id = s.trigger_id "
        "LEFT JOIN watchlist_entities e ON e.entity_id = s.entity_id "
        "WHERE s.status = 'active'").fetchall()
    # Cache combo evaluation per entity_id: many signals share an entity.
    combo_cache = {}  # entity_id -> Optional[float]
    scored = decayed = 0
    for row in rows:
        entity_id = row["entity_id"]
        # Determine combo multiplier for this signal's entity.
        if entity_id is not None and enabled_rules:
            if entity_id not in combo_cache:
                product = 1.0
                any_fired = False
                for rule in enabled_rules:
                    try:
                        if _evaluate_combo(conn, entity_id, rule["logic_expr"]):
                            product *= float(rule["multiplier"])
                            any_fired = True
                    except ComboExprError:
                        pass
                combo_cache[entity_id] = product if any_fired else None
            combo_multiplier = combo_cache[entity_id]
        else:
            combo_multiplier = None
        sc = compute_score(weights, row, now, combo_multiplier=combo_multiplier)
        status = "decayed" if sc.score < DECAY_THRESHOLD else "active"
        conn.execute(
            "UPDATE signals SET score = ?, status = ?, score_base = ?, "
            " score_decay = ?, score_account_fit = ?, score_scope_fit = ?, "
            " score_combo = ?, scored_at = ?, scoring_config_version = ? "
            "WHERE signal_id = ?",
            (sc.score, status, sc.base, sc.decay, sc.account_fit,
             sc.scope_fit, sc.score_combo, scored_at, config_version,
             row["signal_id"]))
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
