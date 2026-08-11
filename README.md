# GridSignals

Monitor external events across US energy companies and flag when an account becomes more likely to buy a specific Microsoft security product — with a suggested play, license path, and draft outreach opener.

Stack: Python · SQLite · Streamlit. All data sources are free and read-only.

> **Status: early / foundation.** The database schema and seed loader are implemented and verified. Ingestion, entity resolution, scoring, license-play resolution, the UI, and the feedback/audit loop are **in progress** — see [Roadmap](#roadmap).

## How it works

GridSignals ingests a broad set of free, public signals about US energy companies and turns the highest-signal events into scored, sourced cards. Each card maps an event to relevant Microsoft security products, resolves a licensing path, and drafts an outreach opener. Every claim carries a source link and an evidence-quality tag; nothing surfaces unsourced.

At MVP, two trigger types are **classified into cards** — regulatory actions (NERC/FERC/TSA) and leadership changes (new CISO/CIO/CTO). A wider set of sources — cyber incidents, ransomware activity, known-exploited vulnerabilities, and global news — is **ingested and stored** to build historical backfill, so later stages classify against history instead of starting cold.

Design principles:

- **Read-only, free sources only.** No paid feeds, no ToS-restricted scraping, no ML.
- **Evidence over noise.** Confidence gating and an automated accuracy audit rather than a firehose; low-volume, high-signal triggers are classified first.
- **Config as data.** Products, triggers, mappings, watchlist, and the license matrix live as CSV seeded into SQLite — editable without code changes.

## Data sources

The MVP target source set — all free and accessed read-only (GET / RSS / JSON / bulk download). Ingestion is in progress; see [Roadmap](#roadmap).

| Source | Role |
|---|---|
| SEC EDGAR — 8-K / 10-K filings + submissions API | Classified — leadership + regulatory |
| Federal Register API (FERC, TSA) | Classified — regulatory |
| Press-wire RSS (PR Newswire, GlobeNewswire) | Classified — leadership |
| NERC / FERC public pages | Classified — regulatory |
| GDELT global news | Stored for backfill (later-stage classification) |
| CISA KEV + NVD | Stored — known-exploited vulnerabilities |
| Ransomware trackers (ransomware.live / RansomLook) | Stored — incident early-warning |
| EIA API | Enrichment — facility geo/capacity for the map |
| GLEIF + Wikidata | Entity resolution — LEI / QID anchoring |

## What works today

- **Schema + migrations** — the full data model (config + runtime layers, query indexes) managed by a versioned, checksummed migration runner. Applied migrations are tamper-guarded.
- **Connection helper** — SQLite in WAL mode with foreign keys and a busy timeout, matching the single-writer / read-heavy architecture.
- **Seed loader** — idempotent, foreign-key-ordered load of the config data with a per-table row-count report. Safe to re-run; never clobbers runtime-managed state (e.g. a source disabled by the operator stays disabled).
- **Source policy registry** — the MVP source inventory seeded with per-source access method, poll interval, ToS status, evidence rank, and rate-limit notes.
- **Entity resolution core** — deterministic CIK/ticker/LEI/alias matching with a fuzzy-name fallback. Known-collision names (e.g. bare "Dominion") never auto-match without corroborating context; ambiguous or low-confidence results go to a review queue instead of firing, and every match decision is logged with its terms and parser version. Covered by an adversarial test fixture set (collisions, subsidiaries, abbreviations, near-twins).
- **Entity enrichment** — an annual-refresh job that anchors the watchlist to external identifiers: Wikidata queried by SEC CIK (deterministic, one batch) for QIDs and LEIs, GLEIF fulltext as fallback accepted only on exact normalized-name match, plus GLEIF parent/child relationship import. Results are generated into reviewable seed CSVs; hand-verified values always win over generated ones.

## Getting started

Requires Python 3.11+ (standard library only — no third-party dependencies for the loader).

```bash
python -m app.db.load_seeds
```

This creates `data/gridsignals.db` (gitignored) and populates the config tables from `seeds/`. The command is idempotent — re-running refreshes rows rather than duplicating them.

## Architecture

```
[ingestion process (writer)] --> SQLite (WAL) <-- [Streamlit app (reader + feedback writes)]
        |                                                |
        +--> weekly audit job (Claude API judge) --------+--> audit table
```

Ingestion runs as a separate scheduled process. The app is read-only for all signal/event/config tables and writes only feedback. One source failing never blocks a run.

## Roadmap

The MVP classifies two trigger types — regulatory actions and leadership changes — into cards over a watchlist of US energy companies, while ingesting a broader signal set to build backfill for later stages.

| Area | Status |
|---|---|
| SQLite schema + migrations + WAL connection | Implemented |
| Idempotent config/seed loader + source policy registry | Implemented |
| Entity resolution core (deterministic + fuzzy matching, collision guard, review queue, decision log) | Implemented |
| Entity enrichment (GLEIF LEI / Wikidata QID population, parent/child relationships) | Implemented |
| Classified ingestion (EDGAR, Federal Register, press-wire RSS, NERC/FERC pages) | In progress |
| Store-only ingestion for backfill (GDELT news, CISA KEV/NVD, ransomware trackers) + enrichment (EIA) | In progress |
| Classification & scoring (rule-based; decay half-lives; account fit) | In progress |
| License-play resolution (incl. gov-cloud gating) | In progress |
| Streamlit UI (multi-page, dark SOC theme, card feed) | In progress |
| Feedback loop + automated accuracy audit (Claude judge) + precision reporting | In progress |

Later stages add incident/combo classification, GDELT-based classification, and a hiring/macro-trend layer.

## Notes

Single-operator demo/portfolio project — no auth, no multi-tenancy. The only recurring cost is a capped Claude API audit judge; there are no paid data sources.
