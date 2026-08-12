-- 0006_audit: automated accuracy audit (LLM-as-judge) versioning + run
-- bookkeeping for the feedback/audit chunk (R9.7-R9.12).
--
-- The `audit` and `audit_goldens` tables were created in 0001. This migration
-- only *adds* to `audit` and introduces a runtime-only bookkeeping table; it
-- never rewrites frozen DDL.
--
-- R9.9 versioning: a verdict must store model_id, prompt_version, AND
-- parser_version. `audit` already carries model_id/prompt_version/ts (0001);
-- parser_version is added here. Nullable so the column add is safe on a DB
-- that predates the audit chunk (no verdicts exist there yet); the runner
-- always writes a non-NULL value going forward.
ALTER TABLE audit ADD COLUMN parser_version TEXT;

-- Supports the runner's re-run dedupe (R9.7): "which signals already have a
-- verdict at this prompt_version" and the precision reads that group by check.
CREATE INDEX IF NOT EXISTS idx_audit_signal_prompt
  ON audit(signal_id, prompt_version);

-- audit_runs: one row per invocation of the audit judge job, so an R9.12 skip
-- (no API key / budget exhausted / nothing to sample) is *recorded*, not
-- silent. This is a runtime-managed table (like source_runs); it is never
-- seeded and is fully regenerable by re-running the audit job.
--
--   run_id           "audit:{uuid12}", assigned at job start
--   started_at       UTC ISO-8601 (R10.2) when the run began
--   finished_at      UTC ISO-8601 when it finalized ('' while running)
--   model_id         the judge model id (e.g. claude-haiku-4-5)
--   prompt_version   app.audit.schema.PROMPT_VERSION at run time (R9.9)
--   parser_version   app.audit.schema.PARSER_VERSION at run time (R9.9)
--   signals_sampled  count of signals the run selected to audit
--   verdicts_written rows inserted into `audit` this run (checks x signals)
--   budget_spent     estimated USD spent on judge calls this run (R9.12/R10.1)
--   status           see values below
--   error_state      verbatim last error (R10.3-style), '' when clean
--   skipped_reason   why an R9.12 skip happened ('' when not skipped)
--
-- status values:
--   running                  in flight (row inserted up front)
--   success                  completed; verdicts written (or 0 with no signals)
--   error                    a per-signal / infra error was contained
--   skipped_no_key           ANTHROPIC_API_KEY absent/unreachable (R9.12)
--   skipped_over_budget      per-run budget ceiling already reached (R9.12)
--   skipped_no_signals       nothing new to audit this run
CREATE TABLE IF NOT EXISTS audit_runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT DEFAULT '',
  model_id TEXT DEFAULT '',
  prompt_version TEXT DEFAULT '',
  parser_version TEXT DEFAULT '',
  signals_sampled INTEGER DEFAULT 0,
  verdicts_written INTEGER DEFAULT 0,
  budget_spent REAL DEFAULT 0,
  status TEXT NOT NULL,
  error_state TEXT DEFAULT '',
  skipped_reason TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_runs_started ON audit_runs(started_at);
