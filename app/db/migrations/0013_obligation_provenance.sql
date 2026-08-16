-- 0013_obligation_provenance: tie every derived obligation to its signal (R8.3, R4.1).
--
-- regulatory_obligations has existed since 0001 with ZERO rows and no writer.
-- app/obligations.py is its first producer: it derives a compliance-clock row
-- from a stored regulatory signal + that signal's raw Federal Register payload.
--
-- The 0001 columns record the obligation (regulator, rule_name, dates, scope)
-- but nothing records WHICH stored row it was derived from. source_url points
-- at the regulator's document, not at the signal, so an operator could not ask
-- "show me the card behind this deadline" and the no-fabrication rule had no
-- FK-enforced anchor. Two nullable columns close that:
--
--   signal_id   the signal the obligation was derived from (FK signals). NULL
--               is allowed only so ADD COLUMN is legal on a populated table -
--               the producer always writes it, and a NULL row would mean a
--               hand-inserted obligation with no derivation trail.
--   derived_at  UTC ISO-8601 (R10.2) when the producer wrote the row.
--
-- verified_at (0001) is deliberately NOT reused for derived_at: it means an
-- operator verified the obligation, and the producer verifies nothing. Derived
-- rows leave it NULL, which is the honest reading.
ALTER TABLE regulatory_obligations ADD COLUMN signal_id TEXT REFERENCES signals(signal_id);
ALTER TABLE regulatory_obligations ADD COLUMN derived_at TEXT;

-- The Account 360 Compliance Calendar reads obligations by date; provenance
-- lookups and the producer's idempotency check read them by signal.
CREATE INDEX IF NOT EXISTS idx_regulatory_obligations_signal
  ON regulatory_obligations(signal_id);
CREATE INDEX IF NOT EXISTS idx_regulatory_obligations_effective
  ON regulatory_obligations(effective_date);
