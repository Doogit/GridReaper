#!/bin/sh
# Canonical GridSignals data pipeline: licensing -> ingest (10 live feeds) ->
# classify -> score -> plays -> digest. ONE ordered list, shared by the runtime
# first-load entrypoint (deploy/entrypoint.sh) and any future in-container
# scheduler (the cron decision). The ordering invariants below are contract-
# tested hermetically in tests/test_packaging.py so drift fails locally, not on
# a cloud build.
#
# Backend steps are stdlib-only CLIs — ingestion is a process, never the UI
# (R3.1). Individual ingests are non-fatal (a flaky source degrades, R10.3); the
# classify -> score -> plays chain is hard so a partial store still scores what
# it has.
set -e

# License facts FIRST — required before plays, or play snapshots come out empty
# while the run still "succeeds" (silent-failure guard; see
# test_build_runs_licensing_before_plays).
python -m app.licensing

# Live public feeds. Each is wrapped so one flaky source can't abort the run.
python -m app.ingest.edgar             || echo "WARN: edgar ingest failed, continuing"
python -m app.ingest.edgar_fulltext    || echo "WARN: edgar_fulltext ingest failed, continuing"
python -m app.ingest.federal_register  || echo "WARN: federal_register ingest failed, continuing"
python -m app.ingest.presswire --source prnewswire    || echo "WARN: prnewswire ingest failed, continuing"
python -m app.ingest.presswire --source globenewswire || echo "WARN: globenewswire ingest failed, continuing"
python -m app.ingest.nerc_pages        || echo "WARN: nerc_pages ingest failed, continuing"
python -m app.ingest.cisa_kev          || echo "WARN: cisa_kev ingest failed, continuing"
python -m app.ingest.ransomware        || echo "WARN: ransomware ingest failed, continuing"
python -m app.ingest.security_rss --source therecord        || echo "WARN: the_record ingest failed, continuing"
python -m app.ingest.security_rss --source bleepingcomputer || echo "WARN: bleepingcomputer ingest failed, continuing"
python -m app.ingest.nerc_calendar     || echo "WARN: nerc_calendar ingest failed, continuing"
python -m app.ingest.nerc_enforcement  || echo "WARN: nerc_enforcement ingest failed, continuing"

# Classify -> score -> plays -> digest. Classifiers run before scoring; digest
# is last (reads the freshest scored cards + play snapshots, R8.8).
# incident (8-K 1.05) reads EDGAR submissions; ransomware reads ransomware.live —
# both are Stage-2 incident classifiers that were merged but previously unwired
# here, so they never fired (they mint account/peer incident cards when a real
# 1.05 filing or resolvable ransomware victim appears).
python -m app.classify.regulatory
python -m app.classify.leadership
python -m app.classify.company_statement
python -m app.classify.incident
python -m app.classify.ransomware
python -m app.classify.security_rss
# Obligations (R8.3) derive from classified regulatory signals, not from
# scores — so they run after the classifiers and before scoring.
python -m app.obligations
python -m app.scoring
python -m app.plays
python -m app.ui_web.digest || echo "WARN: digest generation failed, continuing"

# Drift log (NON-fatal). Unlike the old build-time assertion this never exits
# nonzero: a runtime background run must not crash the container — the UI
# degrades to honest empty states (R6.6) if a feed-less day produces nothing.
python -c "import sqlite3, os; \
db = os.environ.get('GRIDSIGNALS_DB', 'data/gridsignals.db'); \
c = sqlite3.connect(db); \
s = c.execute('select count(*) from signals').fetchone()[0]; \
p = c.execute('select count(*) from license_play_snapshots').fetchone()[0]; \
print(f'pipeline complete: {s} signals, {p} license play snapshots')"
