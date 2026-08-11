"""Classification runner framework (R3.7, R4.1, R6.2, R6.4, R6.5, R7.1, R7.2).

A classifier is a function ``classify(conn, raw_event_row)`` returning a list
of candidate dicts:

  trigger_id        triggers.trigger_id; the candidate's signal_scope must be
                    in the trigger's allowed_scopes (R7.2) or it is dropped
  signal_scope      account / sector / regulatory_calendar / ...
  entity_id         pre-attributed watchlist entity (EDGAR payloads carry
                    one); None when the classifier only has a name
  entity_name_hint  name text for EntityResolver when entity_id is None
  event_date        UTC ISO date of the underlying event
  headline          one-line signal headline
  evidence          non-empty list of {"text": ..., "locator": ...} (R4.1:
                    nothing surfaces unsourced - no evidence, no signal)
  confidence        classifier confidence 0..1; a name-resolved signal stores
                    min(classifier confidence, resolution confidence)

Classifiers never write signals. The framework owns everything downstream:
entity resolution via EntityResolver with decisions logged (R6.4), review /
no-match candidates routed to review_queue and never fired (R6.2), parent
rollup so one event yields one card per top-level account (R6.5),
deterministic ``signal_id = "{trigger_id}:{raw_event_id}:{entity_id or
scope}"`` with insert-or-skip so re-runs emit nothing new (R3.7),
signal_evidence rows ranked from source_policies, and per-event bookkeeping
in classified_events. A classifier exception on one event rolls back that
event's writes and leaves it unprocessed for the next run; it never aborts
the run. These are non-incident signals: customer_facing_allowed=1 and
incident_evidence_level NULL (R7.12 gates incidents only, a later chunk).
"""
import argparse
import json
from datetime import datetime, timezone

from app.db.connection import get_connection
from app.ingest import runner as ingest_runner
from app.resolve import EntityResolver, enqueue_review, record_decision

COMMIT_EVERY = 200          # short transactions per R3.2

ACCOUNT_SCOPES = {"account", "parent"}


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def top_level_entity(conn, entity_id):
    """R6.5: follow parent links (entity_relationships, then
    watchlist_entities.parent_id) to the top-level account. Cycle-guarded;
    a parent_id pointing outside the watchlist stops the walk."""
    seen = set()
    current = entity_id
    while current not in seen:
        seen.add(current)
        row = conn.execute(
            "SELECT parent_entity_id FROM entity_relationships "
            "WHERE child_entity_id = ? ORDER BY parent_entity_id LIMIT 1",
            (current,)).fetchone()
        parent = row["parent_entity_id"] if row else None
        if not parent:
            row = conn.execute(
                "SELECT parent_id FROM watchlist_entities WHERE entity_id = ?",
                (current,)).fetchone()
            parent = (row["parent_id"] or "").strip() if row else ""
            if parent and not conn.execute(
                    "SELECT 1 FROM watchlist_entities WHERE entity_id = ?",
                    (parent,)).fetchone():
                parent = ""
        if not parent:
            return current
        current = parent
    return current


def _triggers_config(conn):
    """trigger_id -> {'allowed_scopes': set, 'evidence_quality': str}."""
    cfg = {}
    for r in conn.execute(
            "SELECT trigger_id, allowed_scopes, evidence_quality FROM triggers"):
        try:
            scopes = set(json.loads(r["allowed_scopes"] or "[]"))
        except ValueError:
            scopes = set()
        cfg[r["trigger_id"]] = {"allowed_scopes": scopes,
                                "evidence_quality": r["evidence_quality"]}
    return cfg


def _process_candidate(conn, resolver, raw, cand, evidence_rank,
                       triggers_cfg, parser_version, counts):
    """Resolve, roll up, and insert-or-skip one candidate. Returns 1 when
    the candidate mapped to a signal (new or already existing), else 0."""
    trig = triggers_cfg.get(cand.get("trigger_id"))
    if trig is None or cand.get("signal_scope") not in trig["allowed_scopes"]:
        counts["dropped_scope"] += 1
        return 0
    evidence = [e for e in (cand.get("evidence") or [])
                if (e.get("text") or "").strip()]
    if not evidence:
        counts["dropped_no_evidence"] += 1
        return 0

    confidence = float(cand.get("confidence") or 0.0)
    entity_id = None
    if cand["signal_scope"] in ACCOUNT_SCOPES:
        entity_id = (cand.get("entity_id") or "").strip() or None
        if entity_id is None:
            hint = (cand.get("entity_name_hint") or "").strip()
            if not hint:
                counts["dropped_no_entity"] += 1
                return 0
            context = " ".join(
                [cand.get("headline") or ""] + [e["text"] for e in evidence])
            res = resolver.resolve(name=hint, context_text=context)
            if res.status == "matched":
                record_decision(conn, raw["raw_event_id"], res,
                                decided_by="classification")
                entity_id = res.entity_id
                confidence = min(confidence, res.confidence)
            elif res.status == "review":
                # R6.2: below-threshold/ambiguous MUST NOT auto-fire
                enqueue_review(conn, raw["raw_event_id"], res)
                counts["review_enqueued"] += 1
                return 0
            else:
                counts["dropped_no_entity"] += 1
                return 0
        entity_id = top_level_entity(conn, entity_id)

    signal_id = (f"{cand['trigger_id']}:{raw['raw_event_id']}:"
                 f"{entity_id or cand['signal_scope']}")
    if conn.execute("SELECT 1 FROM signals WHERE signal_id = ?",
                    (signal_id,)).fetchone():
        counts["signals_existing"] += 1
        return 1

    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
        " signal_scope, trigger_id, event_date, headline, evidence_snippet, "
        " source_url, confidence, evidence_quality, incident_evidence_level, "
        " customer_facing_allowed, score, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, NULL, 'active')",
        (signal_id, raw["raw_event_id"], entity_id, cand["signal_scope"],
         cand["trigger_id"], cand.get("event_date") or "",
         cand.get("headline") or "", evidence[0]["text"],
         raw["url"] or "", round(confidence, 4), trig["evidence_quality"]))
    for e in evidence:
        conn.execute(
            "INSERT INTO signal_evidence (signal_id, raw_event_id, "
            " evidence_text, evidence_locator, evidence_rank, "
            " extraction_version) VALUES (?, ?, ?, ?, ?, ?)",
            (signal_id, raw["raw_event_id"], e["text"],
             e.get("locator", "") or "", evidence_rank, parser_version))
    counts["signals_new"] += 1
    return 1


def run_classifier(conn, classifier_id, source_id, classify_fn,
                   parser_version, force=False, limit=None):
    """Run one classifier over the unprocessed raw_events of source_id;
    returns a summary dict. force=True reprocesses events already bookkept
    at this parser_version (deterministic ids make that emit nothing new).
    An unknown source_id is a configuration error and raises."""
    rank_row = conn.execute(
        "SELECT evidence_rank FROM source_policies WHERE source_id = ?",
        (source_id,)).fetchone()
    if rank_row is None:
        raise ValueError(
            f"Unknown source_id {source_id!r}: not in source_policies.")
    evidence_rank = rank_row["evidence_rank"]

    resolver = EntityResolver(conn)
    triggers_cfg = _triggers_config(conn)

    sql = "SELECT * FROM raw_events WHERE source_id = ?"
    if not force:
        sql += (" AND NOT EXISTS (SELECT 1 FROM classified_events c"
                "  WHERE c.raw_event_id = raw_events.raw_event_id"
                "  AND c.classifier_id = ? AND c.parser_version = ?)")
    sql += " ORDER BY first_seen_at, raw_event_id"
    params = (source_id,) if force else (source_id, classifier_id,
                                         parser_version)
    rows = conn.execute(sql, params).fetchall()

    counts = {"events_processed": 0, "events_errored": 0, "signals_new": 0,
              "signals_existing": 0, "review_enqueued": 0,
              "dropped_scope": 0, "dropped_no_evidence": 0,
              "dropped_no_entity": 0}
    last_error = ""
    for i, raw in enumerate(rows):
        if limit is not None and i >= limit:
            break
        conn.execute("SAVEPOINT classify_event")
        try:
            emitted = 0
            for cand in classify_fn(conn, raw) or []:
                emitted += _process_candidate(
                    conn, resolver, raw, cand, evidence_rank, triggers_cfg,
                    parser_version, counts)
            conn.execute(
                "INSERT INTO classified_events (raw_event_id, classifier_id, "
                " parser_version, processed_at, signals_emitted) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(raw_event_id, classifier_id, parser_version) "
                "DO UPDATE SET processed_at = excluded.processed_at, "
                " signals_emitted = excluded.signals_emitted",
                (raw["raw_event_id"], classifier_id, parser_version,
                 _utcnow(), emitted))
            conn.execute("RELEASE classify_event")
        except Exception as exc:
            # contain per event: roll back its writes, leave it unprocessed
            conn.execute("ROLLBACK TO classify_event")
            conn.execute("RELEASE classify_event")
            counts["events_errored"] += 1
            last_error = f"{raw['raw_event_id']}: {type(exc).__name__}: {exc}"
            continue
        counts["events_processed"] += 1
        if counts["events_processed"] % COMMIT_EVERY == 0:
            conn.commit()
    conn.commit()

    counts.update({
        "classifier_id": classifier_id, "source_id": source_id,
        "parser_version": parser_version,
        "status": "error" if counts["events_errored"] else "success",
        "last_error": last_error})
    return counts


def cli(classifier_id, sources, parser_version, description):
    """Shared __main__ for classifier modules. ``sources`` maps source_id ->
    classify function; runs all (or --source one) sequentially under the
    single-writer ingestion lock (R3.2). Returns an exit code (1 when any
    source had event errors)."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--source", choices=sorted(sources), default=None,
                        help="classify one source (default: all)")
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after N events per source (debugging)")
    parser.add_argument("--force", action="store_true",
                        help="reprocess events already classified at this "
                             "parser_version")
    args = parser.parse_args()
    run_sources = [args.source] if args.source else sorted(sources)

    exit_code = 0
    with ingest_runner.ingest_lock():
        conn = get_connection()
        try:
            for source_id in run_sources:
                s = run_classifier(conn, classifier_id, source_id,
                                   sources[source_id], parser_version,
                                   force=args.force, limit=args.limit)
                line = (f"{classifier_id}/{source_id}: {s['status']} "
                        f"processed={s['events_processed']} "
                        f"signals_new={s['signals_new']} "
                        f"signals_existing={s['signals_existing']} "
                        f"review={s['review_enqueued']} "
                        f"dropped_scope={s['dropped_scope']} "
                        f"dropped_no_evidence={s['dropped_no_evidence']} "
                        f"dropped_no_entity={s['dropped_no_entity']} "
                        f"errors={s['events_errored']}")
                if s["last_error"]:
                    line += f" last_error={s['last_error']}"
                print(line)
                if s["events_errored"]:
                    exit_code = 1
        finally:
            conn.close()
    return exit_code
