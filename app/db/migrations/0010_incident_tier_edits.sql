-- 0010_incident_tier_edits: append-only operator re-tier trail (R8.7, R10.5, R7.12).
--
-- The incident evidence-tier editor is the UI's first SIGNAL-STATE write. R10.5
-- evidence tiers are normally set by the classifier (app/classify/runner.py);
-- this table records every operator confirm/re-tier of an incident card as an
-- immutable event, written in the SAME transaction as the signals UPDATE. It is
-- the provenance trail R8.7/R3.3 require for an audited edit.
--
-- Runtime-only and never seeded (like config_audit / source_runs); fully
-- regenerable by replaying operator edits. Append-only by convention - the write
-- helper (data.retier_incident) only ever INSERTs here, never UPDATE/DELETE - so
-- it is an immutable log. UPDATE-in-place on the signals row (never delete +
-- reinsert) keeps snapshots/evidence that FK-reference the signal intact.
--
--   edit_id     autoincrement surrogate; monotonic insertion order
--   signal_id   the re-tiered signal (FK signals.signal_id; UPDATE-in-place)
--   old_level   incident_evidence_level before (a valid R10.5 tier)
--   new_level   incident_evidence_level after (a valid R10.5 tier)
--   old_cfa     customer_facing_allowed before (0/1)
--   new_cfa     customer_facing_allowed after, derived per R7.12
--   editor      who made the change; the constant 'operator' at MVP (no auth)
--   reason      optional operator-supplied note ('' when none)
--   ts          UTC ISO-8601 (R10.2) when the edit was committed
CREATE TABLE IF NOT EXISTS incident_tier_edits (
  edit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id  TEXT NOT NULL REFERENCES signals(signal_id),
  old_level  TEXT NOT NULL,
  new_level  TEXT NOT NULL,
  old_cfa    INTEGER NOT NULL,
  new_cfa    INTEGER NOT NULL,
  editor     TEXT NOT NULL DEFAULT 'operator',
  reason     TEXT DEFAULT '',
  ts         TEXT NOT NULL
);

-- Per-signal history reads newest-first by insertion order.
CREATE INDEX IF NOT EXISTS idx_incident_tier_edits_signal
  ON incident_tier_edits(signal_id, edit_id);
