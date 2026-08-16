"""Contract tests for the Azure/Docker packaging (see Dockerfile, deploy/).

Hermetic and no-Docker (like the rest of the suite) so drift between the repo
and what the image build assumes fails fast and locally, not on a cloud build.

Runtime first-load model: the image ships code + config seeds only (no baked
dataset). The Dockerfile CMD runs deploy/entrypoint.sh, which seeds the schema,
background-runs deploy/ingest_pipeline.sh on first load, then execs uvicorn.
The pipeline's ordering invariants (licensing before plays, classify before
score, digest last) live in the pipeline script and are pinned below.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "Dockerfile"
ENTRYPOINT = REPO / "deploy" / "entrypoint.sh"
PIPELINE = REPO / "deploy" / "ingest_pipeline.sh"
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
