"""Automated accuracy audit — LLM-as-judge (R9.7-R9.12).

The audit is a *separate writer process* (like ingestion): it samples recent
signals, asks a capped Claude judge four objective questions per signal
(entity_match, classification, evidence_support, license_play_support), and
writes `audit` rows plus an `audit_runs` bookkeeping row. It never modifies
weights, half-lives, mappings, or license facts (R9.8); the app stays a reader.

Layout:
  config.py   model id, budget ceiling, pricing, timeout (env-overridable)
  schema.py   versioned judge prompt + strict output schema + verdict parser
  client.py   stdlib (urllib) Anthropic Messages client with injectable transport
  goldens.py  golden-set regression harness (R9.10)
  judge.py    the sampling + write runner (R9.7) and its CLI
  precision.py precision computation feeding the UI + gates (R9.3-R9.5)
"""
