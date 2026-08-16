-- 0012_aggregates: precomputed nightly signal aggregates (R8.10).
--
-- R8.10 asks for precomputed nightly aggregates alongside the named indexes
-- (already present since 0001). The only GROUP BY read the UI performs is the
-- Explore Analytics tab's three-way signal count (trigger / scope / incident
-- evidence tier), so that is what is precomputed here; app.aggregates owns the
-- refresh and app/ui/data.py owns reading it.
--
-- TWO tables on purpose. aggregate_state holds the as-of stamp and the
-- staleness basis for a whole refresh; signal_aggregates holds the counted
-- rows. Folding the stamp onto each count row would make "computed and
-- genuinely empty" indistinguishable from "never computed" — an empty store is
-- the normal case here (R6.6), so that distinction is load-bearing, not
-- hypothetical.
--
-- Runtime-only and never seeded (like source_runs / config_audit): fully
-- regenerable by re-running `python -m app.aggregates`. Deleting every row is
-- always safe — the reader falls back to a live recompute and says so.

-- The as-of + staleness record for one aggregate. One row per aggregate_name,
-- rewritten in the same transaction as its counts, so the stamp can never
-- describe a different refresh than the rows it sits beside.
--
--   aggregate_name    the refresh that produced the rows below
--   computed_at       UTC ISO-8601 (R10.2) when the refresh ran — the as-of
--                     the reader surfaces; aggregates are stale-able BY the
--                     reader, never silently served (see app/ui/data.py)
--   status_filter     JSON list of signals.status values the counts cover; a
--                     read asking for a different filter is not served from
--                     the aggregate at all
--   basis             JSON fingerprint of the store the counts were taken
--                     from: per-status signal counts plus the high-water
--                     incident_tier_edits id. Cheap to recompute (one grouped
--                     count on one column + one MAX), and it moves on every
--                     mutation that can change these numbers — new signals,
--                     status flips (supersede/retract/decay) and operator
--                     re-tiers. A score edit does not move it and does not
--                     change these counts either.
--   refresh_version   which app.aggregates build wrote the row
CREATE TABLE IF NOT EXISTS aggregate_state (
  aggregate_name  TEXT PRIMARY KEY,
  computed_at     TEXT NOT NULL,
  status_filter   TEXT NOT NULL,
  basis           TEXT NOT NULL,
  refresh_version TEXT NOT NULL
);

--   aggregate_name  the refresh that produced the row (FK aggregate_state)
--   dimension       'trigger' | 'scope' | 'incident_tier'
--   key             the stored dimension value (trigger_id, scope, tier)
--   label           display label at refresh time (trigger name, else the key)
--   count           number of signals in that bucket under the refresh's
--                   status filter
--
-- key/label are NULLABLE on purpose. A signal with a NULL trigger_id or
-- signal_scope is a data defect, and the live computation reports it as a NULL
-- bucket rather than hiding it; the precomputed path must round-trip that
-- byte-for-byte or the reader's "aggregate equals live" contract quietly stops
-- holding. At most one NULL bucket per dimension exists per refresh and a
-- refresh replaces the aggregate's rows whole, so NULLs cannot accumulate under
-- the (nullable-tolerant) primary key.
CREATE TABLE IF NOT EXISTS signal_aggregates (
  aggregate_name TEXT NOT NULL REFERENCES aggregate_state(aggregate_name),
  dimension      TEXT NOT NULL,
  key            TEXT,
  label          TEXT,
  count          INTEGER NOT NULL,
  PRIMARY KEY (aggregate_name, dimension, key)
);

-- The reader fetches one aggregate's rows by name, in dimension order.
CREATE INDEX IF NOT EXISTS idx_signal_aggregates_name
  ON signal_aggregates(aggregate_name, dimension);
