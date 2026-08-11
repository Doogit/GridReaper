-- 0004_scoring_weights: operator-tunable scoring weights (R7.3, R7.5, R8.7).
-- Seeded from seeds/scoring_weights.csv; Admin sliders will edit these rows
-- later (R8.7). weight_kind groups the lookup tables the scorer uses:
--   subsector / richness / coverage  -> account_fit factors (R7.5)
--   applicability                    -> replaces richness for
--                                       regulatory_calendar-scoped cards
--   scope                            -> scope_fit (account vs sector vs
--                                       regulatory_calendar cards)
-- weight is REAL; the seed loader passes CSV strings and SQLite's REAL
-- affinity coerces numeric text on insert. Unknown/missing keys fall back to
-- a neutral 1.0 in app/scoring.py, never an error.

CREATE TABLE IF NOT EXISTS scoring_weights (
  weight_kind TEXT NOT NULL,
  key TEXT NOT NULL,
  weight REAL NOT NULL,
  notes TEXT DEFAULT '',
  PRIMARY KEY (weight_kind, key)
);
