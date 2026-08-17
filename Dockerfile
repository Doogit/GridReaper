# GridSignals (FastAPI + HTMX) as a self-contained image for Azure App Service.
#
# Demo mode = RUNTIME FIRST-LOAD INGEST. The image ships ONLY code + config seeds
# — no dataset is baked in, so builds are fast and network-free. On first load the
# entrypoint (deploy/entrypoint.sh) seeds the schema, then runs the ingest
# pipeline (deploy/ingest_pipeline.sh) in the BACKGROUND against live public feeds
# while the web app serves immediately; the feed fills in and the 120s auto-
# refresh (R8.1) surfaces it. A durable volume with existing signals skips ingest,
# so a restart does not re-fetch.
FROM python:3.12-slim

WORKDIR /app

# The in-container scheduler (R3.1). python:3.12-slim ships no cron binary, so
# deploy/crontab would otherwise be inert — the image would pass its file-content
# checks with nothing to run it. Debian's daemon is `cron`, not `crond`.
# tini is PID 1 (see ENTRYPOINT below): cron double-forks and orphans to PID 1,
# and without a real init/reaper the app process itself becomes PID 1 and a
# container stop SIGKILLs an in-flight tick before its lock cleanup can run.
# logrotate keeps /var/log/gridsignals-cron.log bounded in a long-lived
# container; its package wires a daily cron.daily hook automatically, so
# dropping deploy/logrotate.conf in below is all that's needed.
RUN apt-get update \
 && apt-get install -y --no-install-recommends cron tini logrotate \
 && rm -rf /var/lib/apt/lists/*

# The pipeline + data layer are stdlib-only; requirements.txt installs UI-only
# packages: the FastAPI + Uvicorn + Jinja2 stack the deployed app runs on.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Register the schedule. /etc/cron.d entries must be root-owned and mode 644, and
# their filename must contain no dot, or cron silently ignores them.
# `tr -d '\r'` because the build context is whatever working tree the build was
# handed (deploy/azure-deploy.ps1 uploads the local one): a CRLF crontab corrupts
# its trailing field and cron reports that only through syslog/mail — neither
# exists here — so the container would boot, serve, and never refresh.
# .gitattributes is the primary fix; this is the belt-and-braces one.
# Pipeline steps stay in deploy/ingest_pipeline.sh — never here (see docs on
# runbook drift from the canonical pipeline).
RUN tr -d '\r' < deploy/crontab > /etc/cron.d/gridsignals \
 && chmod 644 /etc/cron.d/gridsignals

# Log rotation config for the cron log (same CR-stripping belt-and-braces as
# the crontab above — the build context is whatever working tree the build was
# handed).
RUN tr -d '\r' < deploy/logrotate.conf > /etc/logrotate.d/gridsignals \
 && chmod 644 /etc/logrotate.d/gridsignals

ENV GRIDSIGNALS_DB=/app/data/gridsignals.db

# App Service routes to the port named by the WEBSITES_PORT app setting; keep it
# in sync with the port uvicorn binds (see deploy/azure-deploy.ps1).
ENV PORT=8000
EXPOSE 8000

# tini is PID 1, not the app or cron (see the apt-get RUN above): it reaps
# cron's double-forked orphans and forwards signals, so a container stop can
# actually SIGTERM an in-flight tick instead of SIGKILLing whatever
# double-forked its way to PID 1 without a chance to release its lock.
# Runtime bootstrap: seed schema + config (blocking), background-ingest on first
# load, start the cron scheduler, then exec uvicorn (bind 0.0.0.0 so App Service
# can reach it; Easy Auth, when enabled, fronts the app). Ingestion stays a
# backend process, never the UI (R3.1). See deploy/entrypoint.sh,
# deploy/ingest_pipeline.sh, deploy/crontab.
ENTRYPOINT ["tini", "--"]
CMD ["sh", "deploy/entrypoint.sh"]
