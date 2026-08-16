#!/bin/sh
# Lock guard for scheduled (cron) runs — R3.1, R3.2.
#
# The single-writer ingestion lock (app/ingest/runner.py) RAISES on a live lock
# rather than skipping, and deploy/ingest_pipeline.sh runs under `set -e`. So a
# cron tick landing during a manual or first-load ingest would abort the
# scheduled run mid-pipeline with a RuntimeError. This wrapper turns that into a
# clean, dated, recorded skip (exit 0): the cron log says why the tick did
# nothing, and the next tick runs normally.
#
# A lock older than the runner's own staleness window is presumed abandoned —
# the runner breaks it itself — so the guard must NOT skip forever on the
# residue of a crashed run. That would be the very "never re-ingests" defect the
# scheduler exists to close.
#
# Usage: sh deploy/scheduled_run.sh <command> [args...]
# Env:   GRIDSIGNALS_LOCK  lock path (default data/.ingest.lock)
set -e

LOCK="${GRIDSIGNALS_LOCK:-data/.ingest.lock}"
STALE_MINUTES=120        # mirrors LOCK_STALE_S (2h) in app/ingest/runner.py

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }   # UTC ISO-8601 (R10.2)

if [ -e "$LOCK" ]; then
  if [ -n "$(find "$LOCK" -mmin "+$STALE_MINUTES" 2>/dev/null)" ]; then
    echo "$(now) scheduled-run: stale ingestion lock ($LOCK) — proceeding"
  else
    echo "$(now) scheduled-run: skipped — ingestion lock held ($LOCK)"
    exit 0
  fi
fi

echo "$(now) scheduled-run: starting $*"
"$@"
