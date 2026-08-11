-- GridReaper schema (Requirements v3, §4). Idempotent: CREATE ... IF NOT EXISTS.
-- Column names match the seed CSV headers exactly (verified at load pre-flight).

-- Config layer (seeded from CSVs)
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
  primary_sources TEXT
);

CREATE TABLE IF NOT EXISTS indicator_map (
  trigger_id TEXT NOT NULL REFERENCES triggers(trigger_id),
  product_id TEXT NOT NULL REFERENCES products(product_id),
  evidence_quality TEXT,
  notes TEXT,
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
  aliases TEXT,
  collision_terms TEXT,
  notes TEXT,
  owning_seller TEXT
);

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

-- Runtime layer (created empty now; populated in later sessions)
CREATE TABLE IF NOT EXISTS combo_rules (
  rule_id TEXT PRIMARY KEY,
  logic_expr TEXT,
  multiplier REAL,
  product_ids TEXT,
  enabled INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY,
  name TEXT,
  access_method TEXT,
  ttl INTEGER,
  enabled INTEGER DEFAULT 1,
  last_run TEXT,
  error_state TEXT,
  precision_30d REAL
);

CREATE TABLE IF NOT EXISTS raw_events (
  event_id TEXT PRIMARY KEY,
  source_id TEXT REFERENCES sources(source_id),
  fetched_at TEXT,
  payload TEXT,
  url TEXT
);

CREATE TABLE IF NOT EXISTS signals (
  signal_id TEXT PRIMARY KEY,
  entity_id TEXT REFERENCES watchlist_entities(entity_id),
  trigger_id TEXT REFERENCES triggers(trigger_id),
  event_date TEXT,
  headline TEXT,
  evidence_snippet TEXT,
  source_url TEXT,
  confidence REAL,
  evidence_quality TEXT,
  score REAL,
  license_plays TEXT,
  status TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS feedback (
  signal_id TEXT REFERENCES signals(signal_id),
  verdict TEXT,
  ts TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS audit (
  signal_id TEXT REFERENCES signals(signal_id),
  check_type TEXT,
  result TEXT,
  judge_notes TEXT,
  model_id TEXT,
  ts TEXT
);

CREATE TABLE IF NOT EXISTS review_queue (
  raw_event_id TEXT,
  candidate_entity_id TEXT,
  match_confidence REAL,
  status TEXT DEFAULT 'pending'
);

-- Indexes (per v3 R8.10)
CREATE INDEX IF NOT EXISTS idx_signals_entity ON signals(entity_id);
CREATE INDEX IF NOT EXISTS idx_signals_trigger ON signals(trigger_id);
CREATE INDEX IF NOT EXISTS idx_signals_event_date ON signals(event_date);
CREATE INDEX IF NOT EXISTS idx_raw_events_source ON raw_events(source_id);
