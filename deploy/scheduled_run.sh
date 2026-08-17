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
#   * A tick that runs too long is killed (`timeout`) well before the pipeline
#     lock's staleness window would otherwise let a second writer in behind it.
#
# What it does NOT guarantee — do not read collision-freedom into it. The R3.2
# ingestion lock is acquired and released PER STEP (app/ingest/runner.py,
# app/classify/runner.py, app/scoring.py, app/plays.py, ... — and app.licensing
# and the digest take no lock at all). This guard probes that lock once at tick
# start; it never holds it. A manual run started in an inter-step gap can still
# interleave with a tick already under way. Closing that needs a lock the Python
# steps themselves honor — follow-up, not this unit.
#
# Two locks, two different staleness rules, on purpose:
#
#   * PIPELINE_LOCK (this guard's own tick lock) records the PID that took it
#     (`$$`, written at acquisition) and is judged dead by `kill -0` — a
#     process either exists or it does not, so this needs no timeout guess.
#     Using mtime here was the actual U23 bug: a periodic mtime-toucher (a
#     backup agent, an AV scanner, an mtime-preserving restore) could keep the
#     guard reading "held" forever even after the process died, and the
#     opposite failure — deciding "stale" and `rm -f`ing a lock a live process
#     re-takes in the gap before the `rm` lands — opened a second-writer race.
#     Neither is possible once staleness means "the recorded PID is gone".
#   * LOCK (the per-step R3.2 ingestion lock) is written by Python
#     (app/ingest/runner.py's ingest_lock()), not by this script, so its
#     staleness stays mtime-based here — this guard only reads it, never owns
#     its format. The runner prefers the lock's JSON `ts` and falls back to
#     mtime when it is missing or unparseable, so the two agree on which locks
#     are breakable; tests/test_packaging.py pins the two windows equal.
#
# Heartbeat: every invocation of this script — a real run, a skip because
# another tick is live, or a skip because the ingestion lock is held — writes
# the current UTC timestamp to HEARTBEAT. That is what makes a dead cron
# daemon distinguishable from a healthy one that simply has not ticked yet
# (several source_policies carry a ttl shorter than the daily cadence, so
# "reads stale" is normal for most of the day even when everything works).
# The write is best-effort and never fails the tick.
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
#        GRIDSIGNALS_HEARTBEAT      tick-attempt heartbeat  (default data/.cron_heartbeat)
set -e

LOCK="${GRIDSIGNALS_LOCK:-data/.ingest.lock}"
PIPELINE_LOCK="${GRIDSIGNALS_PIPELINE_LOCK:-data/.scheduled.lock}"
HEARTBEAT="${GRIDSIGNALS_HEARTBEAT:-data/.cron_heartbeat}"
STALE_MINUTES=120        # == LOCK_STALE_S (2h) in app/ingest/runner.py; governs LOCK only
TIMEOUT_MINUTES=110       # comfortably under STALE_MINUTES (R23): a hung tick dies before
                          # the pipeline lock's staleness window could open a second-writer race

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }   # UTC ISO-8601 (R10.2)

# Best-effort: observability must never be why a tick fails.
mkdir -p "$(dirname "$HEARTBEAT")" 2>/dev/null || true
printf '%s\n' "$(now)" > "$HEARTBEAT" 2>/dev/null || true

is_stale() { [ -n "$(find "$1" -mmin "+$STALE_MINUTES" 2>/dev/null)" ]; }

# PID-liveness for the tick lock (U23): dead when the recorded PID is not a
# running process, OR the lock has no readable/numeric PID at all (the residue
# of a crash between the atomic create and the PID write, or a pre-upgrade
# empty-file lock). `kill -0` sends no signal, only tests whether the PID is
# live and signalable — a definitive answer, no timeout guesswork.
pipeline_lock_dead() {
  pid="$(head -n 1 "$1" 2>/dev/null)"
  case "$pid" in
    ''|*[!0-9]*) return 0 ;;
  esac
  ! kill -0 "$pid" 2>/dev/null
}

# Atomic create-if-absent, recording our own PID. `set -C`'s noclobber
# redirect and the PID write used to be two separate syscalls (open() then
# write()) against the SAME final path, so a racing reader could open the
# lock in the gap between them and observe it empty — misreading a live lock
# as the dead/pre-U23 empty-file residue and `rm -f`ing it out from under us
# (U29). Write the PID to a PID-scoped temp file in the same directory
# first, then `ln` (hard link, atomic create-if-absent like `set -C`) it
# into place: the target name never exists with any content but the final
# one. The temp name is always cleaned up, win or lose.
take_lock() {
  tmp="$1.$$.tmp"
  printf '%s\n' "$$" > "$tmp" 2>/dev/null || { rm -f "$tmp"; return 1; }
  if ln "$tmp" "$1" 2>/dev/null; then
    rm -f "$tmp"
    return 0
  fi
  rm -f "$tmp"
  return 1
}

mkdir -p "$(dirname "$PIPELINE_LOCK")"

# -- one scheduled pipeline at a time ----------------------------------------
if ! take_lock "$PIPELINE_LOCK"; then
  dead_pid="$(head -n 1 "$PIPELINE_LOCK" 2>/dev/null)"
  if pipeline_lock_dead "$PIPELINE_LOCK" && rm -f "$PIPELINE_LOCK" && take_lock "$PIPELINE_LOCK"; then
    echo "$(now) scheduled-run: broke a dead tick lock ($PIPELINE_LOCK, pid ${dead_pid:-unknown} not running)"
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
timeout "${TIMEOUT_MINUTES}m" "$@"
