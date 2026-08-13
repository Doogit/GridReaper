"""Contract tests for the Azure/Docker packaging (see Dockerfile, deploy/).

Hermetic and no-Docker (like the rest of the suite) so drift between the repo
and what the image build assumes fails fast and locally, not on a cloud build.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "Dockerfile"
DEPLOY_README = REPO / "deploy" / "README.md"
PACKAGE_WORKFLOW = REPO / ".github" / "workflows" / "package.yml"


class PackagingContractTest(unittest.TestCase):
    def _dockerfile(self) -> str:
        self.assertTrue(DOCKERFILE.exists(), "Dockerfile missing — Azure packaging cannot build")
        return DOCKERFILE.read_text(encoding="utf-8")

    def test_config_seeds_present(self):
        # load_seeds bakes the config layer from these at build time.
        for rel in (
            "seeds/watchlist_entities.csv",
            "seeds/products.csv",
            "seeds/triggers.csv",
            "seeds/license_matrix.csv",
            "seeds/scoring_weights.csv",
        ):
            self.assertTrue((REPO / rel).exists(), f"seed {rel} referenced by the build is missing")

    def test_pipeline_modules_present(self):
        # Every module the Dockerfile invokes (CMD entrypoint + `python -m`
        # steps) must exist.
        for rel in (
            "app/ui_web/app.py",
            "app/db/load_seeds.py",
            "app/licensing.py",
            "app/scoring.py",
            "app/plays.py",
            "app/digest.py",
            "app/classify/regulatory.py",
            "app/classify/leadership.py",
            "app/classify/company_statement.py",
            "app/classify/security_rss.py",
            "app/ingest/security_rss.py",
        ):
            self.assertTrue((REPO / rel).exists(), f"module {rel} the build invokes is missing")

    def test_build_runs_company_statement_classifier_before_scoring(self):
        # Company-statement incident cards are minted from the same press-wire
        # backfill as leadership; the baked demo DB must run the classifier
        # before scoring and plays snapshot the feed.
        df = self._dockerfile()
        self.assertIn("app.classify.company_statement", df)
        self.assertLess(
            df.index("app.classify.company_statement"), df.index("app.scoring"),
            "company-statement classification must run before scoring",
        )

    def test_build_runs_security_rss_pipeline_before_scoring(self):
        # Security-press cards come from their own RSS ingestion module. The
        # baked demo DB must run both feeds and the classifier before scoring.
        df = self._dockerfile()
        self.assertIn("app.ingest.security_rss --source therecord", df)
        self.assertIn("app.ingest.security_rss --source bleepingcomputer", df)
        self.assertIn("app.classify.security_rss", df)
        self.assertLess(
            df.index("app.ingest.security_rss --source therecord"),
            df.index("app.classify.security_rss"),
            "security RSS ingestion must run before classification",
        )
        self.assertLess(
            df.index("app.classify.security_rss"), df.index("app.scoring"),
            "security RSS classification must run before scoring",
        )

    def test_build_runs_digest_after_scoring_and_plays(self):
        # The digest (R8.8) is the pipeline's LAST step: it reads the freshest
        # scored cards + play snapshots, so it must run after scoring and plays.
        df = self._dockerfile()
        self.assertIn("app.digest", df, "Dockerfile dropped the digest step (R8.8)")
        self.assertGreater(
            df.index("app.digest"), df.index("app.scoring"),
            "digest must run after scoring",
        )
        self.assertGreater(
            df.index("app.digest"), df.index("app.plays"),
            "digest must run after plays",
        )

    def test_cmd_launches_fastapi_ui(self):
        # Post-cutover (Chunk 7): the image serves the FastAPI + HTMX UI via
        # uvicorn, not Streamlit. Pin it so the deploy entrypoint can't regress.
        df = self._dockerfile()
        self.assertIn("uvicorn app.ui_web.app:app", df, "Dockerfile CMD must launch the FastAPI app via uvicorn")
        self.assertNotIn("streamlit", df, "Streamlit was removed at the Chunk 7 cutover")

    def test_package_workflow_probes_fastapi_health(self):
        workflow = PACKAGE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("/healthz", workflow)
        self.assertNotIn("/_stcore/health", workflow)

    def test_build_runs_licensing_before_plays(self):
        # Silent-failure guard: without `app.licensing`, plays generate ZERO
        # license-play snapshots and the feed loses its play cards, yet the build
        # still succeeds (signals>0). Pin the step so it can't be dropped.
        df = self._dockerfile()
        self.assertIn("app.licensing", df, "Dockerfile dropped the licensing step -> no license plays")
        self.assertLess(
            df.index("app.licensing"), df.index("app.plays"),
            "licensing must run before plays",
        )

    def test_build_asserts_nonempty_feed_and_play_snapshots(self):
        # The build must fail loudly on an incomplete demo rather than ship
        # blank feed cards or cards with no license plays.
        df = self._dockerfile()
        self.assertRegex(df, r"count\(\*\) from signals")
        self.assertRegex(df, r"count\(\*\) from license_play_snapshots")

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


if __name__ == "__main__":
    unittest.main()
