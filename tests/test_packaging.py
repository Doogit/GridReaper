"""Contract tests for the Azure/Docker packaging (see Dockerfile, deploy/).

Hermetic and no-Docker (like the rest of the suite) so drift between the repo
and what the image build assumes fails fast and locally, not on a cloud build.

Runtime first-load model: the image ships code + config seeds only (no baked
dataset). The Dockerfile CMD runs deploy/entrypoint.sh, which seeds the schema,
background-runs deploy/ingest_pipeline.sh on first load, then execs uvicorn.
The pipeline's ordering invariants (licensing before plays, classify before
score, digest last) live in the pipeline script and are pinned below.

Scheduled refresh (R3.1): the same pipeline is re-run by an in-container cron
daemon (deploy/crontab), started by the entrypoint before uvicorn. The crontab is
a SECOND caller of the canonical pipeline, so the same anti-drift contract
applies to it — it must drive deploy/ingest_pipeline.sh rather than inline its
own step list, and every module it names must exist.
"""

import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from app.ingest.runner import LOCK_STALE_S

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "Dockerfile"
ENTRYPOINT = REPO / "deploy" / "entrypoint.sh"
PIPELINE = REPO / "deploy" / "ingest_pipeline.sh"
CRONTAB = REPO / "deploy" / "crontab"
SCHEDULED_RUN = REPO / "deploy" / "scheduled_run.sh"
ENV_SNAPSHOT = "/etc/gridsignals.env"
SENTINEL = "GUARD-RAN-THE-JOB"
DEPLOY_README = REPO / "deploy" / "README.md"
PACKAGE_WORKFLOW = REPO / ".github" / "workflows" / "package.yml"
AZURE_DEPLOY = REPO / "deploy" / "azure-deploy.ps1"
DOCKERIGNORE = REPO / ".dockerignore"


class PackagingContractTest(unittest.TestCase):
    def _dockerfile(self) -> str:
        self.assertTrue(DOCKERFILE.exists(), "Dockerfile missing — Azure packaging cannot build")
        return DOCKERFILE.read_text(encoding="utf-8")

    def _entrypoint(self) -> str:
        # Executable lines only — order/presence checks must not trip on the
        # step-by-step prose in the comment header.
        self.assertTrue(ENTRYPOINT.exists(), "deploy/entrypoint.sh missing — container has no entrypoint")
        lines = ENTRYPOINT.read_text(encoding="utf-8").splitlines()
        return "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))

    def _pipeline(self) -> str:
        self.assertTrue(PIPELINE.exists(), "deploy/ingest_pipeline.sh missing — first-load ingest cannot run")
        return PIPELINE.read_text(encoding="utf-8")

    def _crontab(self) -> str:
        self.assertTrue(CRONTAB.exists(), "deploy/crontab missing — the container never re-ingests")
        return CRONTAB.read_text(encoding="utf-8")

    def _crontab_jobs(self):
        # Scheduled entries only: drop comments, blank lines, and cron's own
        # NAME=value settings (SHELL, PATH).
        jobs = []
        for line in self._crontab().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", stripped):
                continue
            jobs.append(stripped)
        self.assertTrue(jobs, "deploy/crontab schedules nothing")
        return jobs

    def test_config_seeds_present(self):
        # load_seeds bakes the config layer from these (at runtime first-load).
        for rel in (
            "seeds/watchlist_entities.csv",
            "seeds/products.csv",
            "seeds/triggers.csv",
            "seeds/license_matrix.csv",
            "seeds/scoring_weights.csv",
        ):
            self.assertTrue((REPO / rel).exists(), f"seed {rel} referenced by the build is missing")

    def test_pipeline_modules_present(self):
        # Every module the entrypoint + ingest pipeline invoke (`python -m`
        # steps) must exist.
        for rel in (
            "app/ui_web/app.py",
            "app/db/load_seeds.py",
            "app/first_load.py",
            "app/licensing.py",
            "app/obligations.py",
            "app/scoring.py",
            "app/plays.py",
            "app/aggregates.py",
            "app/ui_web/digest.py",
            "app/classify/regulatory.py",
            "app/classify/leadership.py",
            "app/classify/company_statement.py",
            "app/classify/incident.py",
            "app/classify/ransomware.py",
            "app/classify/security_rss.py",
            "app/ingest/edgar_fulltext.py",
            "app/ingest/ransomware.py",
            "app/ingest/security_rss.py",
            "app/ingest/nerc_calendar.py",
            "app/ingest/nerc_enforcement.py",
            "app/ingest/gdelt.py",
            "app/ingest/cisa_ics.py",
            "app/ingest/epa_echo.py",
            "app/classify/environmental_enforcement.py",
            "app/ingest/usaspending.py",
            "app/classify/capital_project.py",
            "app/ingest/phmsa.py",
            "app/classify/phmsa_enforcement.py",
        ):
            self.assertTrue((REPO / rel).exists(), f"module {rel} the build invokes is missing")

    def test_pipeline_runs_edgar_fulltext_ingest(self):
        # The full-text source is registered in source_policies and is the
        # recall path for filing bodies/exhibits. Registration alone is not
        # enough: first-load/runtime ingestion must actually populate it.
        p = self._pipeline()
        self.assertIn("app.ingest.edgar_fulltext", p)
        self.assertLess(
            p.index("app.ingest.edgar"),
            p.index("app.ingest.edgar_fulltext"),
            "EDGAR submissions should run before full-text search on the shared SEC budget",
        )
        self.assertLess(
            p.index("app.ingest.edgar_fulltext"),
            p.index("app.classify.incident"),
            "all EDGAR ingestion should complete before incident classification",
        )

    def test_pipeline_runs_gdelt_ingest_store_only(self):
        # U9a (R9.6): GDELT is store-only pending the >=20 distinct
        # corporate-action re-entry check. The fetcher must run so the corpus
        # keeps growing under cron, but the classifier stays unwired -- its
        # own docstring documents why it must not be added here.
        p = self._pipeline()
        self.assertIn("app.ingest.gdelt", p, "pipeline dropped the GDELT store-only fetcher (R9.6)")
        self.assertNotIn(
            "app.classify.gdelt", p,
            "the GDELT classifier stays unwired pending the R9.6 silent-trial re-entry check",
        )

    def test_pipeline_runs_company_statement_classifier_before_scoring(self):
        # Company-statement incident cards are minted from the same press-wire
        # backfill as leadership; the pipeline must run the classifier before
        # scoring and plays snapshot the feed.
        p = self._pipeline()
        self.assertIn("app.classify.company_statement", p)
        self.assertLess(
            p.index("app.classify.company_statement"), p.index("app.scoring"),
            "company-statement classification must run before scoring",
        )

    def test_pipeline_runs_security_rss_before_scoring(self):
        # Security-press cards come from their own RSS ingestion module. The
        # pipeline must run both feeds and the classifier before scoring.
        p = self._pipeline()
        self.assertIn("app.ingest.security_rss --source therecord", p)
        self.assertIn("app.ingest.security_rss --source bleepingcomputer", p)
        self.assertIn("app.classify.security_rss", p)
        self.assertLess(
            p.index("app.ingest.security_rss --source therecord"),
            p.index("app.classify.security_rss"),
            "security RSS ingestion must run before classification",
        )
        self.assertLess(
            p.index("app.classify.security_rss"), p.index("app.scoring"),
            "security RSS classification must run before scoring",
        )

    def test_pipeline_runs_incident_classifier_before_scoring(self):
        # The 8-K Item 1.05 incident classifier reads EDGAR submissions and mints
        # the product's core account-scoped confirmed-incident cards. It was
        # merged but previously unwired here (never fired); pin it so it can't be
        # dropped again, ordered after EDGAR ingest and before scoring.
        p = self._pipeline()
        self.assertIn("app.classify.incident", p, "pipeline dropped the 8-K 1.05 incident classifier")
        self.assertLess(
            p.index("app.ingest.edgar"), p.index("app.classify.incident"),
            "EDGAR ingest must run before the incident classifier",
        )
        self.assertLess(
            p.index("app.classify.incident"), p.index("app.scoring"),
            "incident classification must run before scoring",
        )

    def test_pipeline_runs_ransomware_ingest_and_classifier_before_scoring(self):
        # The ransomware.live feed + classifier mint operator early-warning cards.
        # Both were merged but previously unwired here; pin the ingest -> classify
        # -> score ordering so a resolvable victim actually surfaces.
        p = self._pipeline()
        self.assertIn("app.ingest.ransomware", p, "pipeline dropped the ransomware.live feed")
        self.assertIn("app.classify.ransomware", p, "pipeline dropped the ransomware classifier")
        self.assertLess(
            p.index("app.ingest.ransomware"), p.index("app.classify.ransomware"),
            "ransomware ingest must run before its classifier",
        )
        self.assertLess(
            p.index("app.classify.ransomware"), p.index("app.scoring"),
            "ransomware classification must run before scoring",
        )

    def test_pipeline_runs_epa_echo_ingest_and_classifier_before_scoring(self):
        # R7: the EPA ECHO consent-decree fetcher + classifier were merged but
        # previously unwired here, so audit_consent_decree never fired. Pin
        # the ingest -> classify -> score ordering so it actually does.
        p = self._pipeline()
        self.assertIn("app.ingest.epa_echo", p, "pipeline dropped the EPA ECHO fetcher")
        self.assertIn(
            "app.classify.environmental_enforcement", p,
            "pipeline dropped the EPA ECHO classifier",
        )
        self.assertLess(
            p.index("app.ingest.epa_echo"),
            p.index("app.classify.environmental_enforcement"),
            "EPA ECHO ingest must run before its classifier",
        )
        self.assertLess(
            p.index("app.classify.environmental_enforcement"), p.index("app.scoring"),
            "environmental enforcement classification must run before scoring",
        )

    def test_pipeline_runs_usaspending_ingest_and_classifier_before_scoring(self):
        # R5: the USAspending.gov fetcher + classifier wire the previously
        # unused capital_project trigger. Pin the ingest -> classify -> score
        # ordering so it actually fires.
        p = self._pipeline()
        self.assertIn("app.ingest.usaspending", p, "pipeline dropped the USAspending.gov fetcher")
        self.assertIn(
            "app.classify.capital_project", p,
            "pipeline dropped the USAspending.gov classifier",
        )
        self.assertLess(
            p.index("app.ingest.usaspending"),
            p.index("app.classify.capital_project"),
            "USAspending ingest must run before its classifier",
        )
        self.assertLess(
            p.index("app.classify.capital_project"), p.index("app.scoring"),
            "capital_project classification must run before scoring",
        )

    def test_pipeline_runs_phmsa_ingest_and_classifier_before_scoring(self):
        # R6: the PHMSA enforcement fetcher + classifier wire the new
        # pipeline_enforcement_action trigger. Pin the ingest -> classify ->
        # score ordering so a qualifying midstream/LNG enforcement case
        # actually fires (same shape as the EPA ECHO pinning above).
        p = self._pipeline()
        self.assertIn("app.ingest.phmsa", p, "pipeline dropped the PHMSA fetcher")
        self.assertIn(
            "app.classify.phmsa_enforcement", p,
            "pipeline dropped the PHMSA classifier",
        )
        self.assertLess(
            p.index("app.ingest.phmsa"),
            p.index("app.classify.phmsa_enforcement"),
            "PHMSA ingest must run before its classifier",
        )
        self.assertLess(
            p.index("app.classify.phmsa_enforcement"), p.index("app.scoring"),
            "PHMSA classification must run before scoring",
        )

    def test_pipeline_runs_digest_after_scoring_and_plays(self):
        # The digest (R8.8) is the pipeline's LAST step: it reads the freshest
        # scored cards + play snapshots, so it must run after scoring and plays.
        p = self._pipeline()
        self.assertIn("app.ui_web.digest", p, "pipeline dropped the digest step (R8.8)")
        self.assertGreater(
            p.index("app.ui_web.digest"), p.index("app.scoring"),
            "digest must run after scoring",
        )
        self.assertGreater(
            p.index("app.ui_web.digest"), p.index("app.plays"),
            "digest must run after plays",
        )

    def test_pipeline_refreshes_aggregates_between_plays_and_digest(self):
        # R8.10 nightly aggregates. The refresh must run after the last step
        # that mints or re-statuses signals (plays closes the chain) and before
        # the digest, so the digest never reads counts older than the cards
        # beside them. Non-fatal on purpose: a derived optimization must not be
        # the reason the digest is skipped, and the reader falls back to a live
        # recompute rather than serving a stale aggregate.
        p = self._pipeline()
        self.assertIn("app.aggregates", p, "pipeline dropped the R8.10 aggregate refresh")
        self.assertGreater(
            p.index("app.aggregates"), p.index("app.plays"),
            "aggregates must be refreshed after plays",
        )
        self.assertLess(
            p.index("app.aggregates"), p.index("app.ui_web.digest"),
            "aggregates must be refreshed before the digest reads them",
        )
        self.assertRegex(
            p, r"python -m app\.aggregates \|\| echo",
            "the aggregate refresh must be non-fatal (the digest still runs)",
        )

    def test_pipeline_runs_licensing_before_plays(self):
        # Silent-failure guard: without `app.licensing`, plays generate ZERO
        # license-play snapshots and the feed loses its play cards, yet the run
        # still succeeds. Pin the step so it can't be dropped.
        p = self._pipeline()
        self.assertIn("app.licensing", p, "pipeline dropped the licensing step -> no license plays")
        self.assertLess(
            p.index("app.licensing"), p.index("app.plays"),
            "licensing must run before plays",
        )

    def test_pipeline_logs_feed_and_play_counts(self):
        # Drift visibility: the pipeline logs the resulting signal + play counts.
        # Runtime-background now, so this is a NON-fatal log, not a build-time
        # assertion — the UI degrades to honest empty states (R6.6) on a dry run.
        p = self._pipeline()
        self.assertRegex(p, r"count\(\*\) from signals")
        self.assertRegex(p, r"count\(\*\) from license_play_snapshots")

    def test_cmd_runs_entrypoint_which_launches_fastapi_ui(self):
        # The image serves the FastAPI + HTMX UI via uvicorn, launched by the
        # entrypoint. Pin it so the deploy path can't regress to Streamlit.
        df = self._dockerfile()
        self.assertIn("deploy/entrypoint.sh", df, "Dockerfile CMD must run deploy/entrypoint.sh")
        self.assertNotIn("streamlit", df, "Streamlit was removed at the Chunk 7 cutover")
        entry = self._entrypoint()
        self.assertIn("uvicorn app.ui_web.app:app", entry, "entrypoint must launch the FastAPI app via uvicorn")
        self.assertNotIn("streamlit", entry, "Streamlit was removed at the Chunk 7 cutover")

    def test_entrypoint_seeds_schema_before_serving(self):
        # Schema + config must be loaded before uvicorn binds, or the first
        # requests hit missing tables. load_seeds is blocking; uvicorn is exec'd.
        entry = self._entrypoint()
        self.assertIn("app.db.load_seeds", entry)
        self.assertLess(
            entry.index("app.db.load_seeds"), entry.index("uvicorn"),
            "schema/config seeds must load before the server starts",
        )

    def test_entrypoint_backgrounds_first_load_ingest(self):
        # First-load ingest runs in the BACKGROUND (trailing &) so the app serves
        # immediately, gated by the app.first_load empty-feed check, via the
        # canonical pipeline script.
        entry = self._entrypoint()
        self.assertIn("app.first_load", entry, "entrypoint must gate ingest on the first-load check")
        self.assertRegex(
            entry, r"deploy/ingest_pipeline\.sh[^\n]*&",
            "first-load ingest must run in the background (trailing &)",
        )

    # -- scheduled refresh (R3.1) --------------------------------------------

    def test_crontab_drives_the_canonical_pipeline(self):
        # The crontab is the pipeline's second caller. It must invoke the shared
        # script; a hardcoded chain of its own would be exactly the drift these
        # packaging tests exist to prevent (one caller would silently lose a
        # classifier the other kept).
        jobs = self._crontab_jobs()
        self.assertTrue(
            any("deploy/ingest_pipeline.sh" in job for job in jobs),
            "no scheduled entry runs deploy/ingest_pipeline.sh — the dataset freezes at first boot",
        )
        for job in jobs:
            for step in ("app.licensing", "app.ingest.", "app.classify.",
                         "app.scoring", "app.plays", "app.aggregates", "app.ui_web.digest"):
                self.assertNotIn(
                    step, job,
                    f"crontab inlines a pipeline step ({step}) instead of calling "
                    f"deploy/ingest_pipeline.sh — a second step list will drift",
                )

    def test_crontab_schedules_the_precision_and_reverification_jobs(self):
        # R9.3 and R10.7 each name a JOB, not just a page: a monthly precision
        # computation and a semi-annual license re-verification task. Both were
        # marked resolved against a scheduler that did not schedule them, so pin
        # the cadence fields — a job present at the wrong cadence is the same
        # false green in slower motion.
        # A LIST of (schedule, command), never a dict keyed by command: two
        # lines running the same command would collapse into one key, hiding a
        # duplicate or a mis-scheduled twin behind an "exactly once" assertion
        # that could no longer see it.
        entries = []
        for job in self._crontab_jobs():
            match = re.match(r"^(\S+ \S+ \S+ \S+ \S+)\s+\S+\s+(.*)$", job)
            self.assertIsNotNone(match, f"not a 6-field /etc/cron.d entry: {job}")
            entries.append((match.group(1), match.group(2)))

        precision = [e for e in entries if "app.audit.precision" in e[1]]
        self.assertEqual(len(precision), 1, "R9.3's monthly precision job is not scheduled exactly once")
        schedule, command = precision[0]
        self.assertIn("--report", command, "the precision job must run its --report entry point")
        # dom is a single day and mon is every month -> monthly (R9.3).
        _m, _h, dom, mon, _dow = schedule.split()
        self.assertRegex(dom, r"^\d+$", f"R9.3 asks for a MONTHLY job; day-of-month is {dom!r}")
        self.assertEqual(mon, "*", f"R9.3 asks for a MONTHLY job; month field is {mon!r}")

        reverify = [e for e in entries if "app.reverify" in e[1]]
        self.assertEqual(len(reverify), 1, "R10.7's re-verification sweep is not scheduled exactly once")
        # Four months named, three apart -> quarterly (U24). Runs more often than
        # R10.7's literal "semi-annual" minimum so the sweep stays comfortably
        # inside stale_facts's 180-day window.
        _m, _h, dom, mon, _dow = reverify[0][0].split()
        self.assertRegex(dom, r"^\d+$", f"R10.7's sweep asks for a QUARTERLY job; day-of-month is {dom!r}")
        months = sorted(int(part) for part in mon.split(","))
        self.assertEqual(len(months), 4, f"R10.7's sweep asks for a QUARTERLY job; month field is {mon!r}")
        gaps = [b - a for a, b in zip(months, months[1:])]
        self.assertEqual(gaps, [3, 3, 3], f"the four runs are not three months apart each: {mon!r}")

    def test_crontab_targets_exist(self):
        # Mirror of test_pipeline_modules_present for the scheduler: a crontab
        # naming a module that does not exist fails only at 3am in a container.
        text = self._crontab()
        for module in re.findall(r"python -m ([\w.]+)", text):
            try:
                spec = importlib.util.find_spec(module)
            except (ImportError, ValueError):
                spec = None
            self.assertIsNotNone(spec, f"crontab schedules {module}, which is not importable")
        scripts = set(re.findall(r"deploy/[\w.\-]+\.sh", text))
        self.assertIn("deploy/scheduled_run.sh", scripts, "crontab must wrap jobs in the lock guard")
        for script in scripts:
            self.assertTrue((REPO / script).exists(), f"crontab references {script}, which is missing")

    def test_crontab_jobs_source_the_env_snapshot(self):
        # cron strips the environment: without this a scheduled run cannot see
        # GRIDSIGNALS_DB (a Dockerfile ENV) or any optional API key, so a source
        # that skips on a missing key silently never runs while the ledger reads
        # healthy. A scheduled run must see what an interactive run sees.
        for job in self._crontab_jobs():
            self.assertIn(f". {ENV_SNAPSHOT}", job, f"scheduled job does not source {ENV_SNAPSHOT}: {job}")
            self.assertLess(
                job.index(f". {ENV_SNAPSHOT}"), job.index("cd /app"),
                "the environment snapshot must be sourced before the job runs",
            )

    def test_crontab_holds_no_secret(self):
        # R10.8: keys live outside the repo. The snapshot is written at startup
        # to a 600 file; nothing secret is ever assigned in this tracked file.
        text = self._crontab()
        self.assertNotRegex(
            text, r"(?i)\b\w*(api_key|apikey|secret|token|password|credential)\w*\s*=",
            "deploy/crontab must never assign a secret (R10.8)",
        )

    def test_store_writing_crontab_jobs_run_under_the_lock_guard(self):
        # app/ingest/runner.py RAISES on a live lock and ingest_pipeline.sh runs
        # under `set -e`, so an unguarded tick during a manual/first-load run
        # aborts mid-pipeline. That is a WRITER's problem: the guard serializes
        # writes and exits 0 on contention. Wrapping the read-only reporting jobs
        # in it bought nothing (they take no lock) and cost the record — a skip
        # discards a month of R9.3 or six of R10.7 while cron logs success. So
        # the guard is required of the jobs that write, and pinned as absent from
        # the jobs that do not.
        writers = [job for job in self._crontab_jobs() if "ingest_pipeline.sh" in job]
        self.assertTrue(writers, "no scheduled job writes the store — the dataset freezes at first boot")
        for job in writers:
            self.assertIn("deploy/scheduled_run.sh", job, f"scheduled job bypasses the lock guard: {job}")
        for job in self._crontab_jobs():
            if job in writers:
                continue
            self.assertNotIn(
                "deploy/scheduled_run.sh", job,
                f"a read-only reporting job is wrapped in the write guard, whose skip-on-contention "
                f"would discard the record instead of delaying it: {job}",
            )

    def test_crontab_logs_the_whole_job_and_disables_mail(self):
        # A bare `cmd >> log` binds the redirect to the LAST command only, so a
        # failure of the env source or the `cd` — the two likeliest tick-killers
        # — would log zero bytes and go to cron's mail channel, which has no MTA
        # in this image. Brace-group the job, redirect outside the group, and
        # make the env source fatal rather than a silently-skipped prefix.
        self.assertIn('MAILTO=""', self._crontab(), "cron mail is a black hole here; disable it explicitly")
        for job in self._crontab_jobs():
            self.assertRegex(
                job, r"\{.*\}\s*>>",
                f"the log redirect must sit outside the brace group, or only the last command is logged: {job}",
            )
            self.assertRegex(
                job, rf"\. {re.escape(ENV_SNAPSHOT)}\s*&&",
                "a failed environment source must abort the tick, not run it blind",
            )

    def test_crontab_keeps_the_cron_d_format_rules_that_fail_silently(self):
        # Each of these makes cron ignore work with no error anywhere.
        raw = CRONTAB.read_bytes()
        self.assertNotIn(b"\r", raw, "a CRLF /etc/cron.d entry corrupts its trailing field and never fires")
        self.assertTrue(raw.endswith(b"\n"), "a cron.d file without a trailing newline drops its last job")
        for job in self._crontab_jobs():
            self.assertNotIn("%", job, "cron translates an unescaped % into a newline, truncating the job")

    def test_scheduled_run_guard_ships_lf_only(self):
        # Same CRLF trap as the crontab, one step removed: .gitattributes is what
        # keeps a Windows clone from shipping CR-terminated shell scripts.
        self.assertNotIn(b"\r", SCHEDULED_RUN.read_bytes(), "the guard must ship LF-only (see .gitattributes)")
        attributes = (REPO / ".gitattributes").read_text(encoding="utf-8")
        self.assertRegex(attributes, r"(?m)^deploy/crontab\s+text\s+eol=lf")
        self.assertRegex(attributes, r"(?m)^\*\.sh\s+text\s+eol=lf")

    def test_crontab_does_not_pretend_to_automate_entity_enrichment(self):
        # R4.2 is NOT automatable as a cron job: app/enrich_entities.py writes
        # reviewable seed CSVs and never writes the store, so in a container the
        # output lands on ephemeral storage and is never loaded. It stays an
        # operator-run refresh; the crontab must say so rather than schedule it.
        for job in self._crontab_jobs():
            self.assertNotIn(
                "app.enrich_entities", job,
                "the annual identifier refresh writes seed CSVs for review, not the store — "
                "scheduling it would only pretend R4.2 was automated",
            )
        self.assertIn("R4.2", self._crontab(), "deploy/crontab must record why the annual refresh is absent")

    def test_crontab_never_schedules_the_audit_judge(self):
        # U26: the operator overruled the original plan's "schedule it in
        # cron" framing (and R9.7's literal weekly-cadence text) — the audit
        # judge is operator-invoked only, uncapped-by-count, budget-capped at
        # $1.00, with a --estimate dry run. Pin its absence so a future
        # session cannot silently "fix" it back into the schedule.
        for job in self._crontab_jobs():
            self.assertNotIn(
                "app.audit.judge", job,
                "the audit judge must stay on-demand only (operator ruling, U26) — "
                "never scheduled in deploy/crontab",
            )
        self.assertNotIn(
            "app.audit.judge", self._pipeline(),
            "the audit judge must not be inlined into the canonical pipeline either",
        )

    def _scheduler_start(self, entry: str) -> int:
        match = re.search(r"&&\s*cron\b", entry)
        self.assertIsNotNone(
            match,
            "entrypoint must start the cron daemon (Debian: cron, not crond), guarded so a "
            "failure cannot take the web process down with it",
        )
        return match.start()

    def test_entrypoint_starts_scheduler_before_uvicorn(self):
        # A scheduler that blocks the web process is worse than no scheduler:
        # cron is started (backgrounded daemon) before uvicorn is started.
        # U28: uvicorn runs as a plain backgrounded child, `wait`ed on, rather
        # than replacing this shell via `exec` — see
        # test_entrypoint_relays_sigterm_to_cron for why the exec'd-uvicorn
        # design (this test's own name until U28) does not survive shutdown
        # with a cron tick in flight, verified against real Docker.
        entry = self._entrypoint()
        self.assertLess(
            self._scheduler_start(entry), entry.index("uvicorn app.ui_web.app:app"),
            "the scheduler must start before uvicorn starts, or it never starts at all",
        )
        executable = [ln for ln in entry.splitlines() if ln.strip()]
        self.assertTrue(
            executable[-1].startswith('wait "$UVICORN_PID"'),
            "the script must wait on uvicorn as the last step, not exec/replace itself with it — "
            "see test_entrypoint_relays_sigterm_to_cron for why",
        )
        self.assertRegex(
            entry, r'uvicorn app\.ui_web\.app:app[^\n]*&\s*\nUVICORN_PID=\$!',
            "uvicorn must be backgrounded with its pid captured, not exec'd",
        )

    def test_entrypoint_relays_sigterm_to_cron(self):
        # U28: tini -g SIGTERMs every member of this shell's process group
        # directly, but cron puts EACH job it runs into its OWN freshly
        # assigned session/process group at fork time — not one shared group
        # with the daemon, and not stable across ticks (empirically confirmed
        # against real Docker: even `timeout`, wrapping every tick, re-groups
        # itself again inside that, and multiple ticks can be live at once
        # sharing none of it). There is no static pgid to precompute, so a
        # container stop never reaches an in-flight cron tick unless
        # something walks cron's live descendants AT SHUTDOWN TIME and
        # signals them directly. Without this, a tick's
        # `finally: os.remove(path)` lock cleanup (app/ingest/runner.py's
        # ingest_lock) never gets the chance to run and data/.ingest.lock
        # leaks until its 2h staleness window passes.
        #
        # The walk must also run SYNCHRONOUSLY, in the process tini actually
        # waits on, not backgrounded: tini (PID 1) exits the instant its
        # tracked child exits, and the kernel tears down the whole PID
        # namespace the instant tini exits. uvicorn shuts down on TERM in
        # well under a second, so a backgrounded walk — with uvicorn still
        # exec'd as the tracked child — consistently lost that race in real
        # Docker testing (the walk never finished). Hence uvicorn is a
        # waited-on child (see test_entrypoint_starts_scheduler_before_uvicorn)
        # and the walk runs in a trap on THIS shell, which only exits once the
        # walk and uvicorn are both done.
        entry = self._entrypoint()
        self.assertIn(
            "/run/crond.pid", entry,
            "the entrypoint must resolve cron's real pid from its pidfile after starting it",
        )
        self.assertRegex(
            entry, r"relay_sigterm_to_cron_descendants\s*\(\s*\)",
            "the entrypoint must define a relay that walks cron's descendants, not a precomputed pgid",
        )
        self.assertRegex(
            entry, r"awk '\{print \$4\}' \"\$p/stat\"",
            "the descendant walk must read ppid (field 4) out of /proc for each candidate pid",
        )
        self.assertRegex(
            entry, r"trap\s+term_handler\s+TERM",
            "TERM must be trapped in this shell (not backgrounded) so the walk can complete "
            "before tini's tracked child exits",
        )
        self.assertRegex(
            entry, r"(?s)term_handler\(\)\s*\{[^}]*relay_sigterm_to_cron_descendants[^}]*wait \"\$UVICORN_PID\"",
            "the TERM handler must run the descendant walk before waiting on uvicorn to exit",
        )
        self.assertLess(
            entry.index("CRON_PID"), entry.index("uvicorn app.ui_web.app:app"),
            "cron's pid must be resolved before uvicorn starts",
        )

    def test_relay_sigterm_to_cron_descendants_signals_the_whole_live_tree(self):
        # Behavioral, not structural: the tests above pin that the relay is
        # WIRED UP; this exercises what it actually DOES against a real
        # nested process tree, using the actual relay_sigterm_to_cron_
        # descendants() function extracted verbatim out of
        # deploy/entrypoint.sh (not a reimplementation, so it can't drift from
        # what ships) run via a real 3-level `sh` process tree, each level
        # trapping TERM and touching its own marker file.
        #
        # NOTE on what this does NOT prove: the two-pass design (U28) exists
        # to fix a race where a combined walk-and-signal loop only ever
        # reached cron's immediate child, because signalling a process
        # re-parents its still-live children to the subreaper before the next
        # BFS level looks for them. That race is a function of how much wall-
        # clock time elapses per /proc scan relative to how fast a killed
        # process's children get reparented — inherently environment-timing-
        # dependent (process-table size, awk-fork cost, scheduler load), and
        # was NOT reproducible by mutating this test back to the single-pass
        # version on this host (confirmed while writing it: the single-pass
        # version passed here too, only reproduced against real Docker's much
        # larger process table). This test still deterministically catches
        # any regression that breaks multi-level delivery outright (wrong
        # /proc field, a broken frontier loop, only ever reaching level 1) —
        # it just cannot be relied on to catch a reintroduced one-pass
        # walk-and-kill specifically; that needs the real-Docker verification
        # described in this PR.
        shell = shutil.which("sh") or shutil.which("bash")
        if not shell:
            self.skipTest("no POSIX shell available to run the relay function")
        entry_text = ENTRYPOINT.read_text(encoding="utf-8")
        start = entry_text.index("relay_sigterm_to_cron_descendants() {")
        end_marker = "\n}\n"
        end_idx = entry_text.index(end_marker, start)
        relay_fn = entry_text[start:end_idx + len(end_marker)]
        self.assertIn("kill -TERM", relay_fn, "extraction boundary missed the function body")

        with tempfile.TemporaryDirectory(prefix="gs-relay-") as tmp:
            markers = Path(tmp, "markers")
            markers.mkdir()
            root_pid_file = Path(tmp, "root.pid")
            # `daemon` stands in for the cron daemon itself: CRON_PID is set
            # to ITS pid, and — matching production, where cron itself must
            # not be killed, only its job descendants — it deliberately has
            # no trap/marker. level1/2/3 stand in for the job's own
            # multi-level descendant chain (cron's per-job fork -> the job
            # shell -> a step inside it, e.g. `timeout`'s child).
            tree_script = f"""#!/bin/sh
MARKER_DIR="{markers.as_posix()}"
level3() {{
  trap ': > "$MARKER_DIR/level3"; exit 0' TERM
  sleep 60
}}
level2() {{
  trap ': > "$MARKER_DIR/level2"; exit 0' TERM
  level3 &
  sleep 60
}}
level1() {{
  trap ': > "$MARKER_DIR/level1"; exit 0' TERM
  level2 &
  sleep 60
}}
daemon() {{
  level1 &
  sleep 60
}}
daemon &
echo $! > "{root_pid_file.as_posix()}"
wait
"""
            tree_path = Path(tmp, "tree.sh")
            tree_path.write_text(tree_script, encoding="utf-8")
            tree_proc = subprocess.Popen([shell, str(tree_path)], cwd=str(REPO))
            try:
                deadline = time.time() + 5
                while time.time() < deadline and not root_pid_file.exists():
                    time.sleep(0.05)
                self.assertTrue(root_pid_file.exists(), "test process tree never started")
                # Give level2/level3 a moment to finish forking.
                deadline = time.time() + 5
                while time.time() < deadline and len(list(Path(tmp).glob("*.pid"))) < 1:
                    time.sleep(0.05)
                time.sleep(0.3)
                cron_pid = root_pid_file.read_text(encoding="utf-8").strip()

                relay_script = f'#!/bin/sh\nCRON_PID="{cron_pid}"\n{relay_fn}\nrelay_sigterm_to_cron_descendants\n'
                relay_path = Path(tmp, "relay.sh")
                relay_path.write_text(relay_script, encoding="utf-8")
                result = subprocess.run(
                    [shell, str(relay_path)], cwd=str(REPO),
                    capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(0, result.returncode, f"relay script failed: {result.stderr}")

                deadline = time.time() + 5
                while time.time() < deadline and len(list(markers.iterdir())) < 3:
                    time.sleep(0.05)
                found = sorted(p.name for p in markers.iterdir())
                self.assertEqual(
                    ["level1", "level2", "level3"], found,
                    f"the relay must signal EVERY level of a live descendant tree, not just "
                    f"the top one (the exact bug U28's two-pass design fixes); got {found}",
                )
            finally:
                tree_proc.terminate()
                try:
                    tree_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    tree_proc.kill()

    def test_entrypoint_snapshots_environment_for_cron(self):
        # The snapshot lives outside the repo tree and is locked down BEFORE
        # anything is written into it, so no secret is ever readable at a laxer
        # mode and none reaches a tracked file (R10.8). U13: an explicit
        # allowlist replaces a bare `export -p` snapshot of the whole process
        # environment, so anything set in this process that scheduled jobs do
        # not actually read (e.g. build/orchestration secrets) never lands in
        # the file cron sources.
        entry = self._entrypoint()
        self.assertNotRegex(
            entry, r"export -p > /etc/gridsignals\.env",
            "the whole-environment snapshot must be replaced by an explicit allowlist",
        )
        self.assertIn(
            f"export -p | grep -E", entry,
            "the snapshot must filter export -p through an explicit allowlist",
        )
        self.assertIn(f">> {ENV_SNAPSHOT}", entry, "the filtered snapshot must be written to the env file")
        self.assertIn(f"chmod 600 {ENV_SNAPSHOT}", entry, "the environment snapshot may hold secrets")
        self.assertLess(
            entry.index(f"chmod 600 {ENV_SNAPSHOT}"), entry.index(f">> {ENV_SNAPSHOT}"),
            "the snapshot must be chmod'ed before anything is written",
        )
        self.assertLess(
            entry.index(ENV_SNAPSHOT), self._scheduler_start(entry),
            "the snapshot must exist before the daemon that reads it starts",
        )

    def test_entrypoint_env_allowlist_excludes_audit_secrets(self):
        # U26: the audit judge is deliberately NOT scheduled, so cron's env
        # snapshot has no business seeing ANTHROPIC_API_KEY or any
        # GRIDSIGNALS_AUDIT_* knob — pin their absence so a future session
        # cannot silently widen the allowlist back toward `export -p`.
        entry = self._entrypoint()
        allowlist_line = next(
            (ln for ln in entry.splitlines() if "GRIDSIGNALS_ENV_ALLOWLIST=" in ln), None)
        self.assertIsNotNone(allowlist_line, "no explicit env allowlist variable found in the entrypoint")
        self.assertNotIn("ANTHROPIC_API_KEY", allowlist_line)
        self.assertNotIn("GRIDSIGNALS_AUDIT", allowlist_line)
        self.assertIn("GRIDSIGNALS_DB", allowlist_line, "GRIDSIGNALS_DB is required by every scheduled step")

    def test_entrypoint_env_allowlist_includes_the_heartbeat_override(self):
        # deploy/scheduled_run.sh documents GRIDSIGNALS_HEARTBEAT as
        # overridable, matching GRIDSIGNALS_LOCK/GRIDSIGNALS_PIPELINE_LOCK —
        # both of which ARE in the allowlist. Without this, a container-level
        # override would be honored for the first-load invocation (runs in
        # entrypoint.sh's own process env) but silently dropped for every cron
        # tick (which only sees the allowlist-filtered snapshot), so the two
        # invocations of the same guard would write heartbeats to two
        # different files.
        entry = self._entrypoint()
        allowlist_line = next(
            (ln for ln in entry.splitlines() if "GRIDSIGNALS_ENV_ALLOWLIST=" in ln), None)
        self.assertIsNotNone(allowlist_line, "no explicit env allowlist variable found in the entrypoint")
        self.assertIn(
            "GRIDSIGNALS_HEARTBEAT", allowlist_line,
            "GRIDSIGNALS_HEARTBEAT must be in the allowlist alongside the two lock overrides",
        )

    def test_entrypoint_scheduler_failure_does_not_block_serving(self):
        # The script runs under `set -e`, so an unguarded scheduler step would
        # crash-loop the container on a read-only /etc, a future USER directive,
        # or a locked cron pidfile. A stale feed is bad; an unreachable app is
        # worse. The && chain also stops a failed chmod from leaving the
        # environment snapshot (which may hold an API key) world-readable.
        entry = self._entrypoint()
        self.assertRegex(
            entry, r"(?s)if\s*:\s*>\s*/etc/gridsignals\.env.*?&&\s*cron\s*\n\s*then",
            "the scheduler startup must be a guarded conditional, not bare commands under `set -e`",
        )
        self.assertRegex(
            entry, r"WARN: scheduler",
            "a scheduler that fails to start must degrade to a warning, matching the pipeline's convention",
        )

    def test_scheduler_does_not_displace_first_load_ingest(self):
        # Pin the runtime first-load branch verbatim: adding a scheduler must not
        # regress "serve immediately, ingest in the background on an empty feed".
        # U15: the pipeline invocation is routed through deploy/scheduled_run.sh
        # (the same tick-lock guard the cron entry uses) so first-load and a
        # cron tick contend via the SAME lock, not only the per-step one.
        entry = self._entrypoint()
        self.assertIn(
            "sh deploy/scheduled_run.sh sh deploy/ingest_pipeline.sh > /tmp/gridsignals-ingest.log 2>&1 &",
            entry,
            "the first-load background ingest branch changed",
        )
        self.assertLess(
            entry.index("app.first_load"), entry.index("deploy/ingest_pipeline.sh"),
            "first-load ingest must stay gated on the empty-feed check",
        )
        self.assertLess(
            entry.index("deploy/ingest_pipeline.sh"), self._scheduler_start(entry),
            "the scheduler is added after the first-load branch; it does not replace it",
        )

    def test_first_load_ingest_runs_under_the_lock_guard(self):
        # U15: first-load ingest must go through deploy/scheduled_run.sh, the
        # same tick-lock guard the crontab's writer job uses, so a first-load
        # run and a cron tick can never become two writers on the same store.
        entry = self._entrypoint()
        self.assertRegex(
            entry, r"sh deploy/scheduled_run\.sh sh deploy/ingest_pipeline\.sh",
            "first-load ingest bypasses the tick-lock guard",
        )

    def test_dockerfile_installs_the_cron_daemon_and_crontab(self):
        # python:3.12-slim ships NO cron binary. Without this the crontab passes
        # every file-content test above and still never runs.
        df = self._dockerfile()
        self.assertRegex(
            df, r"apt-get install[^\n]*\bcron\b",
            "the base image has no cron binary — the schedule would be inert",
        )
        instructions = "\n".join(ln for ln in df.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotRegex(
            instructions, r"\bcrond\b",
            "Debian's daemon is `cron`; `crond` is the busybox/RHEL name and does not exist here",
        )
        self.assertRegex(
            df, r"tr -d '\\r' < deploy/crontab > /etc/cron\.d/",
            "the crontab must be installed CR-stripped into /etc/cron.d — the build context is "
            "whatever working tree the build was handed, and a CRLF entry never fires",
        )
        self.assertRegex(df, r"chmod 644 /etc/cron\.d/", "cron ignores an /etc/cron.d entry that is not mode 644")
        installed = set(re.findall(r"/etc/cron\.d/([^\s'\"]+)", df))
        self.assertTrue(installed, "the Dockerfile installs no crontab")
        for name in installed:
            self.assertNotIn(".", name, f"cron ignores /etc/cron.d entries whose name contains a dot: {name}")

    def test_dockerfile_uses_tini_as_pid_1(self):
        # U13: without a real init, cron double-forks and orphans to PID 1 and
        # `exec uvicorn` then BECOMES PID 1 itself — a container stop SIGKILLs
        # whichever process that is before its `finally: os.remove(lock)`
        # (app/ingest/runner.py's ingest_lock) can run, leaving a stale lock.
        # tini as the actual ENTRYPOINT (not just installed) reaps orphans and
        # forwards signals so a stop can be a clean SIGTERM instead. `-g`
        # forwards to tini's whole process group (not just its one tracked
        # child), which is what actually reaches the backgrounded first-load
        # ingest job — empirically verified against real Docker; without -g
        # that job gets zero signal, only SIGKILLed by PID-namespace teardown.
        # It does NOT reach a cron-spawned tick (cron daemonizes into its own
        # session/process group) — deploy/entrypoint.sh closes that gap itself
        # (U28, see test_entrypoint_relays_sigterm_to_cron below), not this flag.
        df = self._dockerfile()
        self.assertRegex(df, r"apt-get install[^\n]*\btini\b", "tini must be installed — the base image ships no init")
        self.assertRegex(
            df, r'ENTRYPOINT\s*\[\s*"tini"\s*,\s*"-g"\s*,\s*"--"\s*\]',
            "tini must run with -g (process-group signal forwarding), or a stop never reaches the backgrounded first-load job",
        )
        self.assertIn('CMD ["sh", "deploy/entrypoint.sh"]', df, "the app still launches via deploy/entrypoint.sh, run under tini")

    def test_dockerfile_installs_logrotate_for_the_cron_log(self):
        # U13: deploy/crontab appends to /var/log/gridsignals-cron.log forever
        # in a long-lived container with nothing to cap it. logrotate's package
        # wires its own daily cron.daily hook, so installing it plus dropping a
        # config in is the whole fix.
        df = self._dockerfile()
        self.assertRegex(df, r"apt-get install[^\n]*\blogrotate\b", "logrotate must be installed")
        self.assertRegex(
            df, r"tr -d '\\r' < deploy/logrotate\.conf > /etc/logrotate\.d/",
            "the logrotate config must be installed CR-stripped, matching the crontab's own install step",
        )
        conf = (REPO / "deploy" / "logrotate.conf").read_text(encoding="utf-8")
        self.assertIn("/var/log/gridsignals-cron.log", conf, "the config must target the cron log the crontab writes")
        self.assertRegex(conf, r"\bdaily\b|\bweekly\b", "the log must rotate on a bounded cadence")
        self.assertIn("rotate", conf, "the config must cap how many rotated copies are kept")
        self.assertIn("compress", conf, "rotated copies should be compressed")

    # Both guard locks are redirected into a temp dir for every one of these:
    # a stray data/.ingest.lock in a checkout turns ~25 ui_web tests red with
    # assertions that blame the feature under test.
    def _run_guard(self, tmp, *job):
        shell = shutil.which("sh") or shutil.which("bash")
        if not shell:
            self.skipTest("no POSIX shell available to execute deploy/scheduled_run.sh")
        self.assertTrue(SCHEDULED_RUN.exists(), "deploy/scheduled_run.sh missing — scheduled runs are unguarded")
        env = dict(
            os.environ,
            GRIDSIGNALS_LOCK=os.path.join(tmp, ".ingest.lock").replace("\\", "/"),
            GRIDSIGNALS_PIPELINE_LOCK=os.path.join(tmp, ".scheduled.lock").replace("\\", "/"),
            GRIDSIGNALS_HEARTBEAT=os.path.join(tmp, ".cron_heartbeat").replace("\\", "/"),
        )
        return subprocess.run(
            [shell, "deploy/scheduled_run.sh", *(job or (["echo", SENTINEL]))],
            cwd=str(REPO), env=env, capture_output=True, text=True,
        )

    @staticmethod
    def _backdate(path, hours):
        stamp = time.time() - hours * 3600
        os.utime(path, (stamp, stamp))

    @staticmethod
    def _job_ran(result) -> bool:
        # Exact-line match: the guard also ECHOES the command it is about to run
        # ("scheduled-run: starting echo GUARD-RAN-THE-JOB"), so a substring test
        # would report the job as having run when it was only announced.
        return SENTINEL in result.stdout.splitlines()

    def test_scheduled_run_skips_cleanly_when_the_ingest_lock_is_held(self):
        # A tick colliding with a manual/first-load run must be a recorded skip,
        # not a propagated RuntimeError that aborts the pipeline mid-way.
        with tempfile.TemporaryDirectory(prefix="gs-sched-") as tmp:
            Path(tmp, ".ingest.lock").write_text("{}", encoding="utf-8")
            result = self._run_guard(tmp)
        self.assertEqual(0, result.returncode, f"a lock collision must not fail the tick: {result.stderr}")
        self.assertIn("skipped", result.stdout, "the skip must be recorded, not silent")
        self.assertIn(".ingest.lock", result.stdout, "the skip must name the lock it saw")
        self.assertFalse(self._job_ran(result), "the job ran anyway despite a live lock")

    def test_scheduled_run_proceeds_when_the_ingest_lock_is_stale(self):
        # The anti-wedge half of the same branch. app/ingest/runner.py breaks a
        # lock older than 2h, so the guard must not skip forever on the residue
        # of a crashed run — that is the "never re-ingests" defect this whole
        # unit exists to close, arriving from the other direction.
        with tempfile.TemporaryDirectory(prefix="gs-sched-") as tmp:
            lock = Path(tmp, ".ingest.lock")
            lock.write_text("{}", encoding="utf-8")
            self._backdate(lock, hours=3)
            result = self._run_guard(tmp)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("stale", result.stdout, "the guard must say it decided the lock was abandoned")
        self.assertTrue(self._job_ran(result), "a stale lock must not stop the scheduled run")

    def test_scheduled_run_removes_the_lock_it_declares_abandoned(self):
        # Announcing the lock abandoned and leaving it in place is not a
        # decision, it is a lie: the Python steps the guard then invokes take
        # that same lock and raise on it. The 12 ingest steps and
        # aggregates/digest swallow that with `|| echo WARN`, so the tick keeps
        # going over a dataset that never moved; classify -> obligations ->
        # scoring -> plays are hard under `set -e` and abort the tick non-zero
        # instead. Either way the run is a loss - the swallowing half is just
        # the one that hides it.
        with tempfile.TemporaryDirectory(prefix="gs-sched-") as tmp:
            lock = Path(tmp, ".ingest.lock")
            lock.write_text("{}", encoding="utf-8")
            self._backdate(lock, hours=3)
            result = self._run_guard(tmp)
            self.assertFalse(
                lock.exists(),
                "the guard declared the ingestion lock abandoned but left it in place — "
                "the steps it then runs still see a held lock and raise",
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_guard_stale_window_matches_the_runner_lock_window(self):
        # Two staleness rules, two files: the guard decides whether to run the
        # tick, the runner decides whether to break the lock. If they drift, one
        # of them is wrong at every tick in the gap between them.
        minutes = re.search(r"(?m)^STALE_MINUTES=(\d+)", SCHEDULED_RUN.read_text(encoding="utf-8"))
        self.assertIsNotNone(minutes, "deploy/scheduled_run.sh no longer defines STALE_MINUTES")
        self.assertEqual(
            LOCK_STALE_S, int(minutes.group(1)) * 60,
            "deploy/scheduled_run.sh STALE_MINUTES and app/ingest/runner.py LOCK_STALE_S "
            "must describe the same window",
        )

    def test_scheduled_run_skips_a_second_tick_while_the_first_holds_the_pipeline_lock(self):
        # The R3.2 ingestion lock is taken and released PER STEP, so probing it
        # once at tick start cannot keep two pipelines apart. The guard owns a
        # tick-scoped lock for exactly that. Nested rather than timed: the outer
        # guard holds the lock while its job (a second guard) runs, which is the
        # real overlap without a sleep to go flaky on.
        with tempfile.TemporaryDirectory(prefix="gs-sched-") as tmp:
            result = self._run_guard(
                tmp, "sh", "deploy/scheduled_run.sh", "echo", SENTINEL,
            )
            self.assertFalse(
                Path(tmp, ".scheduled.lock").exists(),
                "the guard must release its tick lock on exit, or one run wedges the schedule",
            )
        self.assertEqual(0, result.returncode, f"a second tick must not fail the first: {result.stderr}")
        self.assertIn("already in progress", result.stdout, "the overlapping tick must record why it stopped")
        self.assertFalse(self._job_ran(result), "two scheduled pipelines ran against the same store")

    def test_scheduled_run_runs_the_job_when_no_lock_is_held(self):
        # The other half of the guard: it must not swallow the run in the normal
        # case, or the schedule is decorative.
        with tempfile.TemporaryDirectory(prefix="gs-sched-") as tmp:
            result = self._run_guard(tmp)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(self._job_ran(result), "the guard did not run its job")

    # -- U23: tick-lock staleness by PID liveness, not mtime -----------------

    def test_scheduled_run_pipeline_lock_dead_pid_is_broken(self):
        # A dead PID is dead regardless of the lock file's age — write it
        # FRESH (no backdating) to prove this is decided by liveness, not
        # timing. 999999 is not a real process on any host running this test.
        with tempfile.TemporaryDirectory(prefix="gs-sched-") as tmp:
            Path(tmp, ".scheduled.lock").write_text("999999\n", encoding="utf-8")
            result = self._run_guard(tmp)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("dead tick lock", result.stdout, "a dead-PID lock must be identified and broken")
        self.assertTrue(self._job_ran(result), "a dead-PID lock must not stop the scheduled run")

    def test_scheduled_run_pipeline_lock_live_pid_is_not_broken(self):
        # A lock recording a genuinely live PID must be respected, not broken.
        # Nested rather than a PID written from this test process directly:
        # this host's POSIX layer (Git Bash / MSYS) can only `kill -0` a
        # process within its own spawned process tree, not an arbitrary
        # unrelated PID (even a real, running one) — so the live PID under
        # test is the OUTER guard's own $$, which take_lock() records
        # automatically at acquisition and which is unquestionably alive for
        # the whole nested call. If pipeline_lock_dead() wrongly reported a
        # live lock as dead, this would break it and run the job anyway.
        with tempfile.TemporaryDirectory(prefix="gs-sched-") as tmp:
            result = self._run_guard(
                tmp, "sh", "deploy/scheduled_run.sh", "echo", SENTINEL,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("already in progress", result.stdout, "a live-PID lock must be respected, not broken")
        self.assertFalse(self._job_ran(result), "a live-PID lock must stop the scheduled run")

    def test_scheduled_run_pipeline_lock_records_its_own_pid(self):
        # take_lock must write $$ into the lock, not an empty file, or the two
        # tests above have nothing to check liveness against.
        with tempfile.TemporaryDirectory(prefix="gs-sched-") as tmp:
            lock = os.path.join(tmp, ".scheduled.lock")
            result = self._run_guard(tmp, "sh", "-c", f'cat "{lock}"')
        self.assertEqual(0, result.returncode, result.stderr)
        digit_lines = [ln for ln in result.stdout.splitlines() if ln.strip().isdigit()]
        self.assertTrue(digit_lines, f"the tick lock must record a numeric PID at acquisition; stdout={result.stdout!r}")
        self.assertGreater(int(digit_lines[0]), 0)

    # -- U29: TOCTOU-free lock creation ---------------------------------------

    def test_take_lock_publishes_content_atomically(self):
        # The old `set -C; printf ... > lock` idiom opened the FINAL lock path
        # and wrote its PID into it as two separate syscalls, so a racing
        # reader could observe the target path already existing but still
        # empty and misclassify a live lock as the dead/empty-file residue
        # U23 already handles — reopening the same second-writer race in a
        # narrower window. The fix writes to a scratch file first and
        # publishes it with `ln` (atomic create-if-absent, like `set -C`),
        # so the target name never exists with any content but the final one.
        text = SCHEDULED_RUN.read_text(encoding="utf-8")
        self.assertRegex(
            text, r'take_lock\(\)\s*\{',
            "deploy/scheduled_run.sh no longer defines take_lock()",
        )
        self.assertRegex(
            text, r"\bln\s+\"\$tmp\"\s+\"\$1\"",
            "take_lock must publish the lock via an atomic `ln`, not a direct create+write "
            "against the final path",
        )
        self.assertNotRegex(
            text, r"set -C;\s*printf",
            "the old two-syscall noclobber-create-then-write idiom must be gone",
        )

    def test_take_lock_still_records_its_own_pid_via_the_new_path(self):
        # Regression guard for the U29 rewrite: the atomically-published lock
        # must still hold the acquiring process's PID, not an empty scratch
        # artifact — test_scheduled_run_pipeline_lock_records_its_own_pid
        # covers this through the guard's normal flow; this pins it directly
        # against take_lock so a future refactor of the guard around it
        # cannot silently stop exercising the same path.
        with tempfile.TemporaryDirectory(prefix="gs-sched-") as tmp:
            lock = os.path.join(tmp, ".scheduled.lock")
            result = self._run_guard(tmp, "sh", "-c", f'cat "{lock}"')
        self.assertEqual(0, result.returncode, result.stderr)
        digit_lines = [ln for ln in result.stdout.splitlines() if ln.strip().isdigit()]
        self.assertTrue(digit_lines, f"the ln-published lock must still record a numeric PID; stdout={result.stdout!r}")

    def test_take_lock_leaves_no_scratch_file_behind(self):
        # `ln` leaves the scratch source name in place alongside the target
        # (same inode, two links) until it is explicitly unlinked. take_lock
        # must clean it up on both the success and failure paths, or every
        # tick leaks a `<lock>.<pid>.tmp` file into data/.
        with tempfile.TemporaryDirectory(prefix="gs-sched-") as tmp:
            self._run_guard(tmp)
            leftovers = [p for p in os.listdir(tmp) if p.endswith(".tmp")]
        self.assertFalse(leftovers, f"take_lock left scratch files behind: {leftovers}")

    # -- U13: heartbeat + timeout ---------------------------------------------

    def test_scheduled_run_writes_a_heartbeat_on_every_attempt(self):
        # Several source_policies carry a ttl shorter than the daily cadence,
        # so the feed reads "stale" for most of every day even when cron is
        # perfectly healthy — "silently dead" and "no tick due yet" need a
        # separate signal. The heartbeat must land on every invocation,
        # success OR guard-skip, not only a completed run.
        with tempfile.TemporaryDirectory(prefix="gs-sched-") as tmp:
            heartbeat = Path(tmp, ".cron_heartbeat")
            self._run_guard(tmp)
            self.assertTrue(heartbeat.exists(), "a normal run must write a heartbeat")
            self.assertRegex(
                heartbeat.read_text(encoding="utf-8").strip(),
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
                "heartbeat must be UTC ISO-8601 (R10.2)",
            )

            # A guard-skip (live ingestion lock) must ALSO touch the heartbeat.
            Path(tmp, ".ingest.lock").write_text("{}", encoding="utf-8")
            result = self._run_guard(tmp)
            self.assertIn("skipped", result.stdout)
            self.assertRegex(
                heartbeat.read_text(encoding="utf-8").strip(),
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
                "a guard-skip must still update the heartbeat",
            )

    def test_scheduled_run_bounds_the_job_with_a_timeout(self):
        # An ingest exceeding ~2h gets classified stale by BOTH this guard's
        # STALE_MINUTES window and app/ingest/runner.py's LOCK_STALE_S,
        # opening a second-writer race. `timeout` kills a hung tick
        # comfortably before that window opens.
        text = SCHEDULED_RUN.read_text(encoding="utf-8")
        match = re.search(r"(?m)^TIMEOUT_MINUTES=(\d+)", text)
        self.assertIsNotNone(match, "deploy/scheduled_run.sh no longer defines TIMEOUT_MINUTES")
        self.assertLess(
            int(match.group(1)), 120,
            "the timeout must be comfortably under the 2h lock staleness window",
        )
        self.assertRegex(
            text, r'timeout\s+"\$\{TIMEOUT_MINUTES\}m"\s+"\$@"',
            "the job invocation must actually be wrapped in `timeout`, not just define the constant",
        )

    def test_build_is_fetch_free(self):
        # The whole point of runtime first-load: no ingest at build. A `RUN`
        # invoking an ingest module would re-introduce a network-dependent build.
        df = self._dockerfile()
        self.assertNotRegex(
            df, r"RUN[^\n]*app\.ingest\.",
            "the image build must not run ingestion (data is populated at runtime)",
        )

    def test_docker_context_excludes_runtime_data(self):
        # COPY . . must not bake ignored local runtime state into the image.
        ignore = DOCKERIGNORE.read_text(encoding="utf-8")
        for pattern in (
            "data/*.db",
            "data/*.db-wal",
            "data/*.db-shm",
            "data/digests/",
            "data/backups/",
            "data/.ingest.lock",
        ):
            self.assertIn(pattern, ignore)

    def test_package_workflow_probes_fastapi_health(self):
        workflow = PACKAGE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("/healthz", workflow)
        self.assertNotIn("/_stcore/health", workflow)

    def test_package_workflow_feed_probe_uses_yaml_safe_run_block(self):
        workflow = PACKAGE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('echo "OK: feed page served"', workflow)
        self.assertNotIn(
            'run: curl -sf http://127.0.0.1:8000/ > /dev/null && echo "OK: feed page served"',
            workflow,
        )

    def test_dockerfile_and_deploy_agree_on_port(self):
        port = re.search(r"ENV PORT=(\d+)", self._dockerfile())
        self.assertIsNotNone(port, "Dockerfile no longer sets ENV PORT")
        deploy = (REPO / "deploy" / "azure-deploy.ps1").read_text(encoding="utf-8")
        self.assertRegex(
            deploy, rf"\$Port\s*=\s*{port.group(1)}\b",
            "deploy/azure-deploy.ps1 $Port must equal Dockerfile PORT",
        )

    def test_deploy_enables_acr_arm_auth_for_managed_identity_pull(self):
        deploy = (REPO / "deploy" / "azure-deploy.ps1").read_text(encoding="utf-8")
        self.assertRegex(
            deploy,
            r"az\s+acr\s+config\s+authentication-as-arm\s+update\b[^\r\n]*--status\s+enabled\b",
            "App Service managed-identity ACR pulls require ARM audience token auth",
        )

    def test_entra_auth_docs_use_current_authv2_action(self):
        readme = DEPLOY_README.read_text(encoding="utf-8")
        self.assertIn("az extension add --name authV2", readme)
        self.assertIn("--unauthenticated-client-action RedirectToLoginPage", readme)
        self.assertNotIn("--action RequireAuthentication", readme)

    def test_deploy_docs_match_runtime_first_load_model(self):
        readme = DEPLOY_README.read_text(encoding="utf-8")
        deploy = AZURE_DEPLOY.read_text(encoding="utf-8")
        for text in (readme, deploy):
            self.assertIn("first load", text.lower())
            self.assertNotIn("SQLite store is baked into the image", text)
            self.assertNotIn("build pipeline", text.lower())
            self.assertNotIn("runs the live ingest pipeline, may take", text)
            self.assertNotIn("live public feeds during the image build", text)


if __name__ == "__main__":
    unittest.main()
