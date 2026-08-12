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
signal feed is empty until the ingest pipeline runs. So the `Dockerfile` runs the
pipeline **at build time** against live public feeds (SEC EDGAR, Federal
Register, press-wire RSS, NERC pages, CISA KEV), classifies, scores, and bakes
the resulting signals into the image.

Consequences:

- **`az acr build` is slower and network-dependent for this repo** — it performs
  live fetches. Individual feeds are non-fatal (a flaky one is skipped), but the
  build asserts at least one signal was produced, so it fails loudly rather than
  shipping an empty feed.
- **The baked feed is a point-in-time snapshot** of whatever was live at build.
  Rebuild the image to refresh it. There is no scheduled ingestion in this demo
  packaging; production would run the ingest CLIs on a schedule against a durable
  database.
- `ANTHROPIC_API_KEY` is **not** required — the app runs without it; only the
  optional accuracy-audit judge uses it.

## What it serves, and the auth boundary

By default the app is **open** and serves signals derived from **public** events.
Before adding any private data or exposing it broadly, put sign-in in front of it.

### Add Entra sign-in

App Service "Easy Auth" gates the whole app behind Microsoft corporate sign-in
with no application code. After the app exists:

```powershell
az webapp auth microsoft update -g rg-gridsignals -n <app-name> `
    --client-id "<entra-app-client-id>" `
    --issuer "https://login.microsoftonline.com/<tenant-id>/v2.0"
az webapp auth update -g rg-gridsignals -n <app-name> `
    --enabled true --action RequireAuthentication --redirect-provider azureactivedirectory
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
