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
                job.index(f". {ENV_SNAPSHOT}"), job.index("deploy/"),
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

    def test_crontab_jobs_run_under_the_lock_guard(self):
        # app/ingest/runner.py RAISES on a live lock and ingest_pipeline.sh runs
        # under `set -e`, so an unguarded tick during a manual/first-load run
        # aborts mid-pipeline.
        for job in self._crontab_jobs():
            self.assertIn("deploy/scheduled_run.sh", job, f"scheduled job bypasses the lock guard: {job}")

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

    def _scheduler_start(self, entry: str) -> int:
        match = re.search(r"&&\s*cron\b", entry)
        self.assertIsNotNone(
            match,
            "entrypoint must start the cron daemon (Debian: cron, not crond), guarded so a "
            "failure cannot take the web process down with it",
        )
        return match.start()

    def test_entrypoint_starts_scheduler_before_exec_uvicorn(self):
        # A scheduler that blocks the web process is worse than no scheduler:
        # cron is started (backgrounded daemon) and uvicorn is still exec'd last.
        entry = self._entrypoint()
        self.assertLess(
            self._scheduler_start(entry), entry.index("exec uvicorn"),
            "the scheduler must start before uvicorn is exec'd, or it never starts at all",
        )
        executable = [ln for ln in entry.splitlines() if ln.strip()]
        self.assertTrue(
            executable[-1].startswith("exec uvicorn"),
            "uvicorn must remain the last (exec'd) step so the web process is PID 1's successor",
        )

    def test_entrypoint_snapshots_environment_for_cron(self):
        # The snapshot lives outside the repo tree and is locked down BEFORE
        # anything is written into it, so no secret is ever readable at a laxer
        # mode and none reaches a tracked file (R10.8).
        entry = self._entrypoint()
        self.assertIn(f"export -p > {ENV_SNAPSHOT}", entry, "cron jobs need an environment snapshot to source")
        self.assertIn(f"chmod 600 {ENV_SNAPSHOT}", entry, "the environment snapshot may hold an API key")
        self.assertLess(
            entry.index(f"chmod 600 {ENV_SNAPSHOT}"), entry.index(f"export -p > {ENV_SNAPSHOT}"),
            "the snapshot must be chmod'ed before it is written",
        )
        self.assertLess(
            entry.index(ENV_SNAPSHOT), self._scheduler_start(entry),
            "the snapshot must exist before the daemon that reads it starts",
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
        entry = self._entrypoint()
        self.assertIn(
            "sh deploy/ingest_pipeline.sh > /tmp/gridsignals-ingest.log 2>&1 &", entry,
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
        # that same lock and raise on it, and every step in
        # deploy/ingest_pipeline.sh swallows failures, so the tick prints
        # "pipeline complete" over a dataset that never moved.
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
