"""Read-only data-access layer for the GridSignals UI (R8.1-R8.3).

Plain stdlib functions the Streamlit pages share, so the pages stay thin and
every query is covered by a hermetic test. The app is a reader: the only
writers here are ``record_feedback`` (R9.1), ``triage_decision`` (R8.2 human
match decisions + review-queue disposition), and nothing else. Cards read
``license_play_snapshots``, never live ``license_facts`` (R7.6); fact rows are
surfaced only as provenance chips and this layer never returns a fact's
``price_note`` so a non-primary price can never reach the DOM (R4.3/R7.11).

Timestamps are UTC ISO-8601 (R10.2); functions that reason about age accept an
injectable ``now`` so pages and tests are deterministic.
"""
import contextlib
import json
import math
import sqlite3
from datetime import date, datetime, timezone

from app.db.connection import get_connection
from app.scoring import rescore

# Signal-scope groupings for the scope-separated feed (R7.2): account cards are
# rendered first, then a labeled divider, then sector/regulatory cards.
SCOPE_GROUPS = {
    "account": ("account", "parent"),
    "sector": ("sector", "subsector", "regulatory_calendar"),
}

# Feedback verdicts (R9.1) and reason codes (R9.2, verbatim). A not_useful
# verdict MUST carry a reason_code.
VERDICTS = ("useful", "not_useful", "converted")
REASON_CODES = (
    "wrong_entity", "duplicate", "stale_event", "weak_trigger", "weak_evidence",
    "bad_product_mapping", "bad_license_play", "not_my_account",
    "already_known", "other",
)

TRIAGE_PARSER_VERSION = "ui-triage/1.0"

# Columns every card needs, identical shape from feed_page and account_signals
# so the one card component (B1) renders both. Score components are the 0005
# nullable columns (NULL until app.scoring.rescore runs under this build).
_SIGNAL_COLUMNS = """
  s.signal_id, s.raw_event_id, s.entity_id, s.signal_scope, s.trigger_id,
  s.event_date, s.headline, s.evidence_snippet, s.source_url, s.confidence,
  s.evidence_quality, s.incident_evidence_level, s.customer_facing_allowed,
  s.score, s.status,
  s.score_base, s.score_decay, s.score_account_fit, s.score_scope_fit,
  s.scored_at,
  t.name AS trigger_name, t.base_strength, t.decay_half_life_days,
  e.name AS entity_name, e.subsector, e.richness, e.coverage_flag,
  e.gov_cloud_likelihood, e.tenant_cloud_environment
"""
_SIGNAL_FROM = (
    " FROM signals s"
    " JOIN triggers t ON t.trigger_id = s.trigger_id"
    " LEFT JOIN watchlist_entities e ON e.entity_id = s.entity_id ")


# -- small helpers -----------------------------------------------------------

def _utcnow_iso(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _parse_dt(value):
    """Parse a UTC ISO-8601 timestamp; None on empty/garbage."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_date(value):
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def _placeholders(n):
    return ",".join("?" * n)


# -- feed --------------------------------------------------------------------

def feed_page(conn, scope_group="account", after_key=None, limit=25,
              statuses=("active",)):
    """One keyset page of the signal feed for a scope group, newest first.

    Ordering is ``(event_date, signal_id)`` descending (R8.1). ``after_key`` is
    the ``(event_date, signal_id)`` of the last row on the previous page; pass
    None for the first page. ``statuses`` filters status (feed default: active
    only; decayed/superseded/dismissed reachable by widening it). Returns up to
    ``limit`` rows; the caller uses the last row's key as the next after_key.
    """
    if scope_group not in SCOPE_GROUPS:
        raise ValueError(f"unknown scope_group {scope_group!r}")
    scopes = SCOPE_GROUPS[scope_group]
    statuses = tuple(statuses)
    where = [f"s.signal_scope IN ({_placeholders(len(scopes))})",
             f"s.status IN ({_placeholders(len(statuses))})"]
    params = list(scopes) + list(statuses)
    if after_key is not None:
        ev, sid = after_key
        where.append("(s.event_date < ? OR (s.event_date = ? AND s.signal_id < ?))")
        params += [ev, ev, sid]
    sql = (f"SELECT {_SIGNAL_COLUMNS} {_SIGNAL_FROM} WHERE " + " AND ".join(where)
           + " ORDER BY s.event_date DESC, s.signal_id DESC LIMIT ?")
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def signal_detail(conn, signal_id):
    """Full detail for one signal: the card row, its evidence rows, and its
    license-play snapshots with per-play fact provenance. Returns None if the
    signal is unknown. Snapshot text is read verbatim from
    license_play_snapshots (R7.6); facts carry no price_note by construction.
    """
    signal = conn.execute(
        f"SELECT {_SIGNAL_COLUMNS} {_SIGNAL_FROM} WHERE s.signal_id = ?",
        (signal_id,)).fetchone()
    if signal is None:
        return None
    evidence = conn.execute(
        "SELECT raw_event_id, evidence_text, evidence_locator, evidence_rank, "
        " extraction_version FROM signal_evidence WHERE signal_id = ? "
        "ORDER BY evidence_rank, rowid", (signal_id,)).fetchall()
    snap_rows = conn.execute(
        "SELECT sn.play_id, sn.fact_ids, sn.generated_at, sn.generation_version, "
        " sn.display_text, sn.outreach_safe_text, c.product_id, "
        " c.recommended_path, c.discovery_question, p.name AS product_name "
        "FROM license_play_snapshots sn "
        "JOIN license_play_candidates c ON c.play_id = sn.play_id "
        "LEFT JOIN products p ON p.product_id = c.product_id "
        "WHERE sn.signal_id = ? ORDER BY sn.play_id", (signal_id,)).fetchall()
    snapshots = []
    for sn in snap_rows:
        try:
            fact_ids = json.loads(sn["fact_ids"] or "[]")
        except ValueError:
            fact_ids = []
        facts = []
        if fact_ids:
            # No price_note in this projection - a non-primary price must never
            # reach the card DOM (R4.3). Chips show source_quality + segment.
            facts = conn.execute(
                "SELECT fact_id, product_id, segment, source_quality, source_url "
                f"FROM license_facts WHERE fact_id IN ({_placeholders(len(fact_ids))}) "
                "ORDER BY fact_id", fact_ids).fetchall()
        snapshots.append({
            "play_id": sn["play_id"],
            "product_id": sn["product_id"],
            "product_name": sn["product_name"] or sn["product_id"],
            "recommended_path": sn["recommended_path"],
            "discovery_question": sn["discovery_question"],
            "display_text": sn["display_text"],
            "outreach_safe_text": sn["outreach_safe_text"],
            "generated_at": sn["generated_at"],
            "generation_version": sn["generation_version"],
            "facts": facts,
        })
    return {"signal": signal, "evidence": evidence, "snapshots": snapshots}


def account_signals(conn, entity_id, statuses=("active",)):
    """All signals attributed to one entity, newest first (Account 360 tabs).
    Same row shape as feed_page so the card component is reused."""
    statuses = tuple(statuses)
    sql = (f"SELECT {_SIGNAL_COLUMNS} {_SIGNAL_FROM} "
           f"WHERE s.entity_id = ? AND s.status IN ({_placeholders(len(statuses))}) "
           "ORDER BY s.event_date DESC, s.signal_id DESC")
    return conn.execute(sql, [entity_id] + list(statuses)).fetchall()


# -- review queue / triage ---------------------------------------------------

def review_pending(conn):
    """Pending review-queue candidates (R8.2) joined to the candidate entity
    and the underlying raw event. review_queue stores the resolver ``reason``
    (its method, e.g. collision_term / fuzzy_below_threshold) and a confidence
    - matched/rejected term trails are not persisted for review candidates, so
    the page shows the reason, not a fabricated term list. ``snippet`` is the
    raw event's title/headline when the payload is JSON, else a truncation."""
    rows = conn.execute(
        "SELECT rq.rowid AS rowid, rq.raw_event_id, rq.candidate_entity_id, "
        " rq.reason, rq.confidence, rq.created_at, "
        " e.name AS candidate_name, e.subsector, "
        " re.url AS source_url, re.payload AS payload, re.event_date "
        "FROM review_queue rq "
        "LEFT JOIN watchlist_entities e ON e.entity_id = rq.candidate_entity_id "
        "LEFT JOIN raw_events re ON re.raw_event_id = rq.raw_event_id "
        "WHERE rq.disposition = 'pending' "
        "ORDER BY rq.created_at DESC, rq.rowid DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["snippet"] = _payload_snippet(r["payload"])
        d.pop("payload", None)
        out.append(d)
    return out


def _payload_snippet(payload, limit=200):
    if not payload:
        return ""
    try:
        obj = json.loads(payload)
        for key in ("title", "headline", "name", "summary"):
            if isinstance(obj, dict) and (obj.get(key) or "").strip():
                return obj[key].strip()[:limit]
    except (ValueError, TypeError):
        pass
    return str(payload)[:limit]


def source_health(conn):
    """Every source policy with its most recent run and most recent successful
    run (R8.2/R10.3 operator surface). Callers pass rows to ``source_state`` to
    label them error / never-run / stale / disabled / ok."""
    return conn.execute(
        "SELECT sp.source_id, sp.name, sp.enabled, sp.ttl, sp.access_method, "
        " sp.evidence_rank, "
        " (SELECT r.status FROM source_runs r WHERE r.source_id = sp.source_id "
        "   ORDER BY r.started_at DESC, r.run_id DESC LIMIT 1) AS last_status, "
        " (SELECT r.error_state FROM source_runs r WHERE r.source_id = sp.source_id "
        "   ORDER BY r.started_at DESC, r.run_id DESC LIMIT 1) AS last_error_state, "
        " (SELECT r.finished_at FROM source_runs r WHERE r.source_id = sp.source_id "
        "   ORDER BY r.started_at DESC, r.run_id DESC LIMIT 1) AS last_finished_at, "
        " (SELECT r.started_at FROM source_runs r WHERE r.source_id = sp.source_id "
        "   ORDER BY r.started_at DESC, r.run_id DESC LIMIT 1) AS last_started_at, "
        " (SELECT r.finished_at FROM source_runs r WHERE r.source_id = sp.source_id "
        "   AND r.status = 'success' ORDER BY r.started_at DESC, r.run_id DESC "
        "   LIMIT 1) AS last_success_at "
        "FROM source_policies sp ORDER BY sp.enabled DESC, sp.source_id").fetchall()


def source_state(row, now=None):
    """Classify a source_health row: disabled | error | never_run | stale | ok.
    'stale' means the last success is older than the policy ttl (seconds)."""
    if not row["enabled"]:
        return "disabled"
    if row["last_status"] is None:
        return "never_run"
    if row["last_status"] != "success":
        return "error"
    now = now or datetime.now(timezone.utc)
    ttl = row["ttl"]
    success = _parse_dt(row["last_success_at"])
    if ttl and success is not None:
        age = (now - success).total_seconds()
        if age > ttl:
            return "stale"
    return "ok"


def stale_facts(conn, days=180, now=None):
    """License facts whose verified_date is older than ``days`` (R10.7, default
    180). Facts with an empty/unparseable verified_date are included as stale
    (unknown verification cannot be proven fresh); their age_days is None.
    Returns rows ordered oldest-first, each with product_name and age_days."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date()
    rows = conn.execute(
        "SELECT lf.fact_id, lf.product_id, lf.sku_or_plan, lf.segment, "
        " lf.source_quality, lf.source_url, lf.verified_date, "
        " p.name AS product_name "
        "FROM license_facts lf "
        "LEFT JOIN products p ON p.product_id = lf.product_id").fetchall()
    stale = []
    for r in rows:
        vd = _parse_date(r["verified_date"])
        if vd is None:
            age = None
        else:
            age = (today - vd).days
            if age <= days:
                continue
        d = dict(r)
        d["age_days"] = age
        stale.append(d)
    # oldest first; unknown-verified (age None) sort last
    stale.sort(key=lambda d: (d["age_days"] is None, -(d["age_days"] or 0)))
    return stale


# -- feedback / precision reads (R8.6, R9.3) ---------------------------------
#
# These feed app.audit.precision, whose PURE functions read rows with row.get()
# — so every helper here returns plain dicts (sqlite3.Row has no .get()). The
# dimension keys precision slices by (trigger_id, source_id, signal_scope,
# incident_evidence_level, entity_id) are resolved once, here, by the joins:
#   * source_id   comes from the signal's raw_event  (signals.raw_event_id ->
#                 raw_events.source_id); NULL for sector/regulatory signals with
#                 no raw_event.
#   * trigger_name comes from triggers.name (signals.trigger_id -> triggers).
# entity_id is carried through verbatim (None for sector-scope signals) so
# precision's account-vs-sector partition (R9.4) works off the real value.

def precision_feedback_rows(conn):
    """Feedback rows joined to their signal's dimensions (R8.6, R9.3).

    One dict per feedback row, carrying the keys precision.py consumes:
    ``signal_id, verdict, reason_code, ts, trigger_id, trigger_name, source_id,
    signal_scope, incident_evidence_level, entity_id``. source_id resolves via
    ``signals.raw_event_id -> raw_events.source_id`` (LEFT JOIN: None when the
    signal has no raw event); trigger_name via ``triggers.name``.
    """
    rows = conn.execute(
        "SELECT f.signal_id, f.verdict, f.reason_code, f.ts, "
        " s.trigger_id, t.name AS trigger_name, re.source_id AS source_id, "
        " s.signal_scope, s.incident_evidence_level, s.entity_id "
        "FROM feedback f "
        "JOIN signals s ON s.signal_id = f.signal_id "
        "LEFT JOIN triggers t ON t.trigger_id = s.trigger_id "
        "LEFT JOIN raw_events re ON re.raw_event_id = s.raw_event_id "
        "ORDER BY f.ts, f.rowid").fetchall()
    return [dict(r) for r in rows]


def precision_audit_rows(conn):
    """Audit verdicts joined to their signal's dimensions (R8.6, R9.3, R9.4).

    One dict per audit verdict, carrying: ``signal_id, check_type, result,
    model_id, prompt_version, ts, trigger_id, trigger_name, source_id,
    signal_scope, incident_evidence_level, entity_id``. Same joins as
    ``precision_feedback_rows`` resolve source_id / trigger_name / entity_id.
    """
    rows = conn.execute(
        "SELECT a.signal_id, a.check_type, a.result, a.model_id, "
        " a.prompt_version, a.ts, s.trigger_id, t.name AS trigger_name, "
        " re.source_id AS source_id, s.signal_scope, s.incident_evidence_level, "
        " s.entity_id "
        "FROM audit a "
        "JOIN signals s ON s.signal_id = a.signal_id "
        "LEFT JOIN triggers t ON t.trigger_id = s.trigger_id "
        "LEFT JOIN raw_events re ON re.raw_event_id = s.raw_event_id "
        "ORDER BY a.ts, a.rowid").fetchall()
    return [dict(r) for r in rows]


def precision_halflife_rows(conn):
    """One row per signal for the half-life effectiveness view (R8.6, R9.3).

    Carries ``signal_id, trigger_id, trigger_name, decay_half_life_days, score,
    score_decay, status, event_date, verdict``. ``verdict`` comes from a LEFT
    JOIN to feedback and is None when the signal is unrated. A signal may have
    several feedback rows; we pick the LATEST by ts (ties broken by feedback
    rowid) via a correlated subquery, so a signal contributes exactly one row
    and half_life_effectiveness's rated/useful counts are per-signal, not
    per-feedback-row.
    """
    rows = conn.execute(
        "SELECT s.signal_id, s.trigger_id, t.name AS trigger_name, "
        " t.decay_half_life_days, s.score, s.score_decay, s.status, "
        " s.event_date, "
        " (SELECT f.verdict FROM feedback f WHERE f.signal_id = s.signal_id "
        "   ORDER BY f.ts DESC, f.rowid DESC LIMIT 1) AS verdict "
        "FROM signals s "
        "LEFT JOIN triggers t ON t.trigger_id = s.trigger_id "
        "ORDER BY s.trigger_id, s.signal_id").fetchall()
    return [dict(r) for r in rows]


def audit_run_rows(conn):
    """audit_runs rows newest-first for the run-history panel (R8.6, R9.12).

    Surfaces the R9.12 skip/budget transparency (status, verdicts_written,
    budget_spent, skipped_reason, error_state) so an operator sees why a run
    wrote nothing rather than a silent gap. Returns plain dicts."""
    rows = conn.execute(
        "SELECT run_id, started_at, finished_at, model_id, prompt_version, "
        " parser_version, signals_sampled, verdicts_written, budget_spent, "
        " status, error_state, skipped_reason "
        "FROM audit_runs ORDER BY started_at DESC, run_id DESC").fetchall()
    return [dict(r) for r in rows]


# -- account 360 -------------------------------------------------------------

def account_header(conn, entity_id):
    """Account 360 header (R8.3): the entity row, its parent and children from
    entity_relationships, and its signal counts by status. Returns None for an
    unknown entity."""
    entity = conn.execute(
        "SELECT * FROM watchlist_entities WHERE entity_id = ?",
        (entity_id,)).fetchone()
    if entity is None:
        return None
    parent = conn.execute(
        "SELECT e.entity_id, e.name FROM entity_relationships r "
        "JOIN watchlist_entities e ON e.entity_id = r.parent_entity_id "
        "WHERE r.child_entity_id = ? ORDER BY e.entity_id LIMIT 1",
        (entity_id,)).fetchone()
    if parent is None and (entity["parent_id"] or "").strip():
        parent = conn.execute(
            "SELECT entity_id, name FROM watchlist_entities WHERE entity_id = ?",
            (entity["parent_id"].strip(),)).fetchone()
    children = conn.execute(
        "SELECT DISTINCT e.entity_id, e.name FROM entity_relationships r "
        "JOIN watchlist_entities e ON e.entity_id = r.child_entity_id "
        "WHERE r.parent_entity_id = ? ORDER BY e.name", (entity_id,)).fetchall()
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM signals WHERE entity_id = ? "
        "GROUP BY status", (entity_id,))}
    return {
        "entity": entity,
        "parent": parent,
        "children": children,
        "signal_counts": counts,
        "total_signals": sum(counts.values()),
    }


def badge_legend(conn):
    """badge_legend table -> {badge_kind: {code: {'label','description'}}}.
    The UI renders badge labels from this, never hardcoding a code's meaning."""
    out = {}
    for r in conn.execute(
            "SELECT badge_kind, code, label, description FROM badge_legend"):
        out.setdefault(r["badge_kind"], {})[r["code"]] = {
            "label": r["label"], "description": r["description"]}
    return out


# -- writes (the only two) ---------------------------------------------------

def record_feedback(conn, signal_id, verdict, reason_code=None, note="",
                    now=None):
    """Insert a feedback row (R9.1). A not_useful verdict MUST carry a
    reason_code from R9.2; any provided reason_code must be a known code.
    Raises ValueError on invalid input (the page surfaces it, never writes)."""
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")
    if verdict == "not_useful" and not reason_code:
        raise ValueError("reason_code is required when verdict is not_useful (R9.1)")
    if reason_code and reason_code not in REASON_CODES:
        raise ValueError(f"unknown reason_code {reason_code!r}")
    conn.execute(
        "INSERT INTO feedback (signal_id, verdict, reason_code, ts, note) "
        "VALUES (?, ?, ?, ?, ?)",
        (signal_id, verdict, reason_code or None, _utcnow_iso(now), note or ""))
    conn.commit()


def triage_decision(conn, raw_event_id, entity_id, accept, now=None):
    """Record a human review decision (R8.2): log an entity_match_decisions row
    (decided_by='human') and update the matching review_queue row's disposition
    - both in one transaction. Accepting records the decision only; creating a
    signal from an accepted match is deferred (documented on the page)."""
    ts = _utcnow_iso(now)
    decision = "reviewed" if accept else "rejected"
    disposition = "accepted" if accept else "rejected"
    conn.execute(
        "INSERT INTO entity_match_decisions (raw_event_id, entity_id, method, "
        " confidence, matched_terms, rejected_terms, decision, decided_by, ts, "
        " parser_version) VALUES (?, ?, 'human_review', 1.0, '[]', '[]', ?, "
        " 'human', ?, ?)",
        (raw_event_id, entity_id, decision, ts, TRIAGE_PARSER_VERSION))
    conn.execute(
        "UPDATE review_queue SET disposition = ?, disposed_at = ? "
        "WHERE raw_event_id = ? AND candidate_entity_id = ?",
        (disposition, ts, raw_event_id, entity_id))
    conn.commit()


# -- config writes (R8.7 Admin/Config) ---------------------------------------
#
# The first Streamlit writes into *seeded* config tables. Three rules hold every
# one (see the plan's KTDs):
#   * Provenance (R3.3): each edit writes a config_audit row (field, old->new,
#     editor, reason, ts) in the SAME transaction as the edit. Append-only:
#     these helpers only UPDATE the target row and INSERT audit - never DELETE,
#     so nothing FK-referencing a config row can be orphaned.
#   * Reload-safe (Pattern B): weight / decay_half_life_days are frozen against
#     seed reload by the loader's update_cols, so a direct UPDATE here survives
#     the next load_seeds run. Reads (scoring.load_weights, rescore) are unchanged.
#   * Single-writer (R3.2): the page wraps each save in config_write_conn(),
#     which takes the ingestion lock and hands back a fresh short-lived
#     connection. A weight/half-life edit re-runs rescore() ACTIVE-ONLY (R8.1):
#     decayed/dismissed/superseded rows keep their frozen score and components.
CONFIG_EDITOR = "operator"          # single-operator MVP: no auth, no PII (R10.6)


@contextlib.contextmanager
def config_write_conn(db_path=None, lock_path=None):
    """Fresh connection holding the single-writer ingestion lock (R3.2) for one
    Admin save. Raises RuntimeError if an ingestion/scoring run holds the lock
    (the page catches it -> "ingestion in progress"). Acquire this INSIDE the
    save handler, never at page render (Streamlit reruns top-to-bottom)."""
    from app.ingest.runner import ingest_lock, LOCK_PATH
    with ingest_lock(lock_path or LOCK_PATH):
        conn = get_connection(db_path) if db_path else get_connection()
        try:
            yield conn
        finally:
            conn.close()


def _config_str(value):
    """Store config_audit old/new as TEXT; numbers become their str form."""
    return None if value is None else str(value)


def _record_config_edit(conn, table_name, pk, field, old_value, new_value,
                        editor, reason, now):
    """Append one config_audit provenance row (R3.3). Caller does the matching
    UPDATE in the same transaction and commits."""
    conn.execute(
        "INSERT INTO config_audit (table_name, pk, field, old_value, new_value, "
        " editor, reason, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (table_name, pk, field, _config_str(old_value), _config_str(new_value),
         editor, reason or "", _utcnow_iso(now)))


def update_weight(conn, weight_kind, key, new_weight, reason="",
                  editor=CONFIG_EDITOR, now=None):
    """Set a scoring_weights.weight (R7.5 tunable), audit it, and rescore active
    signals so live cards reflect it. Validates a finite weight >= 0 and that the
    (weight_kind, key) row exists (no key insert/delete here - value edits only,
    so scoring.py's neutral-1.0 fallback can't be tripped by a removed key).
    A no-op edit (new == old) writes nothing and does not rescore. Raises
    ValueError (the page surfaces it, never writes). Returns
    {old, new, changed[, scored, decayed]} (rescore stats only when changed)."""
    try:
        w = float(new_weight)
    except (TypeError, ValueError):
        raise ValueError(f"weight must be a number, got {new_weight!r}")
    if not math.isfinite(w) or w < 0:
        raise ValueError(f"weight must be a finite value >= 0, got {w}")
    row = conn.execute(
        "SELECT weight FROM scoring_weights WHERE weight_kind = ? AND key = ?",
        (weight_kind, key)).fetchone()
    if row is None:
        raise ValueError(f"unknown scoring weight {(weight_kind, key)!r}")
    old = row["weight"]
    if w == old:
        # A no-op save writes no provenance row and skips the rescore, so the
        # audit trail records real changes, not button presses (persona pass).
        return {"old": old, "new": w, "changed": False}
    conn.execute(
        "UPDATE scoring_weights SET weight = ? WHERE weight_kind = ? AND key = ?",
        (w, weight_kind, key))
    _record_config_edit(
        conn, "scoring_weights",
        json.dumps({"weight_kind": weight_kind, "key": key}, sort_keys=True),
        "weight", old, w, editor, reason, now)
    conn.commit()
    summary = rescore(conn, now=now)
    return {"old": old, "new": w, "changed": True, **summary}


def update_half_life(conn, trigger_id, new_half_life_days, reason="",
                     editor=CONFIG_EDITOR, now=None):
    """Set a triggers.decay_half_life_days (R7.4 heuristic), audit it, and
    rescore active signals. Validates a whole number of days >= 1 (it is a decay
    divisor; a fractional or non-positive value is rejected, not truncated) and
    that the trigger exists. A no-op edit writes nothing and does not rescore.
    Raises ValueError. Returns {old, new, changed[, scored, decayed]}."""
    if isinstance(new_half_life_days, bool):
        raise ValueError("half-life must be a whole number of days")
    try:
        f = float(new_half_life_days)
    except (TypeError, ValueError):
        raise ValueError(
            f"half-life must be a whole number of days, got {new_half_life_days!r}")
    hl = int(f)
    if hl != f:
        raise ValueError(
            f"half-life must be a whole number of days, got {new_half_life_days!r}")
    if hl < 1:
        raise ValueError(f"half-life must be >= 1 day, got {hl}")
    row = conn.execute(
        "SELECT decay_half_life_days FROM triggers WHERE trigger_id = ?",
        (trigger_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown trigger {trigger_id!r}")
    old = row["decay_half_life_days"]
    if hl == old:
        return {"old": old, "new": hl, "changed": False}
    conn.execute(
        "UPDATE triggers SET decay_half_life_days = ? WHERE trigger_id = ?",
        (hl, trigger_id))
    _record_config_edit(conn, "triggers", trigger_id, "decay_half_life_days",
                        old, hl, editor, reason, now)
    conn.commit()
    summary = rescore(conn, now=now)
    return {"old": old, "new": hl, "changed": True, **summary}


def set_source_enabled(conn, source_id, enabled, reason="",
                       editor=CONFIG_EDITOR, now=None):
    """Toggle source_policies.enabled (R8.7 source-policy review; enabled is
    already runtime-managed) and audit it. No rescore - enabling/disabling a
    source affects the next ingestion run, not existing scores. Validates the
    source exists; a no-op toggle writes nothing. Raises ValueError. Returns
    {old, new, changed}."""
    row = conn.execute(
        "SELECT enabled FROM source_policies WHERE source_id = ?",
        (source_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown source_id {source_id!r}")
    old = row["enabled"]
    new = 1 if enabled else 0
    if new == old:
        return {"old": old, "new": new, "changed": False}
    conn.execute("UPDATE source_policies SET enabled = ? WHERE source_id = ?",
                 (new, source_id))
    _record_config_edit(conn, "source_policies", source_id, "enabled",
                        old, new, editor, reason, now)
    conn.commit()
    return {"old": old, "new": new, "changed": True}


def config_audit_tail(conn, limit=50):
    """Recent config edits, newest first, as plain dicts (R8.7 recent-changes
    panel / provenance visibility)."""
    rows = conn.execute(
        "SELECT table_name, pk, field, old_value, new_value, editor, reason, ts "
        "FROM config_audit ORDER BY audit_id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def source_policy_rows(conn, now=None):
    """Source policies for the Admin review table (R8.7): each source_health row
    as a plain dict, plus its computed state and its Gate G2 demotion
    recommendation (report-only, R9.5) keyed off the same source_id. ``g2`` is
    None for a source with no rated feedback yet."""
    from app.audit.precision import g2_status
    g2 = g2_status(precision_feedback_rows(conn), now=now)
    out = []
    for r in source_health(conn):
        d = dict(r)
        d["state"] = source_state(r, now=now)
        d["g2"] = g2.get(r["source_id"])
        out.append(d)
    return out
