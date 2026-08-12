# GridSignals

Monitor external events across US energy companies and flag when an account becomes more likely to buy a specific Microsoft security product — with a suggested play, license path, and draft outreach opener.

Stack: Python · SQLite · Streamlit. All data sources are free and read-only.

> **Status: signal pipeline working end-to-end.** The database schema, seed loader, entity resolution, the core ingestion layer (EDGAR, Federal Register, press-wire RSS, NERC pages, CISA KEV), normalized license facts, rule-based classification & scoring, and immutable license-play snapshots with gov-cloud gating are implemented and verified against the stored 12-month backfill. The **Streamlit UI** (signal feed, review queue, account 360, feedback/precision, admin/config) is implemented as a rule-based MVP, now with a **feedback loop, a capped Claude API accuracy-audit judge, precision reporting, and an audited Admin config surface for tuning weights, half-lives, and source policies** — see [Roadmap](#roadmap).

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
- **Seed loader** — idempotent, foreign-key-ordered load of the config data with a per-table row-count report. Safe to re-run; never clobbers runtime-managed state (e.g. a source disabled by the operator, or a scoring weight / decay half-life tuned in Admin, survives a reload — while a fresh rebuild-from-seeds restores the pristine baseline).
- **Source policy registry** — the MVP source inventory seeded with per-source access method, poll interval, ToS status, evidence rank, and rate-limit notes.
- **Entity resolution core** — deterministic CIK/ticker/LEI/alias matching with a fuzzy-name fallback. Known-collision names (e.g. bare "Dominion") never auto-match without corroborating context; ambiguous or low-confidence results go to a review queue instead of firing, and every match decision is logged with its terms and parser version. Covered by an adversarial test fixture set (collisions, subsidiaries, abbreviations, near-twins).
- **Entity enrichment** — an annual-refresh job that anchors the watchlist to external identifiers: Wikidata queried by SEC CIK (deterministic, one batch) for QIDs and LEIs, GLEIF fulltext as fallback accepted only on exact normalized-name match, plus GLEIF parent/child relationship import. Results are generated into reviewable seed CSVs; hand-verified values always win over generated ones.
- **Ingestion layer** — a shared runner (per-source policy checks, TTL skips, run bookkeeping, idempotent native-id/content-hash dedupe, per-source error containment, single-writer lock) plus five live fetchers: SEC EDGAR submissions (8-K/10-K per watchlist CIK), Federal Register (FERC + TSA documents), press-wire RSS (PR Newswire, GlobeNewswire), NERC standards-page snapshots, and the CISA KEV catalog. A 12-month backfill (~5,200 raw events, local — the database is gitignored and rebuildable) is stored and re-runs dedupe to zero.
- **License facts + play candidates** — the hand-verified license matrix normalized into per-segment `license_facts` (commercial + GCC High, with a conservative, lossless mapping of the freeform gov-cloud notes) and one conditional license-play candidate per trigger→product mapping. Rebuild is deterministic from seeded config.
- **Classification & scoring (rule-based MVP)** — a classifier framework (entity resolution with review-queue gating, parent rollup, deterministic signal ids, per-version bookkeeping so re-runs are incremental and rule changes reprocess history) plus two precision-first classifiers: leadership changes (8-K Item 5.02 + press-wire appointment grammar, security-relevant titles only) and regulatory actions (Federal Register FERC/TSA rules with a required compliance-clock anchor; NERC standards-page diffs). Scores follow `base_strength × 0.5^(age/half-life) × account_fit × scope_fit` with operator-tunable weights seeded from CSV; stale signals decay automatically. Every signal carries ranked evidence rows — nothing surfaces unsourced.
- **License-play snapshots + gov-cloud gating (rule-based MVP)** — each signal gets immutable play snapshots pinning the licensing evidence basis (fact ids, display text, outreach-safe text) at generation time, so old cards stay explainable after licensing data changes. Outreach text never states non-primary prices, never asserts the account's current tier, and stays sector-phrased for sector-wide events. Security Copilot plays are suppressed for known/likely US government cloud tenants.
- **Streamlit UI (rule-based MVP, five pages)** — a dark-themed multi-page app. A **Signal Feed** of custom HTML/CSS cards (severity strip, decay bar, the R7.3 score decomposition `2.34 = 5 x 0.85 x 1.00 x 0.55`, evidence/scope/coverage badges, product and license-play chips, expandable sourced evidence, an outreach draft shown only when customer-facing-allowed, and useful/not-useful feedback with reason codes), scope-separated so account cards sit above a labeled sector/regulatory divider (R7.2), with keyset pagination and a status filter. A **Review Queue / Triage** page (pending entity matches with accept/reject, per-source health that distinguishes error / never-run / stale / disabled, and a stale-license-fact list). An **Account 360** page (identifiers, relationships, gov-cloud posture, timeline and signal cards). The **Feedback / Precision** and **Admin / Config** pages are described in their own entries below. The app is read-mostly — it writes only feedback, review dispositions, human match decisions, and explicitly-audited Admin config edits (R8.7, below); cards read immutable snapshots, never live facts (R7.6), and non-primary prices never reach the UI (R4.3). Score components are persisted by `rescore()` and a `supersede` pass flips a superseded proposal (docket-overlap only) out of the default feed. Empty and dark surfaces read "low-volume by design," never "broken" (R6.6).
- **Feedback loop + accuracy audit + precision reporting (rule-based MVP + capped Claude judge)** — cards capture useful/not-useful feedback with reason codes; a separate **audit judge** samples recent signals and asks a capped Claude model (default Haiku 4.5, called over `urllib` — no SDK) four *objective* checks per card (entity match, classification, evidence support, license-play support). The judge never rates usefulness and never changes weights, mappings, or facts; every verdict is versioned (model id + prompt version + parser version). It reads `ANTHROPIC_API_KEY` from the environment and enforces a per-run budget ceiling — with no key or an exhausted budget it records a skipped run and exits cleanly, never blocking ingestion or fabricating confidence. A **golden set** gates prompt/model changes, and a **Feedback / Precision** page reports human useful-rate and auto-accuracy by trigger/source/scope/tier, reason-code distribution, judge-vs-human disagreement, half-life effectiveness, and the G1/G2 gate status — every rate shown with its sample size, and labeled QA precision, explicitly **not** validated sales lift.
- **Admin / Config (tuning slice, R8.7)** — an operator page that makes scoring tunable without code changes: per-row **weight** and per-trigger **decay half-life** editors (each save re-runs scoring on active cards only, so decayed and dismissed cards keep their frozen score decomposition), **source enable/disable** with the report-only Gate G2 demotion recommendation shown alongside, and read-only `verified_date` staleness flags (>180 days). Every edit is recorded to an append-only `config_audit` provenance trail (R3.3); the hand-verified seed CSVs are never touched, so operator tuning survives a `load_seeds` reload while a fresh rebuild-from-seeds still returns the pristine baseline. Watchlist, alias, license-fact, and incident evidence-tier editors are the next slice.

## Getting started

Requires Python 3.11+. The pipeline and data layer are standard-library only; the UI adds a single dependency, **Streamlit** (`requirements.txt`).

```bash
pip install -r requirements.txt   # Streamlit (UI only); everything else is stdlib
python -m app.db.load_seeds       # create data/gridsignals.db + config tables
python -m app.licensing           # normalize license facts + play candidates
python -m unittest discover -s tests   # hermetic tests, no network
streamlit run app/ui/Home.py      # launch the UI: Signal Feed / Review Queue / Account 360 / Precision / Admin
```

This creates `data/gridsignals.db` (gitignored) and populates the config layer from `seeds/`. All commands are idempotent and safe to re-run. A fresh clone has no event data yet, so the feed reads "low-volume by design" until you build a backfill (below).

A fresh clone starts with **no event data** — the raw-event backfill referenced below lives in the (gitignored) local database, not the repo. To build your own and see real signals end-to-end, run the pipeline (live fetches; free, read-only, polite):

```bash
python -m app.ingest.edgar                          # SEC EDGAR submissions
python -m app.ingest.federal_register               # FERC + TSA documents
python -m app.ingest.presswire --source prnewswire  # press-wire RSS
python -m app.ingest.presswire --source globenewswire
python -m app.ingest.nerc_pages                     # NERC page snapshots
python -m app.ingest.cisa_kev                       # CISA KEV (store-only)

python -m app.classify.leadership                   # offline from here on
python -m app.classify.regulatory
python -m app.scoring
python -m app.plays

python -m app.audit.judge                           # accuracy audit (needs ANTHROPIC_API_KEY;
                                                    #   no key / over budget -> records a skipped run, exits 0)
python -m app.audit.goldens                         # golden-set regression check (needs a key)
```

Signals land in the `signals` table with ranked evidence in `signal_evidence`; each signal's license plays are pinned in `license_play_snapshots`. View them in the UI with `streamlit run app/ui/Home.py`.

## Architecture

```
[ingestion process (writer)] --> SQLite (WAL) <-- [Streamlit app (reader + feedback / review / config writes)]
        |                                                |
        +--> audit job (Claude API judge) ---------------+--> audit table
```

Ingestion runs as a separate scheduled process. The app never writes signal or event tables; its only writes are feedback, review dispositions, human match decisions, and audited Admin config edits (each takes the same single-writer lock as ingestion). One source failing never blocks a run.

## Roadmap

The MVP classifies two trigger types — regulatory actions and leadership changes — into cards over a watchlist of US energy companies, while ingesting a broader signal set to build backfill for later stages.

| Area | Status |
|---|---|
| SQLite schema + migrations + WAL connection | Implemented |
| Idempotent config/seed loader + source policy registry | Implemented |
| Entity resolution core (deterministic + fuzzy matching, collision guard, review queue, decision log) | Implemented |
| Entity enrichment (GLEIF LEI / Wikidata QID population, parent/child relationships) | Implemented |
| Ingestion runner (dedupe, run bookkeeping, error containment, single-writer lock) | Implemented |
| Ingestion: EDGAR, Federal Register, press-wire RSS, NERC pages, CISA KEV | Implemented |
| Store-only ingestion for backfill (GDELT news, NVD, ransomware trackers) + enrichment (EIA) | In progress |
| License facts + play candidates (normalized from the license matrix) | Implemented |
| Classification & scoring (rule-based; decay half-lives; account fit) | Implemented |
| License-play snapshots + gov-cloud gating | Implemented |
| Streamlit UI (multi-page dark theme; signal feed, review queue, account 360) | Implemented |
| Feedback loop + automated accuracy audit (Claude judge) + precision reporting | Implemented |
| Admin / Config (weight + half-life tuning, source enable/disable, staleness, config audit trail) | Implemented (tuning slice; watchlist / alias / license-fact / incident-tier editors next) |

Later stages add incident/combo classification, GDELT-based classification, and a hiring/macro-trend layer.

## Notes

Single-operator demo/portfolio project — no auth, no multi-tenancy. The only recurring cost is the Claude API accuracy-audit judge, held down by a per-run budget ceiling (config; a normal run is well under a cent) and gated on the `ANTHROPIC_API_KEY` env var — with no key it simply skips. There are no paid data sources.
