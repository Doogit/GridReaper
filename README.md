# GridReaper

Monitor external events across US energy companies and flag when an account becomes more likely to buy a specific Microsoft security product — with a suggested play, license path, and draft outreach opener.

Stack: Python · SQLite · Streamlit. All data sources are free and read-only.

> **Status: early / foundation.** The database schema and seed loader are implemented and verified. Ingestion, entity resolution, scoring, license-play resolution, the UI, and the feedback/audit loop are **in progress** — see [Roadmap](#roadmap).

## How it works

GridReaper watches public signals about US energy companies — regulatory actions (NERC/FERC/TSA) and leadership changes (new CISO/CIO) — and turns them into scored, sourced signal cards. Each card maps the event to relevant Microsoft security products, resolves a licensing path, and drafts an outreach opener. Every claim carries a source link and an evidence-quality tag; nothing surfaces unsourced.

Design principles:

- **Read-only, free sources only.** SEC EDGAR, Federal Register, press-wire RSS, NERC/FERC pages. No paid feeds, no ToS-restricted scraping, no ML.
- **Evidence over noise.** Two low-volume-but-high-signal trigger types at MVP, confidence gating, and an automated accuracy audit rather than a firehose.
- **Config as data.** Products, triggers, mappings, watchlist, and the license matrix live as CSV seeded into SQLite — editable without code changes.

## What works today

- **Schema** — the full data model as idempotent SQLite tables (config + runtime layers) with query indexes.
- **Connection helper** — SQLite in WAL mode with foreign keys and a busy timeout, matching the single-writer / read-heavy architecture.
- **Seed loader** — idempotent, foreign-key-ordered load of the config data with a per-table row-count report. Safe to re-run.

## Getting started

Requires Python 3.11+ (standard library only — no third-party dependencies for the loader).

```bash
python -m app.db.load_seeds
```

This creates `data/gridreaper.db` (gitignored) and populates the config tables from `seeds/`. The command is idempotent — re-running refreshes rows rather than duplicating them.

## Architecture

```
[ingestion process (writer)] --> SQLite (WAL) <-- [Streamlit app (reader + feedback writes)]
        |                                                |
        +--> weekly audit job (Claude API judge) --------+--> audit table
```

Ingestion runs as a separate scheduled process. The app is read-only for all signal/event/config tables and writes only feedback. One source failing never blocks a run.

## Roadmap

The MVP targets two classified trigger types — regulatory actions and leadership changes — over a watchlist of US energy companies, surfaced as sourced signal cards with resolved license plays.

| Area | Status |
|---|---|
| SQLite schema + WAL connection | Implemented |
| Idempotent config/seed loader | Implemented |
| Entity resolution (CIK/LEI/QID anchoring; GLEIF/Wikidata enrichment; alias disambiguation) | In progress |
| Source ingestion (EDGAR, Federal Register, press-wire RSS, NERC/FERC pages) | In progress |
| Classification & scoring (rule-based; decay half-lives; account fit) | In progress |
| License-play resolution (incl. gov-cloud gating) | In progress |
| Streamlit UI (multi-page, dark SOC theme, card feed) | In progress |
| Feedback loop + automated accuracy audit (Claude judge) + precision reporting | In progress |

## Notes

Single-operator demo/portfolio project — no auth, no multi-tenancy. The only recurring cost is a capped Claude API audit judge; there are no paid data sources.
