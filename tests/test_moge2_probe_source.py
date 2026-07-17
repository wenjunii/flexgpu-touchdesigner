from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tools" / "moge2_probe.py"


class MoGe2ProbeSourceTests(unittest.TestCase):
    def test_profiles_action_has_no_site_package_or_model_dependency(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-S", str(PROBE), "profiles"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["profiles"]["3080ti_16gb"]["num_tokens"], 1200)
        self.assertEqual(payload["profiles"]["3080ti_16gb"]["max_edge"], 384)
        self.assertEqual(len(payload["pins"]["moge_source_revision"]), 40)
        self.assertEqual(len(payload["pins"]["model_revision"]), 40)
        self.assertEqual(len(payload["pins"]["model_sha256"]), 64)

    def test_source_pins_checkpoint_and_forbids_implicit_runtime_download(self) -> None:
        source = PROBE.read_text(encoding="utf-8")
        for marker in (
            "07444410f1e33f402353b99d6ccd26bd31e469e8",
            "679230677b4d282c6f304189a93e98e14f085902",
            "79a16621928c2bf0ed04659218c55c01075e950507f40bb3332fb4c873d3e1dc",
            'os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"',
            'os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"',
            "verify_model(checkpoint)",
            "apply_mask=True",
            "np.where(mask[..., None], points, 0.0)",
        ):
            self.assertIn(marker, source)
        self.assertEqual(source.count("hf_hub_download("), 1)
        self.assertIn('args.action == "model-install"', source)

    def test_output_is_confined_to_ignored_runtime_tree(self) -> None:
        source = PROBE.read_text(encoding="utf-8")
        self.assertIn("relative_to(RUNTIME_ROOT)", source)
        self.assertIn("DEFAULT_RUNS_PATH", source)
        self.assertNotIn("StreamDiffusionTD.tox", source)


if __name__ == "__main__":
    unittest.main()
