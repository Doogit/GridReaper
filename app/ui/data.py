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
import json
import sqlite3
from datetime import date, datetime, timezone

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
