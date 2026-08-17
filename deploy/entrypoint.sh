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
# 4. exec uvicorn — reachable right away.
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
GRIDSIGNALS_ENV_ALLOWLIST="GRIDSIGNALS_DB|GRIDSIGNALS_LOCK|GRIDSIGNALS_PIPELINE_LOCK|PORT"
if : > /etc/gridsignals.env \
    && chmod 600 /etc/gridsignals.env \
    && { export -p | grep -E "^export (${GRIDSIGNALS_ENV_ALLOWLIST})=" >> /etc/gridsignals.env || true; } \
    && cron
then
  echo "scheduler: cron started (see deploy/crontab)"
else
  echo "WARN: scheduler failed to start — the feed will not refresh on schedule"
fi

exec uvicorn app.ui_web.app:app --host 0.0.0.0 --port "${PORT}"
