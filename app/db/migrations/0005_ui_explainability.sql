-- 0005_ui_explainability: score-component persistence + badge legend for the
-- Streamlit UI chunk (R8.1, R8.3, R4.3, R7.6).
--
-- Score explainability (R8.1 decay bar / "why this score"): nullable component
-- columns on signals, populated by app.scoring.rescore(). Nullable so the
-- column add is safe on a DB scored by an earlier build; a NULL simply means
-- "not yet rescored under this build". scored_at records the injected-clock
-- time of the last rescore for that row (UTC ISO-8601, R10.2).
--
-- The DDL lives here (Phase A) so the whole UI chunk builds against a frozen
-- schema; app.scoring owns *populating* these columns and app.supersede owns
-- the new status value below.
--
-- signals.status gains a new value 'superseded' (alongside
-- active/decayed/dismissed from 0001). A proposal signal is flipped to
-- 'superseded' by app.supersede when a later final-rule signal of the same
-- trigger shares a regulatory docket. status is free TEXT, so no DDL change is
-- needed for the value itself; this comment is the schema-of-record for it.
-- A superseded row's score is frozen (rescore skips non-active rows), exactly
-- as decayed/dismissed rows are.

ALTER TABLE signals ADD COLUMN score_base REAL;
ALTER TABLE signals ADD COLUMN score_decay REAL;
ALTER TABLE signals ADD COLUMN score_account_fit REAL;
ALTER TABLE signals ADD COLUMN score_scope_fit REAL;
ALTER TABLE signals ADD COLUMN scored_at TEXT;

-- Badge legend (R4.3 non-primary badge, R8.1 evidence badge legend tooltip):
-- the UI renders badge labels/descriptions from this table and never hardcodes
-- what a code means. Seeded from seeds/badge_legend.csv by the loader.
--   badge_kind  grouping, e.g. 'evidence_quality', 'source_quality', 'scope'
--   code        the stored value, e.g. 'IR' / 'non-primary' / 'sector'
--   label       short human label for the chip
--   description tooltip / legend text
CREATE TABLE IF NOT EXISTS badge_legend (
  badge_kind TEXT NOT NULL,
  code TEXT NOT NULL,
  label TEXT NOT NULL,
  description TEXT DEFAULT '',
  PRIMARY KEY (badge_kind, code)
);
