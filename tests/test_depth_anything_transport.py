from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from flexgpu.depth_anything_transport import (  # noqa: E402
    CONFIDENCE_SEMANTICS,
    DEPTH_ENCODING,
    DEPTH_SEMANTICS,
    IDENTITY_4X4,
    MASK_SEMANTICS,
    MAX_HEIGHT,
    MAX_PIXELS,
    MAX_WIDTH,
    SENSOR_CONTRACT,
    DepthAnythingTransportError,
    decode_sensor_frame,
    make_sensor_worldbus_metadata,
    pack_sensor_frame,
    pack_sensor_frame_numpy,
)
from flexgpu.worldbus import make_frame  # noqa: E402


class DepthAnythingTransportTests(unittest.TestCase):
    def test_dimensions_match_the_touchdesigner_receiver_ceiling(self) -> None:
        self.assertEqual((MAX_WIDTH, MAX_HEIGHT, MAX_PIXELS), (640, 480, 307200))
        for width, height in ((641, 1), (1, 481)):
            with self.subTest(size=(width, height)), self.assertRaises(
                DepthAnythingTransportError
            ):
                pack_sensor_frame([], [], [], width=width, height=height)

    def test_standard_and_numpy_packers_are_identical(self) -> None:
        depth = [0.5, 1.0, 2.0, 100.0, float("nan"), 1.5]
        mask = [1, 1, 0, 1, 1, 1]
        confidence = [1.0, 0.5, 1.0, 0.25, 1.0, 0.0]
        expected = pack_sensor_frame(
            depth, mask, confidence, width=3, height=2, depth_scale=0.001
        )
        actual = pack_sensor_frame_numpy(
            depth, mask, confidence, width=3, height=2, depth_scale=0.001
        )
        self.assertEqual(actual, expected)
        decoded = decode_sensor_frame(
            actual.payload,
            width=3,
            height=2,
            depth_scale=actual.depth_scale,
            depth_bias=actual.depth_bias,
        )
        self.assertEqual(decoded.packed_depth, (500, 1000, 0, 65535, 0, 0))
        self.assertEqual(decoded.foreground_mask, (True, True, False, True, False, False))
        self.assertAlmostEqual(decoded.confidence[0], 1.0)
        self.assertAlmostEqual(decoded.confidence[1], 128 / 255.0)
        self.assertAlmostEqual(decoded.depth_metres[3], 65.535)
        self.assertEqual(expected.stats.background_pixels, 1)
        self.assertEqual(expected.stats.invalid_depth_pixels, 1)
        self.assertEqual(expected.stats.confidence_rejected_pixels, 1)
        self.assertEqual(expected.stats.far_clipped_pixels, 1)

    def test_decoder_enforces_fail_closed_pixel_invariants(self) -> None:
        invalid_payloads = (
            bytes((0, 1, 0, 0)),
            bytes((0, 0, 255, 255)),
            bytes((0, 1, 255, 0)),
            bytes((0, 1, 127, 255)),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(
                DepthAnythingTransportError
            ):
                decode_sensor_frame(payload, width=1, height=1)

    def test_iterable_size_and_nonfinite_inputs_are_bounded(self) -> None:
        with self.assertRaisesRegex(DepthAnythingTransportError, "fewer"):
            pack_sensor_frame([1.0], [1.0], [1.0], width=2, height=1)
        with self.assertRaisesRegex(DepthAnythingTransportError, "more"):
            pack_sensor_frame([1.0, 2.0], [1.0, 1.0], [1.0, 1.0], width=1, height=1)
        with self.assertRaisesRegex(DepthAnythingTransportError, "finite"):
            pack_sensor_frame([1.0], [1.0], [1.0], width=1, height=1, depth_scale=math.inf)

    def test_metadata_contract_correlates_frame_capture_and_calibration(self) -> None:
        packed = pack_sensor_frame([1.25, 0.0], [1, 0], [0.75, 0], width=2, height=1)
        digest = "a" * 64
        metadata = make_sensor_worldbus_metadata(
            packed,
            frame_id=7,
            capture_timestamp_ns=1_234_567_890,
            intrinsics=(100.0, 100.0, 1.0, 0.5),
            camera_to_world=IDENTITY_4X4,
            generation_id="sensor-generation",
            producer_session_id="sensor-session",
            sensor_calibration_id="calibration-a",
            sensor_calibration_digest=digest,
            model_id="depth-anything/model",
            model_revision="b" * 40,
            calibration_mode="session_frozen",
            raw_order="near_is_larger",
            raw_percentiles=(2.0, 98.0),
            raw_bounds=(0.1, 1.2),
            pseudo_metre_slab=(0.5, 4.0),
            foreground_far_m=3.0,
            capture_source="webcam",
            inference_ms=12.5,
        )
        frame = make_frame(metadata, packed.payload)
        ext = frame.metadata.extensions
        self.assertEqual(frame.metadata.pixel_format, "rgba8")
        self.assertEqual(frame.metadata.frame_id, ext["sensor_frame_id"])
        self.assertEqual(str(frame.metadata.timestamp_ns), ext["sensor_capture_timestamp_ns"])
        self.assertEqual(ext["sensor_calibration_digest"], digest)
        self.assertEqual(ext["depth_anything_contract"], SENSOR_CONTRACT)
        self.assertEqual(ext["depth_anything_depth_encoding"], DEPTH_ENCODING)
        self.assertEqual(ext["depth_anything_depth_semantics"], DEPTH_SEMANTICS)
        self.assertEqual(ext["depth_anything_mask_semantics"], MASK_SEMANTICS)
        self.assertEqual(ext["depth_anything_confidence_semantics"], CONFIDENCE_SEMANTICS)
        self.assertIs(ext["depth_anything_contains_rgb"], False)
        self.assertEqual(frame.metadata.camera_to_world, IDENTITY_4X4)

        with self.assertRaisesRegex(DepthAnythingTransportError, "reserved"):
            make_sensor_worldbus_metadata(
                packed,
                frame_id=0,
                capture_timestamp_ns=1,
                intrinsics=(1, 1, 0.5, 0.5),
                camera_to_world=IDENTITY_4X4,
                generation_id="g",
                producer_session_id="s",
                sensor_calibration_id="c",
                sensor_calibration_digest=digest,
                model_id="m",
                model_revision="r",
                calibration_mode="fixed",
                raw_order="near_is_larger",
                raw_percentiles=(2, 98),
                raw_bounds=(0, 1),
                pseudo_metre_slab=(0.5, 4),
                foreground_far_m=3,
                capture_source="webcam",
                inference_ms=0,
                extra_extensions={"width": 999},
            )

        moved = list(IDENTITY_4X4)
        moved[3] = 1.0
        with self.assertRaisesRegex(DepthAnythingTransportError, "identity"):
            make_sensor_worldbus_metadata(
                packed,
                frame_id=0,
                capture_timestamp_ns=1,
                intrinsics=(1, 1, 0.5, 0.5),
                camera_to_world=moved,
                generation_id="g",
                producer_session_id="s",
                sensor_calibration_id="c",
                sensor_calibration_digest=digest,
                model_id="m",
                model_revision="r",
                calibration_mode="fixed",
                raw_order="near_is_larger",
                raw_percentiles=(2, 98),
                raw_bounds=(0, 1),
                pseudo_metre_slab=(0.5, 4),
                foreground_far_m=3,
                capture_source="webcam",
                inference_ms=0,
            )


if __name__ == "__main__":
    unittest.main()
