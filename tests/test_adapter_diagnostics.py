from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flexgpu.commissioning import demo_calibration, generate_demo_bundle  # noqa: E402
from flexgpu.config import validate_config  # noqa: E402
from flexgpu.diagnostics import run_diagnostics  # noqa: E402


def profile(source: dict[str, object] | None = None, sensor: dict[str, object] | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "topology": "single",
        "experience": "installation",
        "completion": "hybrid",
        "tier": "auto",
        "gpu": {"ai": "auto", "render": "auto"},
        "processes": {"world": {"command": ["python", "show.py"]}},
        "transport": {"type": "local"},
    }
    if source is not None:
        result["source"] = source
    if sensor is not None:
        result["sensor"] = sensor
    return result


class AdapterDiagnosticTests(unittest.TestCase):
    def test_synchronized_replay_and_calibration_are_diagnosed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "capture"
            generate_demo_bundle(bundle, frames=2, width=8, height=8)
            config_path = root / "show.json"
            config = validate_config(
                profile(
                    source={
                        "mode": "replay",
                        "replay_path": "capture/manifest.json",
                        "calibration_path": "capture/calibration.json",
                    }
                ),
                str(config_path),
            )
            checks = {item.code: item for item in run_diagnostics(config, ())}
            self.assertEqual(checks["source.replay_path"].level, "pass")
            self.assertEqual(checks["source.replay.contract"].level, "pass")
            self.assertEqual(checks["source.calibration.contract"].level, "pass")
            self.assertEqual(checks["source.replay.contract"].details["frames"], 2)
            self.assertEqual(checks["source.replay.binding"].level, "warn")

    def test_renamed_replay_manifest_is_still_contract_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "capture"
            generate_demo_bundle(bundle, frames=1, width=8, height=8)
            renamed = bundle / "recording.json"
            (bundle / "manifest.json").replace(renamed)
            config = validate_config(
                profile(source={"mode": "replay", "replay_path": str(renamed)}),
                str(root / "show.json"),
            )
            checks = {item.code: item for item in run_diagnostics(config, ())}
            self.assertEqual(checks["source.replay.contract"].level, "pass")

            renamed.write_text('{"version":"wrong"}', encoding="utf-8")
            checks = {item.code: item for item in run_diagnostics(config, ())}
            self.assertEqual(checks["source.replay.contract"].level, "fail")

    def test_missing_local_private_adapter_fails_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "show.json"
            config = validate_config(
                profile(
                    source={
                        "mode": "streamdiffusion",
                        "auto_load_tox": True,
                        "streamdiffusion_tox": "local-components/StreamDiffusionTD.tox",
                        "rgb_operator": "out_rgb",
                    }
                ),
                str(config_path),
            )
            checks = {item.code: item for item in run_diagnostics(config, ())}
            self.assertEqual(checks["source.streamdiffusion_tox"].level, "fail")

    def test_invalid_calibration_contract_fails_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calibration = root / "calibration.json"
            calibration.write_text(
                json.dumps({"version": "not-supported"}), encoding="utf-8"
            )
            config = validate_config(
                profile(
                    sensor={
                        "mode": "simulated",
                        "calibration_path": "calibration.json",
                    }
                ),
                str(root / "show.json"),
            )
            checks = {item.code: item for item in run_diagnostics(config, ())}
            self.assertEqual(checks["sensor.calibration_path"].level, "pass")
            self.assertEqual(checks["sensor.calibration.contract"].level, "fail")

    def test_mismatched_shared_world_calibration_ids_fail_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "capture"
            generate_demo_bundle(bundle, frames=1, width=8, height=8)
            alternate = demo_calibration(8, 8).to_dict()
            alternate["calibration_id"] = "different-world-epoch"
            alternate_path = root / "alternate.json"
            alternate_path.write_text(json.dumps(alternate), encoding="utf-8")
            config = validate_config(
                profile(
                    source={
                        "mode": "replay",
                        "replay_path": "capture/manifest.json",
                        "calibration_path": "alternate.json",
                    },
                    sensor={
                        "mode": "simulated",
                        "calibration_path": "capture/calibration.json",
                    },
                ),
                str(root / "show.json"),
            )
            checks = {item.code: item for item in run_diagnostics(config, ())}
            self.assertEqual(checks["source.calibration.consistency"].level, "fail")
            self.assertEqual(checks["calibration.shared_world"].level, "fail")


if __name__ == "__main__":
    unittest.main()
