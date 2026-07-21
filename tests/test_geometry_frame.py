from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from flexgpu.geometry_frame import (  # noqa: E402
    CONTRACT,
    COORDINATE_SYSTEM,
    SOURCE_COORDINATE_SYSTEM,
    GeometryFrameError,
    load_geometry_manifest,
    validate_geometry_manifest,
    verify_geometry_bundle,
)


def fixture_manifest() -> dict:
    width, height = 4, 3
    shapes = {
        "rgb": [height, width, 4],
        "position_camera": [height, width, 4],
        "depth": [height, width],
        "normal_camera": [height, width, 4],
        "mask": [height, width],
        "confidence": [height, width],
    }
    dtypes = {
        "rgb": "uint8",
        "position_camera": "float32",
        "depth": "float32",
        "normal_camera": "float32",
        "mask": "uint8",
        "confidence": "float32",
    }
    return {
        "contract": CONTRACT,
        "producer_session_id": "probe-1",
        "frame_id": 0,
        "source_session_id": "saved-frame",
        "source_frame_id": 0,
        "source_timestamp_ns": "100",
        "completed_timestamp_ns": "200",
        "generation_id": "prompt-1",
        "width": width,
        "height": height,
        "model": {
            "id": "Ruicheng:moge-2-vits-normal",
            "source_revision": "0" * 40,
            "model_revision": "1" * 40,
            "precision": "fp16",
            "num_tokens": 1200,
            "inference_ms": 12.5,
        },
        "intrinsics_normalized": [0.8, 0.8, 0.5, 0.5],
        "intrinsics_pixels": [3.2, 2.4, 2.0, 1.5],
        "camera_to_world": [
            1, 0, 0, 0,
            0, 1, 0, 0,
            0, 0, 1, 0,
            0, 0, 0, 1,
        ],
        "coordinate_system": COORDINATE_SYSTEM,
        "source_coordinate_system": SOURCE_COORDINATE_SYSTEM,
        "valid_fraction": 1.0,
        "confidence_mean": 1.0,
        "confidence_semantics": "binary_validity_proxy",
        "planes": {
            name: {
                "filename": name + ".npy",
                "dtype": dtypes[name],
                "shape": shape,
                "byte_length": 1,
                "sha256": hashlib.sha256(name.encode()).hexdigest(),
                "semantics": name + " test plane",
            }
            for name, shape in shapes.items()
        },
    }


class GeometryFrameContractTests(unittest.TestCase):
    def test_valid_manifest_round_trips_to_typed_contract(self) -> None:
        frame = validate_geometry_manifest(fixture_manifest())
        self.assertEqual(frame.width, 4)
        self.assertEqual(frame.height, 3)
        self.assertEqual(frame.num_tokens, 1200)
        self.assertEqual(frame.source_revision, "0" * 40)
        self.assertEqual(frame.model_revision, "1" * 40)
        self.assertEqual(set(frame.planes), {
            "rgb", "position_camera", "depth", "normal_camera", "mask", "confidence"
        })

    def test_model_revisions_are_strict_and_canonicalized(self) -> None:
        manifest = fixture_manifest()
        manifest["model"]["source_revision"] = "ABCDEF12" * 5
        manifest["model"]["model_revision"] = "FEDCBA98" * 5
        frame = validate_geometry_manifest(manifest)
        self.assertEqual(frame.source_revision, "abcdef12" * 5)
        self.assertEqual(frame.model_revision, "fedcba98" * 5)

        invalid_revisions = (
            "a" * 39,
            "a" * 41,
            "g" * 40,
            "refs/heads/main",
            123,
            None,
        )
        for field in ("source_revision", "model_revision"):
            for revision in invalid_revisions:
                with self.subTest(field=field, revision=revision):
                    invalid = fixture_manifest()
                    invalid["model"][field] = revision
                    with self.assertRaisesRegex(GeometryFrameError, field):
                        validate_geometry_manifest(invalid)

    def test_unknown_fields_nonfinite_values_and_bad_timestamps_fail_closed(self) -> None:
        cases = []
        unknown = fixture_manifest()
        unknown["private_path"] = "C:/secret"
        cases.append(unknown)
        nonfinite = fixture_manifest()
        nonfinite["model"]["inference_ms"] = float("inf")
        cases.append(nonfinite)
        backwards = fixture_manifest()
        backwards["completed_timestamp_ns"] = "99"
        cases.append(backwards)
        non_ascii_timestamp = fixture_manifest()
        non_ascii_timestamp["source_timestamp_ns"] = "\u0661\u0660\u0660"
        cases.append(non_ascii_timestamp)
        overflowing_number = fixture_manifest()
        overflowing_number["model"]["inference_ms"] = 10**1000
        cases.append(overflowing_number)
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(GeometryFrameError):
                    validate_geometry_manifest(value)

    def test_plane_paths_shapes_and_dtypes_are_bounded(self) -> None:
        cases = []
        escaped = fixture_manifest()
        escaped["planes"]["depth"]["filename"] = "../depth.npy"
        cases.append(escaped)
        wrong_shape = fixture_manifest()
        wrong_shape["planes"]["mask"]["shape"] = [3, 4, 1]
        cases.append(wrong_shape)
        wrong_dtype = fixture_manifest()
        wrong_dtype["planes"]["rgb"]["dtype"] = "object"
        cases.append(wrong_dtype)
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(GeometryFrameError):
                    validate_geometry_manifest(value)

    def test_cross_field_ambiguities_fail_closed(self) -> None:
        inconsistent_intrinsics = fixture_manifest()
        inconsistent_intrinsics["intrinsics_pixels"][0] += 0.01

        aliased_plane = fixture_manifest()
        aliased_plane["planes"]["mask"]["filename"] = "DEPTH.NPY"

        singular_transform = fixture_manifest()
        singular_transform["camera_to_world"][0:12] = [0] * 12

        for value in (inconsistent_intrinsics, aliased_plane, singular_transform):
            with self.subTest(value=value):
                with self.assertRaises(GeometryFrameError):
                    validate_geometry_manifest(value)

    def test_non_json_keys_and_unhashable_enums_raise_contract_errors(self) -> None:
        non_string_key = fixture_manifest()
        non_string_key[1] = "not a JSON object key"

        unhashable_precision = fixture_manifest()
        unhashable_precision["model"]["precision"] = []

        unhashable_dtype = fixture_manifest()
        unhashable_dtype["planes"]["depth"]["dtype"] = []

        for value in (non_string_key, unhashable_precision, unhashable_dtype):
            with self.subTest(value=value):
                with self.assertRaises(GeometryFrameError):
                    validate_geometry_manifest(value)

    def test_strict_loader_rejects_duplicate_keys_and_constants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            for payload in ('{"contract":"a","contract":"b"}', '{"value":NaN}'):
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaises(GeometryFrameError):
                        load_geometry_manifest(path)

    def test_bundle_verification_detects_missing_or_tampered_planes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = fixture_manifest()
            for name, descriptor in manifest["planes"].items():
                payload = name.encode()
                path = root / descriptor["filename"]
                path.write_bytes(payload)
                descriptor["byte_length"] = len(payload)
                descriptor["sha256"] = hashlib.sha256(payload).hexdigest()
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            frame = verify_geometry_bundle(manifest_path)
            self.assertEqual(frame.frame_id, 0)

            (root / manifest["planes"]["depth"]["filename"]).write_bytes(b"tampered")
            with self.assertRaisesRegex(GeometryFrameError, "depth"):
                verify_geometry_bundle(manifest_path)


if __name__ == "__main__":
    unittest.main()
