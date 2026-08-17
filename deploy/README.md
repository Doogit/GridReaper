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
  container is a first load and re-ingests.
- **Scheduled refresh keeps a durable volume current.** Because first load skips
  a populated volume, a scheduler is what stops that container from freezing at
  its first boot. `deploy/crontab` (installed to `/etc/cron.d/gridsignals`) runs
  the same `deploy/ingest_pipeline.sh` daily at **03:17 UTC**; the daemon starts
  in `deploy/entrypoint.sh` before uvicorn, so it never blocks serving. See
  [Scheduled refresh](#scheduled-refresh) below.
- `ANTHROPIC_API_KEY` is **not** required — the app runs without it; only the
  optional accuracy-audit judge uses it.

## Scheduled refresh

An in-container cron daemon re-runs the canonical pipeline so the dataset does
not freeze at first boot (R3.1).

| Piece | Role |
|---|---|
| `deploy/crontab` | The schedule. Installed by the Dockerfile to `/etc/cron.d/gridsignals` (mode 644 — cron ignores anything else). |
| `deploy/scheduled_run.sh` | Lock guard. Serializes scheduled ticks against each other, and records a dated skip (exit 0) rather than aborting mid-pipeline when a manual run already holds the ingestion lock. |
| `deploy/entrypoint.sh` | Starts `cron` before `exec uvicorn`, so the scheduler never blocks the web process. |
| `deploy/ingest_pipeline.sh` | The one canonical step list, shared with the first-load path. The crontab must never inline its own copy. |

Two container facts the schedule has to work around:

- **cron strips the environment.** A scheduled job inherits nothing from the
  container, so `GRIDSIGNALS_DB` and any optional API key would be invisible to
  every tick. The entrypoint snapshots the exported environment to
  `/etc/gridsignals.env` (mode 600, outside the repo tree) and each crontab line
  sources it first. No secret is ever written into a tracked file.
- **A tick can collide with another run.** `deploy/scheduled_run.sh` holds a
  tick-scoped lock for the whole run, so two scheduled ticks can never overlap,
  and it probes the per-step ingestion lock at tick start so a manual run already
  in flight yields a clean skip. A lock older than 2h is treated as abandoned, so
  a crashed run cannot wedge the schedule permanently.
- **Line endings matter more than usual here.** `az acr build` uploads the local
  working tree as the build context. A CRLF shell script fails loudly, but a CRLF
  `/etc/cron.d` entry just never fires — cron reports that only via syslog/mail,
  neither of which exists in this image. `.gitattributes` pins these files to LF
  and the Dockerfile strips CRs on install.

Scheduled output goes to `/var/log/gridsignals-cron.log` inside the container.

Known gap, not closed here: the R3.2 ingestion lock is acquired and released
**per step**, so the guard's single probe cannot stop a manual run started
mid-tick from interleaving with a scheduled one. Serializing that needs a lock
the Python steps honor. Routing the first-load ingest through the guard is the
related follow-up, deliberately left alone so this change cannot regress
first-load behavior.

**Not scheduled: the annual entity-identifier refresh (R4.2).**
`app/enrich_entities.py` writes reviewable seed CSVs and never writes the store —
generated fills are reviewed in a PR by design, and in a container those CSVs
land on ephemeral storage and are never loaded. It stays an operator-run refresh:
`python -m app.enrich_entities` → review the diff → `python -m app.db.load_seeds`.

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

- **Untested against a live subscription from this repo checkout.** The runtime
  ingest pipeline's ordering is covered by local packaging tests; the `az`
  deploy commands assume a recent Azure CLI.
- **State is ephemeral and demo-only by default.** The SQLite store is created at
  container startup inside the container filesystem; first-load signals and
  feedback persist only until the container is replaced. For a real deployment,
  point `GRIDSIGNALS_DB` at durable storage (Azure Files); the scheduled refresh
  above then keeps that volume current.
- **Single container, always-on B1 plan.** For a rarely-used demo you can switch
  the plan SKU to `F1` (free, but no Always On → cold starts).
