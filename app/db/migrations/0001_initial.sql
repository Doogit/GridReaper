-- 0001_initial: GridReaper data model per requirements v3 §4.1.
-- Config tables are seeded from seeds/*.csv; column names for seeded columns
-- match the CSV headers. Runtime tables are created empty.

-- ---------------------------------------------------------------------------
-- Config layer (seeded)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS products (
  product_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  persona TEXT,
  licensing_model TEXT,
  keywords TEXT,
  energy_ot_flag INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS triggers (
  trigger_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT,
  base_strength INTEGER NOT NULL,
  decay_half_life_days INTEGER NOT NULL,
  mvp_flag INTEGER DEFAULT 0,
  evidence_quality TEXT,
  primary_sources TEXT,
  -- JSON array of allowed signal scopes (R7.2); hand-configured, not in CSV
  allowed_scopes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS indicator_map (
  trigger_id TEXT NOT NULL REFERENCES triggers(trigger_id),
  product_id TEXT NOT NULL REFERENCES products(product_id),
  evidence_quality TEXT,
  rationale TEXT,           -- seeded from indicator_map.csv "notes" column
  config_version TEXT DEFAULT '',
  PRIMARY KEY (trigger_id, product_id)
);

CREATE TABLE IF NOT EXISTS cip_product_map (
  cip_standard TEXT NOT NULL,
  topic TEXT,
  product_ids TEXT,
  outreach_angle TEXT,
  PRIMARY KEY (cip_standard)
);

CREATE TABLE IF NOT EXISTS watchlist_entities (
  entity_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  cik TEXT,
  lei TEXT,
  wikidata_qid TEXT,
  ticker TEXT,
  subsector TEXT,
  parent_id TEXT,
  richness TEXT,
  coverage_flag TEXT,
  gov_cloud_likelihood TEXT,
  -- R7.10 gov-cloud first-class fields
  tenant_cloud_environment TEXT DEFAULT 'unknown',
  cloud_evidence TEXT DEFAULT '',
  cloud_verified_at TEXT DEFAULT '',
  notes TEXT,
  owning_seller TEXT
);

-- Aliases/collision terms normalized out of watchlist_entities (R4.4);
-- seeded from the watchlist CSV's aliases / collision_terms cells when present.
CREATE TABLE IF NOT EXISTS entity_aliases (
  entity_id TEXT NOT NULL REFERENCES watchlist_entities(entity_id),
  alias TEXT NOT NULL,
  alias_type TEXT DEFAULT 'common',   -- legal/dba/ticker/common
  source TEXT DEFAULT '',
  verified_at TEXT DEFAULT '',
  PRIMARY KEY (entity_id, alias)
);

CREATE TABLE IF NOT EXISTS entity_collision_terms (
  entity_id TEXT NOT NULL REFERENCES watchlist_entities(entity_id),
  term TEXT NOT NULL,
  reason TEXT DEFAULT '',
  PRIMARY KEY (entity_id, term)
);

CREATE TABLE IF NOT EXISTS entity_relationships (
  parent_entity_id TEXT NOT NULL REFERENCES watchlist_entities(entity_id),
  child_entity_id TEXT NOT NULL REFERENCES watchlist_entities(entity_id),
  relationship_type TEXT NOT NULL,
  source TEXT DEFAULT '',
  verified_at TEXT DEFAULT '',
  PRIMARY KEY (parent_entity_id, child_entity_id, relationship_type)
);

CREATE TABLE IF NOT EXISTS source_policies (
  source_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  access_method TEXT,
  ttl INTEGER,                        -- seconds between fetches
  enabled INTEGER DEFAULT 1,
  tos_status TEXT DEFAULT '',
  evidence_rank INTEGER,              -- 1=primary disclosure .. 3=tracker/news
  rate_limit TEXT DEFAULT '',
  last_policy_review TEXT DEFAULT ''
);

-- Seeded staging copy of license_matrix.csv (columns = CSV headers).
-- The normalized license_facts / play tables below are populated from this
-- by a curated transform in the license-play build (gcc_high is freeform
-- text and cannot mechanically map to the segment enum).
CREATE TABLE IF NOT EXISTS license_matrix (
  product_id TEXT,
  tier TEXT,
  included_or_addon TEXT,
  addon_price_note TEXT,
  upgrade_path TEXT,
  gcc_high TEXT,
  source_quality TEXT,
  verified_date TEXT,
  source_url TEXT,
  PRIMARY KEY (product_id, tier)
);

-- ---------------------------------------------------------------------------
-- Runtime layer (empty at seed time)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS combo_rules (
  rule_id TEXT PRIMARY KEY,
  logic_expr TEXT,
  multiplier REAL,
  product_ids TEXT,
  enabled_stage INTEGER               -- NULL = disabled (combos off at MVP)
);

CREATE TABLE IF NOT EXISTS source_runs (
  run_id TEXT PRIMARY KEY,
  source_id TEXT REFERENCES source_policies(source_id),
  started_at TEXT,
  finished_at TEXT,
  status TEXT,
  records_seen INTEGER,
  records_new INTEGER,
  error_state TEXT,
  parser_version TEXT
);

CREATE TABLE IF NOT EXISTS raw_events (
  raw_event_id TEXT PRIMARY KEY,
  source_id TEXT REFERENCES source_policies(source_id),
  run_id TEXT REFERENCES source_runs(run_id),
  source_native_id TEXT,
  fetched_at TEXT,
  event_date TEXT,
  payload TEXT,
  url TEXT,
  canonical_url TEXT,
  etag TEXT,
  last_modified TEXT,
  content_hash TEXT,
  first_seen_at TEXT,
  last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS entity_match_decisions (
  raw_event_id TEXT REFERENCES raw_events(raw_event_id),
  entity_id TEXT REFERENCES watchlist_entities(entity_id),
  method TEXT,
  confidence REAL,
  matched_terms TEXT,
  rejected_terms TEXT,
  decision TEXT,                      -- auto/reviewed/rejected
  decided_by TEXT,
  ts TEXT
);

CREATE TABLE IF NOT EXISTS signals (
  signal_id TEXT PRIMARY KEY,
  raw_event_id TEXT REFERENCES raw_events(raw_event_id),
  entity_id TEXT REFERENCES watchlist_entities(entity_id),  -- nullable for sector scope
  signal_scope TEXT,                  -- account/parent/subsector/sector/regulatory_calendar
  trigger_id TEXT REFERENCES triggers(trigger_id),
  event_date TEXT,
  headline TEXT,
  evidence_snippet TEXT,
  source_url TEXT,
  confidence REAL,
  evidence_quality TEXT,
  incident_evidence_level TEXT,       -- confirmed/corroborated/unconfirmed_early_warning; NULL for non-incident
  customer_facing_allowed INTEGER DEFAULT 0,
  score REAL,
  status TEXT DEFAULT 'active'        -- active/decayed/dismissed
);

CREATE TABLE IF NOT EXISTS signal_evidence (
  signal_id TEXT NOT NULL REFERENCES signals(signal_id),
  raw_event_id TEXT REFERENCES raw_events(raw_event_id),
  evidence_text TEXT,
  evidence_locator TEXT,
  evidence_rank INTEGER,
  extraction_version TEXT
);

CREATE TABLE IF NOT EXISTS feedback (
  signal_id TEXT REFERENCES signals(signal_id),
  verdict TEXT,                       -- useful/not_useful/converted
  reason_code TEXT,                   -- required when not_useful (R9.1/R9.2)
  ts TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS audit (
  signal_id TEXT REFERENCES signals(signal_id),
  check_type TEXT,                    -- entity_match/classification/evidence_support/license_play
  result TEXT,                        -- pass/fail/unclear
  judge_notes TEXT,
  model_id TEXT,
  prompt_version TEXT,
  ts TEXT
);

CREATE TABLE IF NOT EXISTS audit_goldens (
  golden_id TEXT PRIMARY KEY,
  signal_fixture TEXT,
  expected_results TEXT,
  reviewed_by TEXT,
  reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS license_facts (
  fact_id TEXT PRIMARY KEY,
  product_id TEXT REFERENCES products(product_id),
  sku_or_plan TEXT,
  segment TEXT,                       -- commercial/gcc/gcc_high/dod/azure_gov/unknown
  price_note TEXT,
  included_or_addon TEXT,             -- suite/included/addon/standalone/consumption/site/free
  prerequisite TEXT,
  effective_date TEXT,
  verified_date TEXT,
  source_quality TEXT,                -- primary/non-primary
  source_url TEXT
);

CREATE TABLE IF NOT EXISTS license_play_candidates (
  play_id TEXT PRIMARY KEY,
  trigger_id TEXT REFERENCES triggers(trigger_id),
  product_id TEXT REFERENCES products(product_id),
  assumed_baseline TEXT,
  recommended_path TEXT,
  discovery_question TEXT,
  rank_rule TEXT,
  config_version TEXT
);

CREATE TABLE IF NOT EXISTS license_play_snapshots (
  signal_id TEXT NOT NULL REFERENCES signals(signal_id),
  play_id TEXT NOT NULL REFERENCES license_play_candidates(play_id),
  fact_ids TEXT,                      -- JSON array of license_facts.fact_id
  generated_at TEXT,
  generation_version TEXT,
  display_text TEXT,
  outreach_safe_text TEXT,
  PRIMARY KEY (signal_id, play_id)
);

CREATE TABLE IF NOT EXISTS review_queue (
  raw_event_id TEXT REFERENCES raw_events(raw_event_id),
  candidate_entity_id TEXT REFERENCES watchlist_entities(entity_id),
  reason TEXT,
  confidence REAL,
  created_at TEXT,
  disposition TEXT DEFAULT 'pending',
  disposed_at TEXT
);

CREATE TABLE IF NOT EXISTS regulatory_obligations (
  obligation_id TEXT PRIMARY KEY,
  source_url TEXT,
  regulator TEXT,
  rule_name TEXT,
  affected_scope TEXT,
  applicability_rule TEXT,
  effective_date TEXT,
  compliance_date TEXT,
  mapped_products TEXT,
  verified_at TEXT
);

CREATE TABLE IF NOT EXISTS facility_assets (
  facility_id TEXT PRIMARY KEY,
  source_id TEXT REFERENCES source_policies(source_id),
  source_native_id TEXT,
  facility_name TEXT,
  latitude REAL,
  longitude REAL,
  capacity_mw REAL,
  owner_operator_entity_id TEXT REFERENCES watchlist_entities(entity_id),
  ownership_source_url TEXT,
  facility_owner_confidence REAL,
  verified_at TEXT
);

-- ---------------------------------------------------------------------------
-- Indexes (R8.10) + dedupe support (R10.4)
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_signals_entity ON signals(entity_id);
CREATE INDEX IF NOT EXISTS idx_signals_trigger ON signals(trigger_id);
CREATE INDEX IF NOT EXISTS idx_signals_event_date_id ON signals(event_date, signal_id);
CREATE INDEX IF NOT EXISTS idx_signals_raw_event ON signals(raw_event_id);
CREATE INDEX IF NOT EXISTS idx_raw_events_source_fetched ON raw_events(source_id, fetched_at);
CREATE INDEX IF NOT EXISTS idx_raw_events_content_hash ON raw_events(content_hash);
CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_events_native
  ON raw_events(source_id, source_native_id)
  WHERE source_native_id IS NOT NULL AND source_native_id != '';
CREATE INDEX IF NOT EXISTS idx_source_runs_source ON source_runs(source_id);
CREATE INDEX IF NOT EXISTS idx_signal_evidence_signal ON signal_evidence(signal_id);
CREATE INDEX IF NOT EXISTS idx_match_decisions_raw_event ON entity_match_decisions(raw_event_id);
CREATE INDEX IF NOT EXISTS idx_review_queue_raw_event ON review_queue(raw_event_id);
CREATE INDEX IF NOT EXISTS idx_entity_aliases_entity ON entity_aliases(entity_id);
CREATE INDEX IF NOT EXISTS idx_feedback_signal ON feedback(signal_id);
CREATE INDEX IF NOT EXISTS idx_audit_signal ON audit(signal_id);
