"""Read-only data-access layer for the GridSignals UI (R8.1-R8.3).

Plain stdlib functions the Streamlit pages share, so the pages stay thin and
every query is covered by a hermetic test. The app is a reader; its writes are
each an explicitly sanctioned, transactional seam: ``record_feedback`` (R9.1),
``triage_decision`` (R8.2 human match decisions + review-queue disposition),
the config-write helpers over seeded config tables (R8.7, single-writer lock +
``config_audit``), and ``retier_incident`` — the R8.7 operator re-tier, the one
write that mutates a ``signals`` row's state (single-writer lock +
``incident_tier_edits``). The read seam never calls the write seam. Cards read
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

from app.classify import regulatory as regulatory_classifier
from app.classify.runner import INCIDENT_TIERS, customer_facing_for_tier
from app.db.connection import get_connection
from app.licensing import EDITABLE_FACT_COLS
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
        " sp.evidence_rank, sp.origin, "
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

# R8.7 watchlist manager: the curation columns an operator may edit on a seeded
# entity. The loader freezes exactly these against reload (load_seeds
# watchlist_entities update_cols excludes them); identifiers/name stay
# seed-refreshable so seed corrections still flow. All are watchlist CSV columns,
# so reset_entity_to_seed can restore every one of them from the seed row.
EDITABLE_ENTITY_COLS = (
    "subsector", "parent_id", "richness", "coverage_flag",
    "gov_cloud_likelihood", "notes", "owning_seller",
)

# Operator-added alias / collision-term rows carry this marker (entity_aliases.
# source / entity_collision_terms.reason) so the loader's seed-scoped DELETE
# (load_seeds WATCHLIST_ALIAS_SOURCE / WATCHLIST_COLLISION_REASON) never wipes
# them - Pattern B for the normalized side tables (R4.4).
OPERATOR_SOURCE = "operator"

# Tables that FK-reference watchlist_entities.entity_id. The FKs are bare (no ON
# DELETE, so NO ACTION): a hard-delete with any referencing row raises
# IntegrityError. remove_operator_entity counts these first and blocks legibly
# instead of crashing or orphaning history (R8.7 delete safety; trap 2).
_ENTITY_REFERENCING = (
    ("signals", "entity_id"),
    ("entity_match_decisions", "entity_id"),
    ("review_queue", "candidate_entity_id"),
    ("facility_assets", "owner_operator_entity_id"),
)

# R8.7 license-fact editor. EDITABLE_FACT_COLS is imported from app.licensing
# (canonical there, no import cycle - licensing has no UI deps); segment /
# sku_or_plan / fact_id / product_id encode a fact's matrix identity and are NOT
# editable. FACT_SEGMENTS validates an optional segment at add-time only.
FACT_SEGMENTS = ("commercial", "gcc", "gcc_high", "dod", "azure_gov", "unknown")
FACT_SOURCE_QUALITY = ("primary", "non-primary")

# Fact date columns validated as ISO-8601 date (or '') on edit.
_FACT_DATE_COLS = ("effective_date", "verified_date")

# R9.5 source registry. Tables that FK-reference source_policies.source_id; a
# hard-delete with any referencing row raises IntegrityError (bare FK, NO
# ACTION). remove_source counts these first and blocks legibly. signals has no
# direct source_id - it references transitively via signals.raw_event_id ->
# raw_events.source_id (surfaced by source_reference_breakdown's join). Mirror
# the exhaustive-FK pattern of _ENTITY_REFERENCING: every bare FK to source_id
# must be listed or an operator source that owns rows in the missing table would
# get past the breakdown and leak a raw IntegrityError.
_SOURCE_REFERENCING = (
    ("raw_events", "source_id"),
    ("source_runs", "source_id"),
    ("facility_assets", "source_id"),
)


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
    try:
        summary = rescore(conn, now=now)
    except Exception:
        conn.rollback()
        raise
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
    try:
        summary = rescore(conn, now=now)
    except Exception:
        conn.rollback()
        raise
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


# -- incident evidence-tier editor (R8.7; R10.5, R7.12, R3.3) -----------------
#
# The one UI write that mutates a signals row's state. It mirrors the config-write
# path: the route's config_write_conn() owns the single-writer lock + fresh
# connection (R3.2); this helper takes that open conn, validates (ValueError ->
# the route surfaces it, no write), UPDATEs the signal IN PLACE (never delete +
# reinsert - snapshots/evidence FK-reference it), appends one immutable
# incident_tier_edits row in the SAME transaction, and commits. It does NOT
# rescore: a tier change gates outreach (R7.12), it does not move the frozen score.

def retier_incident(conn, signal_id, new_level, reason="",
                    editor=CONFIG_EDITOR, now=None):
    """Operator confirm/re-tier of an incident signal (R8.7, R10.5, R7.12).

    Sets signals.incident_evidence_level = new_level and recomputes
    customer_facing_allowed from it (R7.12: only unconfirmed_early_warning -> 0,
    via the classifier's own customer_facing_for_tier so the two never diverge),
    then appends ONE incident_tier_edits provenance row in the same transaction.
    A no-op (new_level == current) writes nothing (idempotent - the trail records
    real changes, not button presses). Refuses a non-incident signal
    (incident_evidence_level NULL): the editor confirms an EXISTING incident's
    tier, it does not turn an ordinary signal into an incident. Raises ValueError
    (the route surfaces it, never writes). Returns
    {old_level, new_level, old_cfa, new_cfa, changed}."""
    if new_level not in INCIDENT_TIERS:
        raise ValueError(f"unknown incident tier {new_level!r}")
    row = conn.execute(
        "SELECT incident_evidence_level, customer_facing_allowed "
        "FROM signals WHERE signal_id = ?", (signal_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown signal {signal_id!r}")
    old_level = row["incident_evidence_level"]
    if old_level is None:
        raise ValueError(
            f"signal {signal_id!r} is not an incident — the tier editor "
            "re-tiers an existing incident, it does not create one")
    old_cfa = row["customer_facing_allowed"]
    if new_level == old_level:
        return {"old_level": old_level, "new_level": new_level,
                "old_cfa": old_cfa, "new_cfa": old_cfa, "changed": False}
    new_cfa = customer_facing_for_tier(new_level)
    # Trust gate (R4.1/R7.12): a re-tier that RAISES customer_facing_allowed
    # (unconfirmed -> confirmed/corroborated) clears the card for customer-facing
    # outreach — the one transition that can promote an early warning to a basis
    # for contacting the account. "Nothing surfaces unsourced" applies to that
    # promotion itself, so it requires the operator to record why (which source
    # confirmed it). Suppressing (1 -> 0) and lateral moves stay reason-optional.
    if new_cfa == 1 and old_cfa == 0 and not (reason or "").strip():
        raise ValueError(
            f"Re-tiering to {new_level} clears this card for customer-facing "
            "outreach — record which source confirmed it (a reason is required).")
    conn.execute(
        "UPDATE signals SET incident_evidence_level = ?, "
        "customer_facing_allowed = ? WHERE signal_id = ?",
        (new_level, new_cfa, signal_id))
    conn.execute(
        "INSERT INTO incident_tier_edits (signal_id, old_level, new_level, "
        " old_cfa, new_cfa, editor, reason, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (signal_id, old_level, new_level, old_cfa, new_cfa, editor,
         reason or "", _utcnow_iso(now)))
    conn.commit()
    return {"old_level": old_level, "new_level": new_level,
            "old_cfa": old_cfa, "new_cfa": new_cfa, "changed": True}


def incident_tier_history(conn, signal_id):
    """The append-only re-tier trail for one signal, newest first (R8.7
    provenance). Plain dicts so the template/tests read row['field']."""
    rows = conn.execute(
        "SELECT old_level, new_level, old_cfa, new_cfa, editor, reason, ts "
        "FROM incident_tier_edits WHERE signal_id = ? "
        "ORDER BY edit_id DESC", (signal_id,)).fetchall()
    return [dict(r) for r in rows]


def recent_retiers(conn, limit=50):
    """Central re-tier audit trail across ALL incident cards, newest first (R8.7
    oversight). incident_tier_history shows one signal's trail on its own card;
    this is the single place an operator sees every re-tier — who changed which
    card's tier, and whether any card was cleared for customer-facing outreach.
    Each incident_tier_edits row is joined to its signal's headline / scope /
    entity / current tier as a plain dict. ``gate_raised`` is True for the one
    transition that clears outreach (old_cfa 0 -> new_cfa 1) and so required a
    recorded reason (R4.1/R7.12) — the panel flags it. ``entity_name`` is None
    for a sector-scoped peer incident (no account). Read-only; ordered by
    edit_id (append-only, so it never ties like ts can)."""
    rows = conn.execute(
        "SELECT ite.edit_id, ite.signal_id, ite.old_level, ite.new_level, "
        " ite.old_cfa, ite.new_cfa, ite.editor, ite.reason, ite.ts, "
        " s.headline, e.name AS entity_name "
        "FROM incident_tier_edits ite "
        "JOIN signals s ON s.signal_id = ite.signal_id "
        "LEFT JOIN watchlist_entities e ON e.entity_id = s.entity_id "
        "ORDER BY ite.edit_id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["gate_raised"] = bool(r["old_cfa"] == 0 and r["new_cfa"] == 1)
        out.append(d)
    return out


def source_policy_rows(conn, now=None):
    """Source policies for the Admin review table (R8.7): each source_health row
    as a plain dict, plus its computed state, its Gate G2 demotion recommendation
    (report-only, R9.5) keyed off the same source_id, its ``origin`` (seed vs
    operator, from source_health), and a ``reference_count`` so the UI renders
    disable-vs-remove (only operator-origin zero-reference sources are
    removable). ``g2`` is None for a source with no rated feedback yet."""
    from app.audit.precision import g2_status
    g2 = g2_status(precision_feedback_rows(conn), now=now)
    out = []
    for r in source_health(conn):
        d = dict(r)
        d["state"] = source_state(r, now=now)
        d["g2"] = g2.get(r["source_id"])
        d["reference_count"] = sum(
            source_reference_breakdown(conn, r["source_id"]).values())
        out.append(d)
    return out


# -- watchlist entity editors (R8.7 entity manager; R3.3, R4.4, R6.3) ---------
#
# Write helpers mirror the config-write path above: the page's
# config_write_conn() owns the single-writer lock + fresh connection; each helper
# takes that open conn, validates (ValueError -> the page surfaces it, no write),
# writes + one config_audit row, and commits. NONE of them rescore: entity edits
# change FUTURE resolution/ingestion, never an existing signal's frozen score
# (R8.1). Soft-disable + edits are UPDATE-in-place; the only hard-delete is
# remove_operator_entity, guarded by an FK-referencing-row check.

def _entity_exists(conn, entity_id):
    return conn.execute(
        "SELECT 1 FROM watchlist_entities WHERE entity_id = ?",
        (entity_id,)).fetchone() is not None


def add_watchlist_entity(conn, entity_id, name, fields=None, reason="",
                         editor=CONFIG_EDITOR, now=None):
    """Add an operator watchlist entity (origin='operator', active=1) that the
    seed CSV lacks (R8.7). ``fields`` may set curation columns (a subset of
    EDITABLE_ENTITY_COLS); identifiers are the enrichment pipeline's job. New
    entity rows are not deleted by load_seeds, so they survive reload; a
    from-scratch rebuild drops them (replayable from config_audit). Raises
    ValueError. Returns {'entity_id', 'created': True}."""
    eid = (entity_id or "").strip()
    nm = (name or "").strip()
    if not eid:
        raise ValueError("entity_id is required")
    if not nm:
        raise ValueError("name is required")
    if _entity_exists(conn, eid):
        raise ValueError(f"entity_id {eid!r} already exists")
    fields = fields or {}
    bad = set(fields) - set(EDITABLE_ENTITY_COLS)
    if bad:
        raise ValueError(f"not editable column(s): {sorted(bad)}")
    cols = ["entity_id", "name", "origin", "active"] + list(fields)
    vals = [eid, nm, "operator", 1] + [fields[c] for c in fields]
    placeholders = ",".join("?" * len(cols))
    conn.execute(
        f"INSERT INTO watchlist_entities ({','.join(cols)}) VALUES ({placeholders})",
        vals)
    _record_config_edit(conn, "watchlist_entities", eid, "__add__",
                        None, nm, editor, reason, now)
    conn.commit()
    return {"entity_id": eid, "created": True}


def update_watchlist_entity(conn, entity_id, field, new_value, reason="",
                            editor=CONFIG_EDITOR, now=None):
    """Edit one curation column of a watchlist entity (R8.7). ``field`` must be in
    EDITABLE_ENTITY_COLS - identifiers/name are seed-managed, not edited here.
    A no-op edit writes nothing. Raises ValueError. Returns {old,new,changed}."""
    if field not in EDITABLE_ENTITY_COLS:
        raise ValueError(
            f"{field!r} is not an editable column "
            f"(editable: {', '.join(EDITABLE_ENTITY_COLS)})")
    row = conn.execute(
        f"SELECT {field} AS v FROM watchlist_entities WHERE entity_id = ?",
        (entity_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown entity {entity_id!r}")
    old = row["v"]
    new = "" if new_value is None else str(new_value)
    if new == ("" if old is None else str(old)):
        return {"old": old, "new": new, "changed": False}
    conn.execute(
        f"UPDATE watchlist_entities SET {field} = ? WHERE entity_id = ?",
        (new, entity_id))
    _record_config_edit(conn, "watchlist_entities", entity_id, field,
                        old, new, editor, reason, now)
    conn.commit()
    return {"old": old, "new": new, "changed": True}


def set_entity_active(conn, entity_id, active, reason="",
                      editor=CONFIG_EDITOR, now=None):
    """Soft-disable / re-enable a watchlist entity (R8.7). No rescore - a disable
    stops FUTURE resolution/ingestion (EntityResolver, EDGAR, Account 360 all
    filter active=1) while existing signals keep their frozen scores. A no-op
    toggle writes nothing. Raises ValueError. Returns {old,new,changed}."""
    row = conn.execute(
        "SELECT active FROM watchlist_entities WHERE entity_id = ?",
        (entity_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown entity {entity_id!r}")
    old = row["active"]
    new = 1 if active else 0
    if new == old:
        return {"old": old, "new": new, "changed": False}
    conn.execute("UPDATE watchlist_entities SET active = ? WHERE entity_id = ?",
                 (new, entity_id))
    _record_config_edit(conn, "watchlist_entities", entity_id, "active",
                        old, new, editor, reason, now)
    conn.commit()
    return {"old": old, "new": new, "changed": True}


def add_alias(conn, entity_id, alias, alias_type="common", reason="",
              editor=CONFIG_EDITOR, now=None):
    """Add an operator positive alias for entity resolution (R6.1/R6.3). Written
    with source='operator' so a reload never wipes it. Raises ValueError.
    Returns {'entity_id','alias','created': True}."""
    al = (alias or "").strip()
    if not _entity_exists(conn, entity_id):
        raise ValueError(f"unknown entity {entity_id!r}")
    if not al:
        raise ValueError("alias is required")
    if conn.execute("SELECT 1 FROM entity_aliases WHERE entity_id = ? AND alias = ?",
                    (entity_id, al)).fetchone():
        raise ValueError(f"alias {al!r} already exists for this entity")
    conn.execute(
        "INSERT INTO entity_aliases (entity_id, alias, alias_type, source) "
        "VALUES (?, ?, ?, ?)", (entity_id, al, alias_type or "common",
                                OPERATOR_SOURCE))
    _record_config_edit(conn, "entity_aliases", entity_id, "alias",
                        None, al, editor, reason, now)
    conn.commit()
    return {"entity_id": entity_id, "alias": al, "created": True}


def remove_alias(conn, entity_id, alias, reason="",
                 editor=CONFIG_EDITOR, now=None):
    """Remove an operator-added alias (R6.3). Refuses to touch a seed alias -
    seed aliases change via reset_entity_to_seed. Raises ValueError. Returns
    {'entity_id','alias','removed': True}."""
    row = conn.execute(
        "SELECT source FROM entity_aliases WHERE entity_id = ? AND alias = ?",
        (entity_id, alias)).fetchone()
    if row is None:
        raise ValueError(f"unknown alias {alias!r} for entity {entity_id!r}")
    if row["source"] != OPERATOR_SOURCE:
        raise ValueError(
            f"alias {alias!r} is seed data - reset the entity to change it")
    conn.execute("DELETE FROM entity_aliases WHERE entity_id = ? AND alias = ?",
                 (entity_id, alias))
    _record_config_edit(conn, "entity_aliases", entity_id, "alias",
                        alias, None, editor, reason, now)
    conn.commit()
    return {"entity_id": entity_id, "alias": alias, "removed": True}


def add_collision_term(conn, entity_id, term, reason="",
                       editor=CONFIG_EDITOR, now=None):
    """Add an operator known-collision negative term (R6.3). Written with
    reason='operator' so a reload never wipes it. Raises ValueError. Returns
    {'entity_id','term','created': True}."""
    tm = (term or "").strip()
    if not _entity_exists(conn, entity_id):
        raise ValueError(f"unknown entity {entity_id!r}")
    if not tm:
        raise ValueError("term is required")
    if conn.execute(
            "SELECT 1 FROM entity_collision_terms WHERE entity_id = ? AND term = ?",
            (entity_id, tm)).fetchone():
        raise ValueError(f"collision term {tm!r} already exists for this entity")
    conn.execute(
        "INSERT INTO entity_collision_terms (entity_id, term, reason) "
        "VALUES (?, ?, ?)", (entity_id, tm, OPERATOR_SOURCE))
    _record_config_edit(conn, "entity_collision_terms", entity_id, "term",
                        None, tm, editor, reason, now)
    conn.commit()
    return {"entity_id": entity_id, "term": tm, "created": True}


def remove_collision_term(conn, entity_id, term, reason="",
                          editor=CONFIG_EDITOR, now=None):
    """Remove an operator-added collision term (R6.3). Refuses to touch a seed
    term. Raises ValueError. Returns {'entity_id','term','removed': True}."""
    row = conn.execute(
        "SELECT reason FROM entity_collision_terms WHERE entity_id = ? AND term = ?",
        (entity_id, term)).fetchone()
    if row is None:
        raise ValueError(f"unknown collision term {term!r} for entity {entity_id!r}")
    if row["reason"] != OPERATOR_SOURCE:
        raise ValueError(
            f"collision term {term!r} is seed data - reset the entity to change it")
    conn.execute(
        "DELETE FROM entity_collision_terms WHERE entity_id = ? AND term = ?",
        (entity_id, term))
    _record_config_edit(conn, "entity_collision_terms", entity_id, "term",
                        term, None, editor, reason, now)
    conn.commit()
    return {"entity_id": entity_id, "term": term, "removed": True}


def entity_reference_breakdown(conn, entity_id):
    """{table: count} for each FK source that references this entity (only
    non-zero entries; both directions of entity_relationships combined). An
    empty dict means a hard-delete is FK-safe. Drives the remove caption/error
    so the operator sees exactly what blocks a remove, not a signals-only count."""
    out = {}
    for table, col in _ENTITY_REFERENCING:
        n = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {col} = ?",
            (entity_id,)).fetchone()["n"]
        if n:
            out[table] = n
    rel = conn.execute(
        "SELECT COUNT(*) AS n FROM entity_relationships "
        "WHERE parent_entity_id = ? OR child_entity_id = ?",
        (entity_id, entity_id)).fetchone()["n"]
    if rel:
        out["entity_relationships"] = rel
    return out


def _count_entity_references(conn, entity_id):
    """Total rows across every table that FK-references this entity. Zero means
    a hard-delete is FK-safe."""
    return sum(entity_reference_breakdown(conn, entity_id).values())


def reset_entity_to_seed(conn, entity_id, reason="",
                         editor=CONFIG_EDITOR, now=None):
    """Restore a SEEDED entity's editable columns to their seed-CSV values,
    re-enable it, and drop its operator-added aliases / collision terms (R8.7).
    An operator-added entity has no seed to reset to - remove it instead. Reads
    the seed row via the loader; performs NO entity-row delete (FK-safe). Raises
    ValueError. Returns {'entity_id','reset': True}."""
    import os
    from app.db.load_seeds import read_rows, SEEDS_DIR
    row = conn.execute(
        "SELECT origin FROM watchlist_entities WHERE entity_id = ?",
        (entity_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown entity {entity_id!r}")
    if row["origin"] != "seed":
        raise ValueError(
            f"entity {entity_id!r} is operator-added and has no seed to reset to "
            "- remove it instead")
    _, seed_rows = read_rows(os.path.join(SEEDS_DIR, "watchlist_entities.csv"))
    seed = next((r for r in seed_rows if r.get("entity_id") == entity_id), None)
    if seed is None:
        raise ValueError(f"no seed row for entity {entity_id!r}")
    assignments = [f"{c} = ?" for c in EDITABLE_ENTITY_COLS] + ["active = 1"]
    params = [seed.get(c, "") for c in EDITABLE_ENTITY_COLS] + [entity_id]
    conn.execute(
        f"UPDATE watchlist_entities SET {', '.join(assignments)} "
        "WHERE entity_id = ?", params)
    conn.execute("DELETE FROM entity_aliases WHERE entity_id = ? AND source = ?",
                 (entity_id, OPERATOR_SOURCE))
    conn.execute(
        "DELETE FROM entity_collision_terms WHERE entity_id = ? AND reason = ?",
        (entity_id, OPERATOR_SOURCE))
    _record_config_edit(conn, "watchlist_entities", entity_id, "__reset__",
                        None, "seed values", editor, reason, now)
    conn.commit()
    return {"entity_id": entity_id, "reset": True}


def remove_operator_entity(conn, entity_id, reason="",
                           editor=CONFIG_EDITOR, now=None):
    """Hard-delete an OPERATOR-added entity - the one delete path in the chunk,
    guarded by an FK-referencing-row check (R8.7 delete safety; trap 2). Blocks
    with a legible ValueError when any signal / match decision / review-queue /
    facility / relationship row references it (disable it instead). Refuses to
    touch a seeded entity. Deletes the entity's own operator aliases/collision
    terms (leaf rows) with it. Raises ValueError. Returns
    {'entity_id','removed': True}."""
    row = conn.execute(
        "SELECT name, origin FROM watchlist_entities WHERE entity_id = ?",
        (entity_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown entity {entity_id!r}")
    if row["origin"] != "operator":
        raise ValueError(
            f"entity {entity_id!r} is seed data - disable it instead of removing")
    breakdown = entity_reference_breakdown(conn, entity_id)
    if breakdown:
        detail = ", ".join(f"{table}: {n}" for table, n in breakdown.items())
        total = sum(breakdown.values())
        raise ValueError(
            f"{total} row(s) reference this entity ({detail}) - disable it "
            "instead of removing")
    conn.execute("DELETE FROM entity_aliases WHERE entity_id = ?", (entity_id,))
    conn.execute("DELETE FROM entity_collision_terms WHERE entity_id = ?",
                 (entity_id,))
    conn.execute("DELETE FROM watchlist_entities WHERE entity_id = ?", (entity_id,))
    _record_config_edit(conn, "watchlist_entities", entity_id, "__remove__",
                        row["name"], None, editor, reason, now)
    conn.commit()
    return {"entity_id": entity_id, "removed": True}


def entity_editor_rows(conn):
    """Watchlist entities for the Admin manager table (R8.7): id, name, a couple
    of curation columns, active/origin, and a signal-reference count (so the
    operator sees which entities a remove would block). Plain dicts, name-ordered."""
    rows = conn.execute(
        "SELECT e.entity_id, e.name, e.subsector, e.gov_cloud_likelihood, "
        "       e.active, e.origin, "
        "       (SELECT COUNT(*) FROM signals s WHERE s.entity_id = e.entity_id) "
        "         AS signal_count "
        "FROM watchlist_entities e ORDER BY e.name").fetchall()
    return [dict(r) for r in rows]


def entity_alias_rows(conn, entity_id):
    """Aliases + collision terms for one entity (R8.7 alias/collision editor),
    each flagged operator_editable when it is an operator-added row (seed rows
    change only via reset). Returns {'aliases': [...], 'collision_terms': [...]}."""
    aliases = [{
        "alias": r["alias"], "alias_type": r["alias_type"], "source": r["source"],
        "operator_editable": r["source"] == OPERATOR_SOURCE,
    } for r in conn.execute(
        "SELECT alias, alias_type, source FROM entity_aliases "
        "WHERE entity_id = ? ORDER BY alias", (entity_id,)).fetchall()]
    terms = [{
        "term": r["term"], "reason": r["reason"],
        "operator_editable": r["reason"] == OPERATOR_SOURCE,
    } for r in conn.execute(
        "SELECT term, reason FROM entity_collision_terms "
        "WHERE entity_id = ? ORDER BY term", (entity_id,)).fetchall()]
    return {"aliases": aliases, "collision_terms": terms}


# -- regulatory monitor (R8.4, R7.2, D8) -------------------------------------
#
# Raw regulatory records that the current regulatory classifier evaluated and
# did NOT graduate to the signal feed. "Regulatory" is the same source set the
# classifier reads; derive it from app.classify.regulatory.SOURCES so the UI
# cannot silently drift when that classifier adds or removes a source.
# Non-graduated = classified_events says this parser emitted zero signals, and
# no signals row references the raw_event. The classified_events gate matters:
# an unprocessed backlog item may still become a scored signal, so it must not
# be displayed as "chatter" yet.
REGULATORY_SOURCE_IDS = tuple(regulatory_classifier.SOURCES)
_REGULATORY_CLASSIFIER_ID = regulatory_classifier.CLASSIFIER_ID
_REGULATORY_PARSER_VERSION = regulatory_classifier.PARSER_VERSION

# Federal Register document type -> display label; mirrors the label map in
# app.classify.regulatory._headline so the two surfaces read identically.
_FR_DOC_TYPE_LABELS = {
    "Rule": "final rule", "Proposed Rule": "proposed rule", "Notice": "notice"}
# nerc_pages snapshots have no document type (they are page-diff records).
_NERC_SNAPSHOT_LABEL = "page snapshot"
_MAX_REGULATORY_HEADLINE_CHARS = 140


def _regulatory_agency(payload, source_id, source_name):
    """Agency string, taken verbatim from the payload where present (never
    inferred). nerc_pages records are NERC; a Federal Register record uses its
    own agency name, falling back to the source policy name."""
    if source_id == "nerc_pages":
        return "NERC"
    for agency in payload.get("agencies") or []:
        if isinstance(agency, dict):
            name = (agency.get("name") or agency.get("raw_name") or "").strip()
            if name:
                return name
    for name in payload.get("agency_names") or []:
        if isinstance(name, str) and name.strip():
            return name.strip()
    return source_name or source_id


def _page_label(page_url):
    """A human page label for a nerc_pages record: the URL's last non-empty path
    segment (e.g. 'CIPStandards.aspx'), falling back to the bare page_url when it
    has no path segment. Derived only from the URL's own text - nothing invented."""
    path = page_url.split("?", 1)[0].split("#", 1)[0]
    for segment in reversed(path.split("/")):
        if segment.strip():
            return segment.strip()
    return page_url


def _truncate_title(title, budget):
    """Trim a quoted-headline title to ``budget`` characters, breaking on a word
    boundary when trivial and appending an ellipsis. The caller composes the
    surrounding quotes AFTER this, so the closing quote always balances."""
    if budget < 1 or len(title) <= budget:
        return title
    cut = title[:budget - 1].rstrip()
    space = cut.rfind(" ")
    if space >= budget // 2:            # prefer a word boundary if not too early
        cut = cut[:space].rstrip()
    return cut + "…"


def _regulatory_chatter_row(row):
    """Shape one non-graduated regulatory raw_event into a display dict, all
    fields derived VERBATIM from payload keys - no action verb is ever inferred
    from the scope or date (D8). Non-JSON / malformed payloads fall back to a
    safe snippet and never raise."""
    source_id = row["source_id"]
    source_name = row["source_name"]
    try:
        payload = json.loads(row["payload"] or "")
    except (ValueError, TypeError):
        payload = None
    if not isinstance(payload, dict):
        payload = {}

    agency = _regulatory_agency(payload, source_id, source_name)
    if source_id == "nerc_pages":
        doc_type_label = _NERC_SNAPSHOT_LABEL
        # nerc_pages carries no title; use a human page label (the page_url's
        # last path segment) UNQUOTED - a raw URL wrapped in quotes reads as a
        # mis-parsed title, and the full URL stays in the record's Source link.
        page_url = (payload.get("page_url") or row["url"] or "").strip()
        title = _page_label(page_url)
        # Unquoted label; the full URL is still shown via the Source link.
        headline = f"{agency} {doc_type_label}: {title}" if title \
            else f"{agency} {doc_type_label}"
    else:
        doc_type_label = _FR_DOC_TYPE_LABELS.get(
            payload.get("type"), (payload.get("type") or "document"))
        title = (payload.get("title") or "").strip()
        if not title:
            # Last resort: a safe snippet of the raw payload, never empty.
            title = _payload_snippet(row["payload"]) or "(untitled record)"
        # Truncate the TITLE to a budget FIRST, then compose the quoted headline
        # around it, so the closing quote is always appended after truncation and
        # the quote pair always balances (never a dangling opening quote).
        budget = _MAX_REGULATORY_HEADLINE_CHARS - len(f'{agency} {doc_type_label}: ""')
        headline = f'{agency} {doc_type_label}: "{_truncate_title(title, budget)}"'

    # Descriptive "does it carry a compliance clock" flag - NOT a graduation
    # claim (a chatter record can carry an anchor and still not have graduated).
    has_anchor = bool(payload.get("effective_on")
                      or payload.get("comments_close_on"))

    return {
        "raw_event_id": row["raw_event_id"],
        "source_id": source_id,
        "source_name": source_name,
        "agency": agency,
        "doc_type_label": doc_type_label,
        "title": title,
        "headline": headline,
        "has_anchor": has_anchor,
        "event_date": row["event_date"],
        "url": row["url"],
        "docket_ids": payload.get("docket_ids") or [],
        "comments_close_on": payload.get("comments_close_on") or None,
        "effective_on": payload.get("effective_on") or None,
    }


def regulatory_monitor(conn, limit=100):
    """Non-graduated regulatory chatter, newest first (R8.4). Regulatory
    raw_events (REGULATORY_SOURCE_IDS) that the current regulatory parser has
    evaluated with zero emitted signals and with NO signals row referencing
    them - i.e. records that did NOT graduate to the signal feed. Read-only:
    this NEVER writes and NEVER emits a score/confidence/entity_name/
    signal_scope/signal_id. Returns up to ``limit`` display dicts."""
    placeholders = _placeholders(len(REGULATORY_SOURCE_IDS))
    rows = conn.execute(
        "SELECT re.raw_event_id, re.source_id, re.event_date, re.payload, "
        " re.url, re.first_seen_at, sp.name AS source_name "
        "FROM raw_events re "
        "JOIN source_policies sp ON sp.source_id = re.source_id "
        f"WHERE re.source_id IN ({placeholders}) "
        " AND EXISTS (SELECT 1 FROM classified_events ce "
        "             WHERE ce.raw_event_id = re.raw_event_id "
        "               AND ce.classifier_id = ? "
        "               AND ce.parser_version = ? "
        "               AND ce.signals_emitted = 0) "
        " AND NOT EXISTS (SELECT 1 FROM signals s "
        "                 WHERE s.raw_event_id = re.raw_event_id) "
        "ORDER BY re.event_date DESC, re.raw_event_id DESC "
        "LIMIT ?",
        list(REGULATORY_SOURCE_IDS) + [
            _REGULATORY_CLASSIFIER_ID, _REGULATORY_PARSER_VERSION, limit,
        ]).fetchall()
    return [_regulatory_chatter_row(r) for r in rows]


# -- license-fact editors (R8.7; R3.3, R7.6) ---------------------------------
#
# license_facts is owned by the app.licensing transform, not load_seeds. These
# helpers mirror the config-write path: validate (ValueError -> the page
# surfaces it, no write), write + one config_audit row, commit. NONE rescore -
# cards read frozen license_play_snapshots (R7.6), never live facts, so a fact
# edit never moves an existing card; it is picked up by the NEXT play snapshot.
# There is no fact-delete path (edit + add + supersede only): fact_snapshot_
# citations is the JSON-scan gate the FK-COUNT pattern cannot see, surfaced as a
# caption ("cited by N cards - edit in place"), not a delete guard.


def _valid_fact_date(value):
    """A fact date column accepts '' (unset) or an ISO-8601 date; else False."""
    v = (value or "").strip()
    if not v:
        return True
    try:
        date.fromisoformat(v)
    except ValueError:
        return False
    return True


def _is_future_date(value, now=None):
    """True when value parses to a date after today (UTC). '' and unparseable
    are not future (parseability is checked separately)."""
    try:
        d = date.fromisoformat((value or "").strip())
    except ValueError:
        return False
    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    return d > today


def _fact_id_list(raw):
    """Return fact_ids only when the snapshot column holds a JSON array."""
    try:
        ids = json.loads(raw or "[]")
    except ValueError:
        return []
    return ids if isinstance(ids, list) else []


def _normalize_evidence_rank(value):
    """Evidence rank is optional, but when present it must be documented 1..3."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        if not v.isdigit():
            raise ValueError("evidence_rank must be 1, 2, or 3")
        rank = int(v)
    else:
        try:
            rank = int(value)
        except (TypeError, ValueError):
            raise ValueError("evidence_rank must be 1, 2, or 3")
    if rank not in (1, 2, 3):
        raise ValueError("evidence_rank must be 1, 2, or 3")
    return rank


def update_license_fact(conn, fact_id, field, new_value, reason="",
                        editor=CONFIG_EDITOR, now=None):
    """Edit one editable column of a license fact (R8.7). ``field`` must be in
    EDITABLE_FACT_COLS (segment / sku_or_plan / identity are not editable).
    source_quality is validated against FACT_SOURCE_QUALITY; date columns must be
    an ISO-8601 date or ''. A no-op edit writes nothing and does not rescore.
    Raises ValueError. Returns {old, new, changed}."""
    if field not in EDITABLE_FACT_COLS:
        raise ValueError(
            f"{field!r} is not an editable column "
            f"(editable: {', '.join(EDITABLE_FACT_COLS)})")
    new = "" if new_value is None else str(new_value)
    if field == "source_quality" and new and new not in FACT_SOURCE_QUALITY:
        raise ValueError(
            f"source_quality must be one of {FACT_SOURCE_QUALITY}, got {new!r}")
    if field in _FACT_DATE_COLS and not _valid_fact_date(new):
        raise ValueError(f"{field} must be an ISO-8601 date or empty, got {new!r}")
    # verified_date only: a future date would compute a negative age and hide the
    # fact from the R10.7 staleness list (a fact can't be "verified" tomorrow).
    # effective_date may legitimately be future (a rule effective 2026-10-01).
    if field == "verified_date" and _is_future_date(new, now):
        raise ValueError("verified_date cannot be in the future")
    row = conn.execute(
        f"SELECT {field} AS v FROM license_facts WHERE fact_id = ?",
        (fact_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown fact {fact_id!r}")
    old = row["v"]
    if new == ("" if old is None else str(old)):
        return {"old": old, "new": new, "changed": False}
    conn.execute(
        f"UPDATE license_facts SET {field} = ? WHERE fact_id = ?",
        (new, fact_id))
    _record_config_edit(conn, "license_facts", fact_id, field,
                        old, new, editor, reason, now)
    conn.commit()
    return {"old": old, "new": new, "changed": True}


def add_license_fact(conn, fact_id, product_id, fields=None, reason="",
                     editor=CONFIG_EDITOR, now=None):
    """Add an operator license fact the transform lacks (R8.7). ``fact_id`` must
    be unique; ``product_id`` must exist in products or be None/''. ``fields``
    may set EDITABLE_FACT_COLS and an optional ``segment`` (validated against
    FACT_SEGMENTS). sku_or_plan is left NULL - that is the marker the licensing
    rebuild uses to leave operator facts untouched. Raises ValueError. Returns
    {'fact_id', 'created': True}."""
    fid = (fact_id or "").strip()
    if not fid:
        raise ValueError("fact_id is required")
    if conn.execute("SELECT 1 FROM license_facts WHERE fact_id = ?",
                    (fid,)).fetchone():
        raise ValueError(f"fact_id {fid!r} already exists")
    pid = (product_id or "").strip() or None
    if pid is not None and not conn.execute(
            "SELECT 1 FROM products WHERE product_id = ?", (pid,)).fetchone():
        raise ValueError(f"unknown product_id {pid!r}")
    fields = dict(fields or {})
    segment = fields.pop("segment", None)
    bad = set(fields) - set(EDITABLE_FACT_COLS)
    if bad:
        raise ValueError(f"not editable column(s): {sorted(bad)}")
    if segment is not None and segment not in FACT_SEGMENTS:
        raise ValueError(f"segment must be one of {FACT_SEGMENTS}, got {segment!r}")
    sq = fields.get("source_quality")
    if sq and sq not in FACT_SOURCE_QUALITY:
        raise ValueError(
            f"source_quality must be one of {FACT_SOURCE_QUALITY}, got {sq!r}")
    for dc in _FACT_DATE_COLS:
        if dc in fields and not _valid_fact_date(fields[dc]):
            raise ValueError(
                f"{dc} must be an ISO-8601 date or empty, got {fields[dc]!r}")
    # verified_date only: future dates would hide the fact from staleness (see
    # update_license_fact); effective_date may legitimately be future.
    if "verified_date" in fields and _is_future_date(fields["verified_date"], now):
        raise ValueError("verified_date cannot be in the future")
    cols = ["fact_id", "product_id", "segment"] + list(fields)
    vals = [fid, pid, segment] + [fields[c] for c in fields]
    conn.execute(
        f"INSERT INTO license_facts ({','.join(cols)}) "
        f"VALUES ({_placeholders(len(cols))})", vals)
    # Record the created fact_id as new_value so the audit trail says WHAT was
    # created (product_id may be None for a product-less operator fact).
    _record_config_edit(conn, "license_facts", fid, "__add__",
                        None, fid, editor, reason, now)
    conn.commit()
    return {"fact_id": fid, "created": True}


def fact_snapshot_citations(conn, fact_id):
    """Count of license_play_snapshots that cite this fact_id (R7.6). fact_ids is
    a JSON array TEXT (not a real FK: 0001_initial.sql), so this hand-scans and
    json.loads each, guarding malformed JSON like signal_detail. This is the
    citation gate the FK-COUNT breakdown pattern cannot see."""
    n = 0
    for r in conn.execute("SELECT fact_ids FROM license_play_snapshots"):
        if fact_id in _fact_id_list(r["fact_ids"]):
            n += 1
    return n


def license_fact_rows(conn):
    """License facts for the Admin editor selectbox (R8.7): every EDITABLE_FACT_COL
    plus identity columns and the operator/transform origin (sku_or_plan IS NULL
    => operator-added). Plain dicts, ordered by fact_id."""
    rows = conn.execute(
        "SELECT lf.fact_id, lf.product_id, lf.sku_or_plan, lf.segment, "
        " lf.price_note, lf.included_or_addon, lf.prerequisite, "
        " lf.effective_date, lf.verified_date, lf.source_quality, lf.source_url, "
        " p.name AS product_name "
        "FROM license_facts lf "
        "LEFT JOIN products p ON p.product_id = lf.product_id "
        "ORDER BY lf.fact_id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["origin"] = "operator" if r["sku_or_plan"] is None else "transform"
        out.append(d)
    return out


# -- source registry (R9.5 add / remove) -------------------------------------
#
# add_source writes origin='operator' (migration 0009) so a reload never
# clobbers it and remove can tell it apart from a seeded row. remove_source is
# the only source hard-delete: refused for seeded sources (disable instead) and
# FK-gated for operator ones (a referencing raw_event / run / signal turns the
# click into a legible error, never a leaked IntegrityError). Editing a seeded
# source's policy fields is out of scope for this chunk (follow-up).


def source_reference_breakdown(conn, source_id):
    """{table: count} for each source that references this source_id (only
    non-zero entries): raw_events + source_runs directly, and signals
    transitively via signals.raw_event_id -> raw_events.source_id (there is no
    signals.source_id FK). An empty dict means a hard-delete is FK-safe."""
    out = {}
    for table, col in _SOURCE_REFERENCING:
        n = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {col} = ?",
            (source_id,)).fetchone()["n"]
        if n:
            out[table] = n
    signals = conn.execute(
        "SELECT COUNT(*) AS n FROM signals s "
        "JOIN raw_events re ON re.raw_event_id = s.raw_event_id "
        "WHERE re.source_id = ?", (source_id,)).fetchone()["n"]
    if signals:
        out["signals"] = signals
    return out


def add_source(conn, source_id, name, access_method="", ttl=None,
               evidence_rank=None, tos_status="", rate_limit="",
               last_policy_review="", enabled=True, reason="",
               editor=CONFIG_EDITOR, now=None):
    """Add an operator source policy the seed set lacks (origin='operator',
    R9.5). ``source_id`` must be unique and ``name`` non-empty. New rows are not
    deleted by load_seeds, so they survive reload. Raises ValueError. Returns
    {'source_id', 'created': True}."""
    sid = (source_id or "").strip()
    nm = (name or "").strip()
    if not sid:
        raise ValueError("source_id is required")
    if not nm:
        raise ValueError("name is required")
    if conn.execute("SELECT 1 FROM source_policies WHERE source_id = ?",
                    (sid,)).fetchone():
        raise ValueError(f"source_id {sid!r} already exists")
    rank = _normalize_evidence_rank(evidence_rank)
    conn.execute(
        "INSERT INTO source_policies (source_id, name, access_method, ttl, "
        " enabled, tos_status, evidence_rank, rate_limit, last_policy_review, "
        " origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'operator')",
        (sid, nm, access_method or "", ttl, 1 if enabled else 0,
         tos_status or "", rank, rate_limit or "",
         last_policy_review or ""))
    _record_config_edit(conn, "source_policies", sid, "__add__",
                        None, nm, editor, reason, now)
    conn.commit()
    return {"source_id": sid, "created": True}


def remove_source(conn, source_id, reason="", editor=CONFIG_EDITOR, now=None):
    """Hard-delete an OPERATOR-added source (R9.5), guarded by an FK-referencing-
    row check. Refuses a seeded source (disable it instead). Blocks with a
    legible ValueError when any raw_event / source_run / signal references it,
    never leaking an IntegrityError. Raises ValueError. Returns
    {'source_id', 'removed': True}."""
    row = conn.execute(
        "SELECT origin FROM source_policies WHERE source_id = ?",
        (source_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown source_id {source_id!r}")
    if row["origin"] != "operator":
        raise ValueError(
            f"source {source_id!r} is seeded - disable it instead of removing")
    breakdown = source_reference_breakdown(conn, source_id)
    if breakdown:
        detail = ", ".join(f"{table}: {n}" for table, n in breakdown.items())
        total = sum(breakdown.values())
        raise ValueError(
            f"{total} row(s) reference this source ({detail}) - disable it "
            "instead of removing")
    # Belt-and-suspenders: even if a future bare FK to source_id is not yet in
    # _SOURCE_REFERENCING, the DELETE must never leak a raw IntegrityError to the
    # UI - turn it into the same legible ValueError.
    try:
        conn.execute("DELETE FROM source_policies WHERE source_id = ?",
                     (source_id,))
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError(
            "other rows reference this source - disable it instead of removing")
    _record_config_edit(conn, "source_policies", source_id, "__remove__",
                        source_id, None, editor, reason, now)
    conn.commit()
    return {"source_id": source_id, "removed": True}
