from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flexgpu.commissioning import (  # noqa: E402
    FRAME_STATE_VERSION,
    AdapterFrameState,
    CalibrationProfile,
    CommissioningError,
    demo_calibration,
    generate_demo_bundle,
    load_strict_json,
    validate_bundle,
    validate_frame_sequence,
    write_calibration,
)


class CommissioningContractTests(unittest.TestCase):
    def test_public_example_calibration_is_valid(self) -> None:
        profile = CalibrationProfile.from_mapping(
            load_strict_json(ROOT / "config" / "calibration.example.json")
        )
        self.assertEqual(profile.calibration_id, "demo-camera-v1")
        self.assertEqual((profile.width, profile.height), (64, 36))

    def test_demo_calibration_round_trip_is_metric_and_finite(self) -> None:
        profile = demo_calibration(640, 360)
        restored = CalibrationProfile.from_mapping(profile.to_dict())
        self.assertEqual(restored, profile)
        self.assertEqual(restored.depth_encoding, "normalized")
        self.assertGreater(restored.fx, 0)
        self.assertEqual(restored.camera_to_world[12:], (0.0, 0.0, 0.0, 1.0))

    def test_calibration_rejects_unknown_nonfinite_and_nonhomogeneous_values(self) -> None:
        base = demo_calibration(64, 36).to_dict()
        cases: list[dict[str, object]] = []

        unknown = dict(base)
        unknown["mystery"] = True
        cases.append(unknown)

        nonfinite = json.loads(json.dumps(base))
        nonfinite["intrinsics"]["fx"] = float("nan")
        cases.append(nonfinite)

        transform = json.loads(json.dumps(base))
        transform["camera_to_world"][15] = 0.0
        cases.append(transform)

        singular = json.loads(json.dumps(base))
        singular["sensor_to_world"][:12] = [0.0] * 12
        cases.append(singular)

        overflow = json.loads(json.dumps(base))
        overflow["intrinsics"]["fx"] = 10**4000
        cases.append(overflow)

        bad_depth = json.loads(json.dumps(base))
        bad_depth["depth"]["far_m"] = bad_depth["depth"]["near_m"]
        cases.append(bad_depth)

        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(CommissioningError):
                    CalibrationProfile.from_mapping(value)

    def test_strict_calibration_json_rejects_duplicates_and_constants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration.json"
            for payload in ('{"version":"x","version":"y"}', '{"fx":NaN}'):
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaises(CommissioningError):
                        load_strict_json(path)

    def test_calibration_write_refuses_to_replace_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration.json"
            profile = demo_calibration(32, 18)
            write_calibration(path, profile)
            self.assertEqual(CalibrationProfile.from_mapping(load_strict_json(path)), profile)
            with self.assertRaises(CommissioningError):
                write_calibration(path, profile)

    def test_frame_state_round_trip_and_freshness(self) -> None:
        state = AdapterFrameState(
            session_id="source-a",
            frame_id=7,
            timestamp_ns=1_000_000_000,
            width=64,
            height=36,
            calibration_id="demo-camera-v1",
            valid_fraction=0.75,
            confidence_mean=0.8,
        )
        self.assertEqual(AdapterFrameState.from_mapping(state.to_dict()), state)
        self.assertEqual(state.freshness(1_050_000_000, 100)["state"], "alive")
        self.assertEqual(state.freshness(1_200_000_000, 100)["state"], "stale")
        self.assertEqual(state.freshness(800_000_000, 100)["state"], "future")

    def test_frame_state_contract_rejects_invalid_coverage_and_identifier(self) -> None:
        mapping = {
            "version": FRAME_STATE_VERSION,
            "session_id": "../unsafe",
            "frame_id": 0,
            "timestamp_ns": 1,
            "width": 1,
            "height": 1,
            "calibration_id": "demo",
            "valid_fraction": 1.2,
            "confidence_mean": 0.5,
        }
        with self.assertRaises(CommissioningError):
            AdapterFrameState.from_mapping(mapping)

    def test_frame_sequence_accepts_session_restart_but_rejects_rollback(self) -> None:
        def state(session: str, frame: int, timestamp: int) -> AdapterFrameState:
            return AdapterFrameState(
                session_id=session,
                frame_id=frame,
                timestamp_ns=timestamp,
                width=8,
                height=8,
                calibration_id="demo",
                valid_fraction=1.0,
                confidence_mean=1.0,
            )

        summary = validate_frame_sequence(
            (state("first", 8, 100), state("first", 9, 200), state("second", 0, 300))
        )
        self.assertEqual(summary, {"frames": 3, "sessions": 2, "calibration_id": "demo"})
        with self.assertRaises(CommissioningError):
            validate_frame_sequence((state("first", 8, 100), state("first", 7, 200)))

    def test_generated_bundle_is_synchronized_and_hash_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "demo"
            generated = generate_demo_bundle(
                output, frames=4, width=32, height=18, interval_ms=50
            )
            inspected = validate_bundle(output / "manifest.json")
            self.assertEqual(generated["frames"], 4)
            self.assertEqual(inspected["frames"], 4)
            self.assertEqual(inspected["media_files"], 16)
            self.assertEqual(inspected["duration_ms"], 150.0)
            self.assertTrue(inspected["hashes_verified"])
            manifest = load_strict_json(output / "manifest.json")
            first = manifest["frames"][0]
            self.assertEqual(first["state"]["frame_id"], 0)
            self.assertEqual(set(first).intersection({"rgb", "depth", "mask", "confidence"}),
                             {"rgb", "depth", "mask", "confidence"})

    def test_bundle_detects_tampering_and_missing_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "demo"
            generate_demo_bundle(output, frames=1, width=8, height=8)
            manifest = load_strict_json(output / "manifest.json")
            rgb = output / manifest["frames"][0]["rgb"]["path"]
            tampered = bytearray(rgb.read_bytes())
            tampered[-1] ^= 0xFF
            rgb.write_bytes(tampered)
            with self.assertRaisesRegex(CommissioningError, "SHA-256"):
                validate_bundle(output / "manifest.json")
            self.assertEqual(
                validate_bundle(output / "manifest.json", verify_hashes=False)["frames"], 1
            )
            rgb.write_bytes(rgb.read_bytes() + b"tamper")
            with self.assertRaisesRegex(CommissioningError, "byte length"):
                validate_bundle(output / "manifest.json", verify_hashes=False)
            rgb.unlink()
            with self.assertRaisesRegex(CommissioningError, "missing"):
                validate_bundle(output / "manifest.json", verify_hashes=False)

    def test_bundle_rejects_media_dimensions_that_disagree_with_frame_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "demo"
            generate_demo_bundle(output, frames=1, width=8, height=8)
            manifest_path = output / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            rgb = output / manifest["frames"][0]["rgb"]["path"]
            payload = rgb.read_bytes().replace(b"P6\n8 8\n", b"P6\n4 16\n", 1)
            rgb.write_bytes(payload)
            with self.assertRaisesRegex(CommissioningError, "dimensions"):
                validate_bundle(manifest_path, verify_hashes=False)

    def test_bundle_rejects_media_references_swapped_between_roles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "demo"
            generate_demo_bundle(output, frames=1, width=8, height=8)
            manifest_path = output / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            frame = manifest["frames"][0]
            frame["rgb"], frame["depth"] = frame["depth"], frame["rgb"]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(CommissioningError, "media role"):
                validate_bundle(manifest_path)

    def test_demo_generation_rejects_sub_nanosecond_intervals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "demo"
            with self.assertRaisesRegex(CommissioningError, "too small"):
                generate_demo_bundle(output, frames=2, interval_ms=1e-10)
            self.assertFalse(output.exists())

    def test_bundle_rejects_path_escape_and_nonmonotonic_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "demo"
            generate_demo_bundle(output, frames=2, width=8, height=8)
            manifest_path = output / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["frames"][0]["rgb"]["path"] = "../outside.ppm"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(CommissioningError, "inside"):
                validate_bundle(manifest_path, verify_hashes=False)

            manifest = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )
            # Restore from a fresh bundle because the previous manifest is intentionally bad.
            replacement = Path(directory) / "second"
            generate_demo_bundle(replacement, frames=2, width=8, height=8)
            second_manifest_path = replacement / "manifest.json"
            second_manifest = json.loads(second_manifest_path.read_text(encoding="utf-8"))
            second_manifest["frames"][1]["state"]["frame_id"] = 0
            second_manifest_path.write_text(json.dumps(second_manifest), encoding="utf-8")
            with self.assertRaisesRegex(CommissioningError, "Frame IDs|frame IDs"):
                validate_bundle(second_manifest_path, verify_hashes=False)

    def test_demo_generation_refuses_nonempty_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "demo"
            output.mkdir()
            (output / "owned.txt").write_text("do not replace\n", encoding="utf-8")
            with self.assertRaises(CommissioningError):
                generate_demo_bundle(output)

    def test_commissioning_cli_generates_and_inspects_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "cli-demo"
            tool = ROOT / "tools" / "commission_flexshow.py"
            generated = subprocess.run(
                [
                    sys.executable,
                    str(tool),
                    "demo",
                    "--output",
                    str(output),
                    "--frames",
                    "2",
                    "--width",
                    "8",
                    "--height",
                    "8",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr.decode("utf-8"))
            inspected = subprocess.run(
                [sys.executable, str(tool), "inspect", str(output / "manifest.json")],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(inspected.returncode, 0, inspected.stderr.decode("utf-8"))
            payload = json.loads(inspected.stdout)
            self.assertEqual(payload["status"], "valid")
            self.assertEqual(payload["frames"], 2)


if __name__ == "__main__":
    unittest.main()
