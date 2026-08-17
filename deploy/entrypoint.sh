#!/bin/sh
# Container entrypoint (Azure/Docker). Unlike the old build-time bake, the image
# ships WITHOUT a dataset — data is populated at runtime on first load, so image
# builds are fast and fetch-free.
#
# 1. Seed schema + config (fast, offline, idempotent) so the app can serve.
# 2. If the feed is empty (fresh container, or a durable volume mounted empty),
#    run the ingest pipeline in the BACKGROUND (a process, not the UI — R3.1) so
#    the web app comes up immediately. The feed fills in and the 120s feed
#    auto-refresh (R8.1) surfaces it. A volume that already holds signals skips
#    ingest, so a restart with durable storage does not re-fetch. Routed through
#    deploy/scheduled_run.sh (U15) so first-load and a cron tick contend via the
#    SAME tick lock, not only via the per-step ingestion lock — a first-load run
#    still in flight when the schedule fires is a recorded skip, not a second
#    writer.
# 3. Start the cron scheduler (R3.1) so the dataset keeps refreshing — the
#    first-load gate above deliberately skips ingest on a populated durable
#    volume, so without a schedule that container never re-ingests. Best-effort:
#    a scheduler that will not start must not stop the app from serving.
# 4. Start uvicorn — reachable right away. It runs as a waited-on child
#    rather than replacing this shell via `exec`, so a SIGTERM relay to any
#    in-flight cron tick (U28, below) has somewhere to run before shutdown.
set -e

python -m app.db.load_seeds

if python -m app.first_load; then
  echo "first-load: empty feed — starting background ingest"
  sh deploy/scheduled_run.sh sh deploy/ingest_pipeline.sh > /tmp/gridsignals-ingest.log 2>&1 &
else
  echo "first-load: feed already populated — skipping ingest"
fi

# Start the scheduler. cron strips the environment — a scheduled job inherits
# nothing from this process, so GRIDSIGNALS_DB (a Dockerfile ENV) and any
# optional runtime var would be invisible to every tick — so first snapshot an
# EXPLICIT ALLOWLIST of the variables the crontab's jobs actually read (not the
# whole process environment: `export -p` would leak anything else set in this
# process, e.g. build/orchestration secrets that have nothing to do with a
# scheduled tick). The file is created empty and chmod'ed BEFORE anything is
# written, so no secret is ever readable at a laxer mode, and it lives outside
# the repo tree so nothing lands in a tracked file (R10.8). The audit judge
# (app/audit/config.py) is deliberately NOT scheduled (U26 — operator-invoked
# only), so its ANTHROPIC_API_KEY / GRIDSIGNALS_AUDIT_* vars are not cron's to
# see and are not in this allowlist.
#
# The whole sequence is best-effort, matching the `|| echo WARN` convention in
# deploy/ingest_pipeline.sh. This script runs under `set -e`, so an unguarded
# failure here — a read-only /etc, a future USER directive, a locked cron pidfile
# — would crash-loop the container because the SCHEDULER failed. A stale feed is
# bad; an unreachable app is worse. The && chain also means a failed chmod stops
# the snapshot from being written at all rather than leaving it world-readable.
GRIDSIGNALS_ENV_ALLOWLIST="GRIDSIGNALS_DB|GRIDSIGNALS_LOCK|GRIDSIGNALS_PIPELINE_LOCK|GRIDSIGNALS_HEARTBEAT|PORT"
CRON_PID=""
if : > /etc/gridsignals.env \
    && chmod 600 /etc/gridsignals.env \
    && { export -p | grep -E "^export (${GRIDSIGNALS_ENV_ALLOWLIST})=" >> /etc/gridsignals.env || true; } \
    && cron
then
  echo "scheduler: cron started (see deploy/crontab)"
  # Resolve cron's own pid from its pidfile — written slightly after `cron`
  # returns control here, hence the short retry loop — so the shutdown relay
  # below can find its live descendants (U28).
  n=0
  while [ -z "$CRON_PID" ] && [ "$n" -lt 20 ]; do
    CRON_PID="$(cat /run/crond.pid 2>/dev/null || true)"
    [ -n "$CRON_PID" ] && break
    n=$((n + 1))
    sleep 0.1
  done
  [ -z "$CRON_PID" ] && echo "WARN: could not resolve cron's pid — a cron tick in flight at shutdown will not get a clean lock release"
else
  echo "WARN: scheduler failed to start — the feed will not refresh on schedule"
fi

# tini -g SIGTERMs every member of this shell's own process group directly
# (this shell, uvicorn once started below), but cron puts EACH job it runs
# into its OWN freshly assigned session/process group at fork time, not a
# single group shared with the daemon (empirically confirmed against real
# Docker: `timeout`, which wraps every tick, re-groups itself yet again
# inside that — and MULTIPLE ticks can be live at once with none of them
# sharing a group). There is no static pgid to precompute and relay to — it
# does not exist until a job actually starts, and a new one is assigned
# every tick. So this walks cron's live descendants in /proc AT THE MOMENT
# SIGTERM arrives (never precomputed) and signals each one directly.
#
# This must run to completion BEFORE this shell exits — not backgrounded.
# tini (PID 1) exits the moment its tracked child exits, and the instant
# tini exits the kernel tears down the whole PID namespace, SIGKILLing
# everything still running. uvicorn shuts down on TERM in well under a
# second, so a version of this that backgrounded the walk and left uvicorn
# `exec`'d as the tracked child (the original design here) lost that race
# on every real-Docker run tried: the walk never got to finish, let alone
# the ticks it signalled get a chance to clean up. uvicorn therefore runs as
# a plain backgrounded child instead of replacing this shell via `exec`, and
# the trap below runs the walk AND waits for uvicorn before this shell —
# tini's actual tracked child — is allowed to exit.
relay_sigterm_to_cron_descendants() {
  # The startup retry loop above gives /run/crond.pid ~2s to appear; if it
  # somehow took longer than that (e.g. under heavy first-load ingest load),
  # CRON_PID would otherwise stay empty for the container's entire remaining
  # lifetime and this would silently no-op on every future SIGTERM. Retry the
  # read here too — cheap, and it's the one place that actually needs it.
  [ -n "$CRON_PID" ] || CRON_PID="$(cat /run/crond.pid 2>/dev/null || true)"
  [ -n "$CRON_PID" ] || return 0
  # Two passes on purpose: signalling a process re-parents its still-live
  # children to the subreaper (tini) immediately, severing the very ppid
  # chain a combined walk-and-signal loop would still need to find the rest
  # of the tree (confirmed against real Docker: a one-pass version only ever
  # reached cron's immediate child). Discover every live descendant first,
  # from one stable snapshot, THEN signal all of them.
  frontier="$CRON_PID"
  descendants=""
  while [ -n "$frontier" ]; do
    next=""
    for p in /proc/[0-9]*; do
      pid="${p#/proc/}"
      # Field 4 of /proc/PID/stat is ppid. Naive whitespace-split field
      # position — fragile if a process's comm (field 2, parenthesized)
      # ever contains a space, which would shift this. None of the processes
      # in this tree (cron, sh, timeout, python, uvicorn) do.
      ppid="$(awk '{print $4}' "$p/stat" 2>/dev/null)" || continue
      for f in $frontier; do
        if [ "$ppid" = "$f" ]; then
          next="$next $pid"
          break
        fi
      done
    done
    descendants="$descendants $next"
    frontier="$next"
  done
  for pid in $descendants; do
    # `|| true`: this script runs under `set -e`, and a descendant discovered
    # a moment ago (e.g. a short-lived cron helper) may have already exited
    # on its own by the time its turn comes up — an unguarded failure here
    # would abort this loop under `set -e` and leave the REST of the
    # discovered list unsignalled.
    kill -TERM "$pid" 2>/dev/null || true
  done
}

term_handler() {
  # Ignore a second TERM arriving mid-handler (e.g. a repeated `docker kill`)
  # rather than leaving that re-entrant behavior to the shell's default
  # (dash-implementation-dependent) — the walk below already handles every
  # descendant discovered on the first signal.
  trap '' TERM
  relay_sigterm_to_cron_descendants
  [ -n "$UVICORN_PID" ] && wait "$UVICORN_PID" 2>/dev/null || true
  exit 0
}
trap term_handler TERM

uvicorn app.ui_web.app:app --host 0.0.0.0 --port "${PORT}" &
UVICORN_PID=$!
wait "$UVICORN_PID"
