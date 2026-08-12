"""Audit judge runner: sample, assemble, judge, and record (R9.7, R9.8, R9.9,
R9.12, R3.2).

The runner is the deterministic, budget-capped orchestration layer around the
injectable ``AuditClient`` (client.py) and the versioned prompt/parser
(schema.py). Per invocation it:

  1. Records an ``audit_runs`` row up front (status 'running', R9.12: a skip is
     recorded, never silent).
  2. Samples up to ``min(limit, MAX_SIGNALS_PER_RUN)`` signals NOT yet audited
     at the current PROMPT_VERSION (R9.7 dedupe), stratified across
     (trigger_id, source_id, signal_scope) so one busy trigger cannot crowd out
     the rest of the sample (deterministic: sorted strata + a seeded RNG).
  3. Assembles the objective-check record (R9.7/R9.8: the card plus its raw
     source text, extracted evidence, and license play) and asks the judge.
  4. Writes one ``audit`` row per check in ``schema.CHECKS`` for each 'ok'
     verdict, stamping model_id + prompt_version + parser_version (R9.9).
  5. Degrades cleanly (R9.12): no key / over budget / nothing to sample finalize
     the run as a recorded skip and return without raising; a per-signal error
     is contained (that signal gets no rows) and never becomes a synthetic pass.

Concurrency: ``run_audit`` is lock-free so tests can drive it on an in-memory
connection; only ``cli()`` wraps it in the single-writer ingestion lock (R3.2).
"""
import argparse
import json
import random
import sys
import uuid
from datetime import datetime, timezone

from app.audit import config as cfg
from app.audit import schema
from app.audit.client import AuditClient
from app.db.connection import get_connection
from app.ingest import runner as ingest_runner


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


# -- record assembly (R9.7/R9.8) --------------------------------------------

def assemble_record(conn, signal_id):
    """Build the judge input dict for one signal (the shape
    ``schema.build_judge_input`` documents): the card fields, the raw source
    text from its raw_events.payload, its extracted evidence snippets ordered by
    rank, and any license play(s). ``entity_name`` is None for a scope with no
    entity; ``license_plays`` is empty when the signal carries no play.

    Returns None if the signal is unknown.
    """
    sig = conn.execute(
        "SELECT s.signal_id, s.raw_event_id, s.entity_id, s.signal_scope, "
        " s.trigger_id, s.headline, s.evidence_quality, "
        " s.incident_evidence_level, r.payload AS payload, "
        " t.name AS trigger_name, e.name AS entity_name "
        "FROM signals s "
        "LEFT JOIN raw_events r ON r.raw_event_id = s.raw_event_id "
        "LEFT JOIN triggers t ON t.trigger_id = s.trigger_id "
        "LEFT JOIN watchlist_entities e ON e.entity_id = s.entity_id "
        "WHERE s.signal_id = ?", (signal_id,)).fetchone()
    if sig is None:
        return None

    evidence = [
        {"text": r["evidence_text"] or "", "locator": r["evidence_locator"] or ""}
        for r in conn.execute(
            "SELECT evidence_text, evidence_locator FROM signal_evidence "
            "WHERE signal_id = ? ORDER BY evidence_rank, rowid", (signal_id,))
    ]

    license_plays = [
        {"product_name": r["product_name"] or r["product_id"] or "",
         "recommended_path": r["recommended_path"] or "",
         "display_text": r["display_text"] or ""}
        for r in conn.execute(
            "SELECT sn.display_text, c.recommended_path, c.product_id, "
            " p.name AS product_name "
            "FROM license_play_snapshots sn "
            "JOIN license_play_candidates c ON c.play_id = sn.play_id "
            "LEFT JOIN products p ON p.product_id = c.product_id "
            "WHERE sn.signal_id = ? ORDER BY sn.play_id", (signal_id,))
    ]

    return {
        "signal_id": sig["signal_id"],
        "headline": sig["headline"],
        "entity_name": sig["entity_name"],
        "signal_scope": sig["signal_scope"],
        "trigger_id": sig["trigger_id"],
        "trigger_name": sig["trigger_name"],
        "evidence_quality": sig["evidence_quality"],
        "incident_evidence_level": sig["incident_evidence_level"],
        "raw_source_text": _pretty_payload(sig["payload"]),
        "evidence": evidence,
        "license_plays": license_plays,
    }


def _pretty_payload(payload):
    """Pretty-print the raw source JSON for the judge; pass through non-JSON or
    empty payloads verbatim."""
    if not payload:
        return ""
    try:
        return json.dumps(json.loads(payload), indent=2, sort_keys=True)
    except (ValueError, TypeError):
        return payload


# -- sampling (R9.7) ---------------------------------------------------------

def sample_signals(conn, prompt_version, limit, rng_seed=0):
    """Pick up to ``limit`` signal_ids to audit, deterministically.

    Excludes any signal that already has an ``audit`` row at this
    ``prompt_version`` (R9.7 re-run dedupe). Stratifies the remaining candidates
    by (trigger_id, source_id, signal_scope): strata are sorted, then filled
    round-robin so the sample spreads across triggers/sources/scopes instead of
    draining one busy stratum. Selection is deterministic for a fixed
    ``rng_seed`` — a seeded ``random.Random`` shuffles within each stratum and
    the round-robin order is fixed by the sorted strata keys.
    """
    if limit <= 0:
        return []
    rows = conn.execute(
        "SELECT s.signal_id, s.trigger_id, s.signal_scope, "
        " r.source_id AS source_id "
        "FROM signals s "
        "LEFT JOIN raw_events r ON r.raw_event_id = s.raw_event_id "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM audit a WHERE a.signal_id = s.signal_id "
        "  AND a.prompt_version = ?) "
        "ORDER BY s.signal_id", (prompt_version,)).fetchall()
    if not rows:
        return []

    rng = random.Random(rng_seed)
    strata = {}
    for r in rows:
        key = (r["trigger_id"] or "", r["source_id"] or "",
               r["signal_scope"] or "")
        strata.setdefault(key, []).append(r["signal_id"])
    # Deterministic shuffle within each stratum, deterministic stratum order.
    ordered_keys = sorted(strata)
    for key in ordered_keys:
        rng.shuffle(strata[key])

    selected = []
    while len(selected) < limit:
        progressed = False
        for key in ordered_keys:
            bucket = strata[key]
            if bucket:
                selected.append(bucket.pop())
                progressed = True
                if len(selected) >= limit:
                    break
        if not progressed:
            break
    return selected


# -- run bookkeeping ---------------------------------------------------------

def _finalize_run(conn, run_id, now, status, signals_sampled, verdicts_written,
                  budget_spent, error_state="", skipped_reason=""):
    conn.execute(
        "UPDATE audit_runs SET finished_at = ?, status = ?, "
        " signals_sampled = ?, verdicts_written = ?, budget_spent = ?, "
        " error_state = ?, skipped_reason = ? WHERE run_id = ?",
        (now, status, signals_sampled, verdicts_written, budget_spent,
         error_state, skipped_reason, run_id))
    conn.commit()
    return {"run_id": run_id, "status": status,
            "signals_sampled": signals_sampled,
            "verdicts_written": verdicts_written,
            "budget_spent": budget_spent, "error_state": error_state,
            "skipped_reason": skipped_reason}


def run_audit(conn, client, now=None, limit=None, rng_seed=0):
    """Run the audit judge over a sampled batch; write ``audit`` rows and a
    finalized ``audit_runs`` row; return a summary dict. Never raises for a
    degraded judge (R9.12) or a contained per-signal error.

    ``now`` (UTC ISO-8601) is injectable for deterministic timestamps. Not
    wrapped in the ingestion lock — ``cli()`` owns that (R3.2).
    """
    now = now or _utcnow()
    run_id = f"audit:{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO audit_runs (run_id, started_at, finished_at, model_id, "
        " prompt_version, parser_version, signals_sampled, verdicts_written, "
        " budget_spent, status) "
        "VALUES (?, ?, '', ?, ?, ?, 0, 0, 0, 'running')",
        (run_id, now, client.model_id, schema.PROMPT_VERSION,
         schema.PARSER_VERSION))
    conn.commit()

    # R9.12: no key -> record the skip and exit success, write no verdicts.
    if not client.available():
        return _finalize_run(
            conn, run_id, now, "skipped_no_key", 0, 0, 0.0,
            skipped_reason="ANTHROPIC_API_KEY not set")

    limit = cfg.MAX_SIGNALS_PER_RUN if limit is None \
        else min(limit, cfg.MAX_SIGNALS_PER_RUN)
    signal_ids = sample_signals(conn, schema.PROMPT_VERSION, limit, rng_seed)
    signals_sampled = len(signal_ids)
    if not signal_ids:
        return _finalize_run(
            conn, run_id, now, "skipped_no_signals", 0, 0, 0.0,
            skipped_reason="no unaudited signals at this prompt_version")

    verdicts_written = 0
    budget_spent = 0.0
    error_state = ""
    per_signal_error = False
    stop_status = None       # set to a skip status when the loop breaks early
    stop_reason = ""

    for signal_id in signal_ids:
        conn.execute("SAVEPOINT audit_signal")
        try:
            record = assemble_record(conn, signal_id)
            if record is None:
                # Signal vanished between sampling and assembly; contain it.
                conn.execute("RELEASE audit_signal")
                continue
            result = client.judge(record)

            if result.status == "ok":
                for check in schema.CHECKS:
                    verdict = result.verdicts.get(check) or {}
                    conn.execute(
                        "INSERT INTO audit (signal_id, check_type, result, "
                        " judge_notes, model_id, prompt_version, ts, "
                        " parser_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (signal_id, check, verdict.get("result"),
                         verdict.get("notes") or "", result.model_id,
                         schema.PROMPT_VERSION, now, schema.PARSER_VERSION))
                verdicts_written += len(schema.CHECKS)
                budget_spent += result.cost_usd or 0.0
                conn.execute("RELEASE audit_signal")

            elif result.status == "over_budget":
                # Budget ceiling hit before this call: nothing paid, stop.
                conn.execute("RELEASE audit_signal")
                stop_status = "over_budget"
                stop_reason = result.error or "per-run budget reached"
                break

            elif result.status == "unavailable":
                # Should not normally happen after the up-front check; treat as
                # a mid-run no-key and stop.
                conn.execute("RELEASE audit_signal")
                stop_status = "no_key"
                stop_reason = result.error or "ANTHROPIC_API_KEY not set"
                break

            else:  # "error" — contain this signal, write nothing, continue.
                budget_spent += result.cost_usd or 0.0
                conn.execute("RELEASE audit_signal")
                per_signal_error = True
                error_state = result.error or "judge error"
                continue
        except Exception as exc:  # noqa: BLE001 - contain any per-signal failure
            conn.execute("ROLLBACK TO audit_signal")
            conn.execute("RELEASE audit_signal")
            per_signal_error = True
            error_state = f"{signal_id}: {type(exc).__name__}: {exc}"
            continue

    # Finalize. An early stop with zero verdicts is a recorded skip; a partial
    # run (some verdicts, then budget) is a success flagged with the reason.
    if stop_status == "over_budget":
        if verdicts_written == 0:
            return _finalize_run(
                conn, run_id, now, "skipped_over_budget", signals_sampled,
                0, round(budget_spent, 6), skipped_reason=stop_reason)
        return _finalize_run(
            conn, run_id, now, "success", signals_sampled, verdicts_written,
            round(budget_spent, 6), error_state=error_state,
            skipped_reason=f"stopped early: {stop_reason}")
    if stop_status == "no_key":
        return _finalize_run(
            conn, run_id, now, "skipped_no_key", signals_sampled,
            verdicts_written, round(budget_spent, 6), skipped_reason=stop_reason)

    # Clean or contained-error finish. 'error' only when a per-signal error
    # occurred AND nothing was written (the run produced no usable verdicts);
    # otherwise 'success' with error_state carrying the last contained error.
    if per_signal_error and verdicts_written == 0:
        status = "error"
    else:
        status = "success"
    return _finalize_run(
        conn, run_id, now, status, signals_sampled, verdicts_written,
        round(budget_spent, 6), error_state=error_state)


# -- CLI (R3.2, R9.12) -------------------------------------------------------

def cli(argv=None):
    """Run the audit judge from the command line under the single-writer
    ingestion lock (R3.2). Builds a real ``AuditClient`` (reads
    ANTHROPIC_API_KEY from the env — an absent key exercises the R9.12
    skipped_no_key dry-run). Prints a one-line summary. Returns 0 for a clean
    run OR a clean skip (a skip is success, never a crash — R9.12); 1 only if
    the run finalized 'error'."""
    parser = argparse.ArgumentParser(
        description="Run the automated accuracy audit (LLM-as-judge).")
    parser.add_argument("--limit", type=int, default=None,
                        help="max signals to audit this run "
                             f"(capped at MAX_SIGNALS_PER_RUN="
                             f"{cfg.MAX_SIGNALS_PER_RUN})")
    parser.add_argument("--rng-seed", type=int, default=0,
                        help="seed for deterministic stratified sampling")
    args = parser.parse_args(argv)

    with ingest_runner.ingest_lock():
        conn = get_connection()
        try:
            client = AuditClient()
            summary = run_audit(conn, client, limit=args.limit,
                                rng_seed=args.rng_seed)
        finally:
            conn.close()

    line = (f"audit {summary['run_id']}: {summary['status']} "
            f"signals_sampled={summary['signals_sampled']} "
            f"verdicts_written={summary['verdicts_written']} "
            f"budget_spent=${summary['budget_spent']:.6f}")
    if summary["skipped_reason"]:
        line += f" skipped_reason={summary['skipped_reason']}"
    if summary["error_state"]:
        line += f" error_state={summary['error_state']}"
    print(line)
    return 1 if summary["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(cli())
