-- 0003_classification: classifier bookkeeping (R3.7).
-- classified_events records which raw_events each (classifier, parser_version)
-- has processed: re-running at the same version is incremental, bumping the
-- version reprocesses history. Deterministic signal ids make reprocessing
-- emit nothing new unless the rules changed.
--
-- triggers.allowed_scopes (R7.2) is populated by the seed loader
-- (app/db/load_seeds.py TRIGGER_SCOPES), not here: migrations run before the
-- triggers CSV loads, so an UPDATE in this file would be a silent no-op on a
-- fresh-from-seeds rebuild.

CREATE TABLE IF NOT EXISTS classified_events (
  raw_event_id TEXT NOT NULL REFERENCES raw_events(raw_event_id),
  classifier_id TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  processed_at TEXT NOT NULL,
  signals_emitted INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (raw_event_id, classifier_id, parser_version)
);

CREATE INDEX IF NOT EXISTS idx_classified_events_classifier
  ON classified_events(classifier_id, parser_version);
