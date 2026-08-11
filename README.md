# GridReaper — Seed Data

Seed CSVs for the SQLite config layer (Requirements v3, §4). Load these into the corresponding tables at Stage 0. All hand-editable without code changes (R3.4).

## Files

| File | Table | Rows | Notes |
|---|---|---|---|
| `products.csv` | products | 16 | Microsoft security product catalog |
| `triggers.csv` | triggers | 21 | Trigger taxonomy w/ base_strength + decay_half_life_days + mvp_flag. **NEW: `nerc_enforcement`** (mvp_flag=1) |
| `indicator_map.csv` | indicator_map | 60 | trigger→product mappings w/ evidence_quality [IR/PC/VM] |
| `cip_product_map.csv` | cip_product_map | 11 | NERC CIP standard → product sub-mapping + outreach angle (drives nerc_enforcement cards) |
| `watchlist_entities.csv` | watchlist_entities | 171 | 130 EDGAR-visible w/ verified CIKs + 41 dark (co-op/muni/federal/RTO) |
| `license_matrix.csv` | license_matrix | ~55 | (in outputs root) MS licensing w/ play path, GCC status, source quality, verified_date |

## Data quality / provenance

- **CIKs are authoritative.** All 130 EDGAR-visible entities verified against `data.sec.gov/submissions/CIK*.json` on 2026-08-11. The other 41 are intentionally dark (no SEC filing) per the coverage-gap finding — `coverage_flag=dark`.
- **mvp_flag=1 triggers:** nerc_cip_revision, nerc_enforcement, tsa_security_directive, leadership_change. These are the regulatory + leadership set that classifies into cards at MVP. Everything else is ingest-and-store (Stage 2+).
- **`notes` column** flags stale entities to verify at build: ALLETE (private 2025), ChampionX (SLB acquisition), EnLink (ONEOK private), NextEra Energy Partners→XPLR Infrastructure (XIFR).
- **Empty columns by design:** `lei`, `wikidata_qid` (populate via GLEIF/Wikidata at Stage 0 entity-resolution build); `aliases`, `collision_terms` (hand-fill per R6.3 — start with high-collision names: Dominion, Chord, Range, Constellation); `parent_id` (fill subsidiary rollups); `owning_seller` (demo simulation).

## To do at Stage 0 (before ingestion)
1. Populate `lei` (GLEIF fuzzy search) and `wikidata_qid` for the 130 filers.
2. Hand-fill `collision_terms` for high-ambiguity names (R6.3).
3. Expand watchlist toward ~250 if desired: authoritative enumerations are EEI (IOUs), NRECA/NCB Co-op 100 (co-ops), APPA/LPPC (public power), Enverus Top 50 (E&P). Current 171 covers all high/medium-signal accounts; the tail is low-signal.
4. Load `license_matrix.csv` and run the R10.6 staleness check (all verified_date = 2026-08-11).

## Subsector codes
iou_electric, iou_gas, coop_gt, coop_transmission, coop_distribution, muni_public, public, state_owned, state_authority, federal, federal_pma, og_major, og_ep, midstream, lng, ofs, refiner, ipp, renewables, storage, rto_iso
