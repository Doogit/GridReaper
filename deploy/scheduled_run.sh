#!/bin/sh
# Lock guard for scheduled (cron) runs — R3.1, R3.2.
#
# What this guarantees, precisely:
#
#   * Scheduled ticks never overlap EACH OTHER. The guard takes its own
#     pipeline-scoped lock for the whole tick and releases it on exit, so a run
#     that outlives its cadence turns the next tick into a recorded skip instead
#     of a second pipeline against the same SQLite file.
#   * A tick that starts while a manual or first-load run already holds the
#     per-step ingestion lock is a recorded skip, not a crash. That lock
#     (app/ingest/runner.py) RAISES on a live lock, and deploy/ingest_pipeline.sh
#     runs under `set -e`, so an unguarded tick would abort mid-pipeline.
#
# What it does NOT guarantee — do not read collision-freedom into it. The R3.2
# ingestion lock is acquired and released PER STEP (app/ingest/runner.py,
# app/classify/runner.py, app/scoring.py, app/plays.py, ... — and app.licensing
# and the digest take no lock at all). This guard probes that lock once at tick
# start; it never holds it. A manual run started in an inter-step gap can still
# interleave with a tick already under way. Closing that needs a lock the Python
# steps themselves honor — follow-up, not this unit.
#
# Staleness: a lock whose mtime is older than the window below is treated as
# abandoned and REMOVED, so the residue of a crashed run cannot wedge the
# schedule forever — that is the "never re-ingests" defect from the other side.
# The runner prefers the lock's JSON `ts` and falls back to the same mtime when
# it is missing or unparseable, so the two agree on which locks are breakable;
# tests/test_packaging.py pins the two windows equal.
#
# Usage: sh deploy/scheduled_run.sh <command> [args...]
# Env:   GRIDSIGNALS_LOCK           per-step ingestion lock (default data/.ingest.lock);
#                                   app/ingest/runner.py reads the same variable, so every
#                                   writer that calls ingest_lock() bare agrees with the guard.
#                                   ONE caller does not: app/ui/data.py:config_write_conn
#                                   passes an explicit path, which wins over the variable, so
#                                   an Admin save still locks data/.ingest.lock. Setting this
#                                   variable therefore splits the UI writer off from the rest
#                                   until that call site is fixed - follow-up, not this unit.
#        GRIDSIGNALS_PIPELINE_LOCK  this guard's tick lock  (default data/.scheduled.lock)
set -e

LOCK="${GRIDSIGNALS_LOCK:-data/.ingest.lock}"
PIPELINE_LOCK="${GRIDSIGNALS_PIPELINE_LOCK:-data/.scheduled.lock}"
STALE_MINUTES=120        # == LOCK_STALE_S (2h) in app/ingest/runner.py

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }   # UTC ISO-8601 (R10.2)

is_stale() { [ -n "$(find "$1" -mmin "+$STALE_MINUTES" 2>/dev/null)" ]; }

# Atomic create-if-absent. The subshell keeps noclobber scoped and keeps a
# redirection error on a special builtin from taking the whole script with it.
take_lock() { ( set -C; : > "$1" ) 2>/dev/null; }

mkdir -p "$(dirname "$PIPELINE_LOCK")"

# -- one scheduled pipeline at a time ----------------------------------------
if ! take_lock "$PIPELINE_LOCK"; then
  if is_stale "$PIPELINE_LOCK" && rm -f "$PIPELINE_LOCK" && take_lock "$PIPELINE_LOCK"; then
    echo "$(now) scheduled-run: broke a stale tick lock ($PIPELINE_LOCK)"
  else
    echo "$(now) scheduled-run: skipped — a scheduled run is already in progress ($PIPELINE_LOCK)"
    exit 0
  fi
fi
trap 'rm -f "$PIPELINE_LOCK"' EXIT INT TERM

# -- do not collide with a manual / first-load run already in flight ---------
if [ -e "$LOCK" ]; then
  if is_stale "$LOCK"; then
    # Remove it, do not merely announce it: the Python steps take this same
    # lock per step and raise on a live one, so leaving the file in place would
    # abort the pipeline the guard just decided to run.
    rm -f "$LOCK"
    echo "$(now) scheduled-run: broke a stale ingestion lock ($LOCK) — proceeding"
  else
    echo "$(now) scheduled-run: skipped — ingestion lock held ($LOCK)"
    exit 0
  fi
fi

echo "$(now) scheduled-run: starting $*"
"$@"
