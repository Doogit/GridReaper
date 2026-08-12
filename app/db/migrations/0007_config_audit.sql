-- 0007_config_audit: provenance trail for operator config edits (R3.3, R8.7).
--
-- The Admin/Config page (R8.7) is the first Streamlit write path into seeded
-- config tables. R3.3 requires those edits be "explicitly audited": every field
-- an operator changes writes one row here, in the same transaction as the edit.
--
-- Runtime-only and never seeded (like source_runs / audit_runs); fully
-- regenerable by replaying operator edits. Append-only by convention - the write
-- helpers only ever INSERT, never UPDATE or DELETE - so it is an immutable log.
--
--   audit_id    autoincrement surrogate; monotonic insertion order
--   table_name  the edited config table (e.g. 'scoring_weights', 'triggers')
--   pk          the edited row's primary key. Single-column PKs store the bare
--               value; composite PKs (scoring_weights = weight_kind+key) store a
--               JSON object so the recent-changes panel can render them
--   field       the column that changed (e.g. 'weight', 'decay_half_life_days')
--   old_value   value before the edit (TEXT; NULL only if the row lacked one)
--   new_value   value after the edit
--   editor      who made the change; the constant 'operator' at MVP (no auth)
--   reason      optional operator-supplied note ('' when none)
--   ts          UTC ISO-8601 (R10.2) when the edit was committed
CREATE TABLE IF NOT EXISTS config_audit (
  audit_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  pk         TEXT NOT NULL,
  field      TEXT NOT NULL,
  old_value  TEXT,
  new_value  TEXT,
  editor     TEXT NOT NULL DEFAULT 'operator',
  reason     TEXT DEFAULT '',
  ts         TEXT NOT NULL
);

-- Recent-changes panel reads newest-first by ts.
CREATE INDEX IF NOT EXISTS idx_config_audit_ts ON config_audit(ts);
