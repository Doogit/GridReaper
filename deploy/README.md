# Deploy to Azure App Service

Runs GridSignals as a container on **Azure App Service for Containers**. The
image is built inside Azure, so **no local Docker is required** — only the
Azure CLI.

## One command

```powershell
az login
az account set -s "<your-subscription>"
./deploy/azure-deploy.ps1
```

The script creates a resource group, an Azure Container Registry, builds the
image with `az acr build`, provisions a Linux App Service plan + web app, wires
up managed-identity pull, WebSockets, and the container port, and prints the URL.

## How the demo gets its data (important)

Unlike a typical seed-and-go app, GridSignals ships only **config seeds** — the
signal feed is empty until the ingest pipeline runs. The image is **fetch-free at
build**; instead the container populates the dataset **on first load** at
runtime. `deploy/entrypoint.sh` seeds the schema (fast, offline), then — if the
feed is empty — runs `deploy/ingest_pipeline.sh` in the **background** against
live public feeds (SEC EDGAR, Federal Register, press-wire RSS, NERC pages, CISA
KEV) while the web app serves immediately. The feed fills in over ~1–2 minutes
and the 120s feed auto-refresh surfaces it.

Consequences:

- **`az acr build` is fast and deterministic** — the build no longer fetches. The
  live network dependency moves to the first container start.
- **First load does the fetch.** The app is reachable right away with an empty
  feed that populates in the background; a flaky feed just yields fewer cards (the
  UI shows honest empty states rather than failing). Watch `docker logs` — the
  background ingest writes to `/tmp/gridsignals-ingest.log` inside the container.
- **Skips re-fetch when data already exists.** The first-load check
  (`app.first_load`) runs ingest only when the signals table is empty. Point
  `GRIDSIGNALS_DB` at a durable volume (Azure Files) and a restart reuses the
  existing dataset instead of re-fetching. Without a durable volume, each fresh
  container is a first load and re-ingests. Scheduled refresh (an in-container
  cron over the same `deploy/ingest_pipeline.sh`) is the planned follow-up.
- `ANTHROPIC_API_KEY` is **not** required — the app runs without it; only the
  optional accuracy-audit judge uses it.

## What it serves, and the auth boundary

By default the app is **open** and serves signals derived from **public** events.
Before adding any private data or exposing it broadly, put sign-in in front of it.

### Add Entra sign-in

App Service "Easy Auth" gates the whole app behind Microsoft corporate sign-in
with no application code. After the app exists:

```powershell
az extension add --name authV2
az webapp auth microsoft update -g rg-gridsignals -n <app-name> `
    --client-id "<entra-app-client-id>" `
    --issuer "https://login.microsoftonline.com/<tenant-id>/v2.0"
az webapp auth update -g rg-gridsignals -n <app-name> `
    --enabled true `
    --unauthenticated-client-action RedirectToLoginPage `
    --redirect-provider AzureActiveDirectory
```

## Notes / limitations

- **Untested against a live subscription from this repo checkout.** The build
  pipeline is verified end-to-end locally (produces signals + license plays); the
  `az` deploy commands assume a recent Azure CLI.
- **State is ephemeral and demo-only.** The SQLite store is baked into the image;
  feedback written through the running app persists only until the container
  restarts. For a real deployment, point `GRIDSIGNALS_DB` at durable storage
  (Azure Files) and run ingestion as a scheduled job.
- **Single container, always-on B1 plan.** For a rarely-used demo you can switch
  the plan SKU to `F1` (free, but no Always On → cold starts).
