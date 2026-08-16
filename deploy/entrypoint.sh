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
#    ingest, so a restart with durable storage does not re-fetch.
# 3. Start the cron scheduler (R3.1) so the dataset keeps refreshing — the
#    first-load gate above deliberately skips ingest on a populated durable
#    volume, so without a schedule that container never re-ingests.
# 4. exec uvicorn — reachable right away.
set -e

python -m app.db.load_seeds

if python -m app.first_load; then
  echo "first-load: empty feed — starting background ingest"
  sh deploy/ingest_pipeline.sh > /tmp/gridsignals-ingest.log 2>&1 &
else
  echo "first-load: feed already populated — skipping ingest"
fi

# cron strips the environment: a scheduled job inherits nothing from this
# process, so GRIDSIGNALS_DB (a Dockerfile ENV) and any optional API key would be
# invisible to every tick. Snapshot the exported environment where the crontab
# can source it — created empty and chmod'ed BEFORE anything is written, so no
# secret is ever readable at a laxer mode, and outside the repo tree so nothing
# lands in a tracked file (R10.8).
: > /etc/gridsignals.env
chmod 600 /etc/gridsignals.env
export -p > /etc/gridsignals.env

# Backgrounded daemon (Debian cron forks); never blocks the web process.
cron

exec uvicorn app.ui_web.app:app --host 0.0.0.0 --port "${PORT}"
