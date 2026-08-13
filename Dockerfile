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

# The pipeline + data layer are stdlib-only; requirements.txt installs UI-only
# packages: the FastAPI + Uvicorn + Jinja2 stack the deployed app runs on.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV GRIDSIGNALS_DB=/app/data/gridsignals.db

# App Service routes to the port named by the WEBSITES_PORT app setting; keep it
# in sync with the port uvicorn binds (see deploy/azure-deploy.ps1).
ENV PORT=8000
EXPOSE 8000

# Runtime bootstrap: seed schema + config (blocking), background-ingest on first
# load, then exec uvicorn (bind 0.0.0.0 so App Service can reach it; Easy Auth,
# when enabled, fronts the app). Ingestion stays a backend process, never the UI
# (R3.1). See deploy/entrypoint.sh + deploy/ingest_pipeline.sh.
CMD ["sh", "deploy/entrypoint.sh"]
