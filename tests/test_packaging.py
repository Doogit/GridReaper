"""Contract tests for the Azure/Docker packaging (see Dockerfile, deploy/).

Hermetic and no-Docker (like the rest of the suite) so drift between the repo
and what the image build assumes fails fast and locally, not on a cloud build.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "Dockerfile"


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
        # Every module the Dockerfile invokes with `python -m` must exist.
        for rel in (
            "app/ui/Home.py",
            "app/db/load_seeds.py",
            "app/licensing.py",
            "app/scoring.py",
            "app/plays.py",
            "app/classify/regulatory.py",
            "app/classify/leadership.py",
        ):
            self.assertTrue((REPO / rel).exists(), f"module {rel} the build invokes is missing")

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

    def test_build_asserts_nonempty_feed(self):
        # The build must fail loudly on an empty feed rather than ship a blank demo.
        self.assertRegex(self._dockerfile(), r"count\(\*\) from signals")

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


if __name__ == "__main__":
    unittest.main()
