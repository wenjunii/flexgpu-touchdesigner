from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from flexgpu.moge2_transport import (  # noqa: E402
    ATLAS_CONTRACT,
    CONFIDENCE_SEMANTICS,
    MoGe2TransportError,
    decode_moge2_atlas,
    make_moge2_worldbus_metadata,
    pack_moge2_atlas,
    pack_moge2_atlas_numpy,
)
from flexgpu.worldbus import make_frame  # noqa: E402


IDENTITY = [
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
]


class MoGe2AtlasPackingTests(unittest.TestCase):
    def test_exact_source_and_big_endian_depth_round_trip(self) -> None:
        source = bytes((1, 2, 3, 4, 10, 20, 30, 40))
        atlas = pack_moge2_atlas(
            source,
            [1.234, 65.535],
            [1.0, True],
            source_width=2,
            height=1,
        )
        self.assertEqual(atlas.atlas_width, 4)
        self.assertEqual(
            atlas.payload,
            source + bytes((0x04, 0xD2, 255, 255, 0xFF, 0xFF, 255, 255)),
        )
        decoded = decode_moge2_atlas(
            atlas.payload,
            atlas_width=atlas.atlas_width,
            height=atlas.height,
            depth_scale=atlas.depth_scale,
            depth_bias=atlas.depth_bias,
        )
        self.assertEqual(decoded.source_rgba, source)
        self.assertEqual(decoded.packed_depth, (1234, 65535))
        self.assertEqual(decoded.depth_metres, (1.234, 65.535))
        self.assertEqual(decoded.mask, (True, True))
        self.assertEqual(decoded.confidence, (True, True))

    def test_planes_are_interleaved_by_row(self) -> None:
        first_row = bytes((1, 1, 1, 1, 2, 2, 2, 2))
        second_row = bytes((3, 3, 3, 3, 4, 4, 4, 4))
        atlas = pack_moge2_atlas(
            first_row + second_row,
            [1.0, 2.0, 3.0, 4.0],
            [1.0] * 4,
            source_width=2,
            height=2,
        )
        row_bytes = atlas.atlas_width * 4
        self.assertEqual(atlas.payload[: len(first_row)], first_row)
        self.assertEqual(atlas.payload[row_bytes : row_bytes + len(second_row)], second_row)
        decoded = decode_moge2_atlas(
            atlas.payload, atlas_width=4, height=2, depth_scale=0.001
        )
        self.assertEqual(decoded.source_rgba, first_row + second_row)
        self.assertEqual(decoded.packed_depth, (1000, 2000, 3000, 4000))

    def test_nonfinite_invalid_mask_and_out_of_range_depth_are_sanitized(self) -> None:
        atlas = pack_moge2_atlas(
            bytes(6 * 4),
            [math.nan, math.inf, -1.0, 0.0001, 100.0, 2.0],
            [1.0, 1.0, 1.0, 1.0, 1.0, math.nan],
            source_width=6,
            height=1,
        )
        decoded = decode_moge2_atlas(
            atlas.payload, atlas_width=12, height=1, depth_scale=0.001
        )
        self.assertEqual(decoded.packed_depth, (0, 0, 0, 1, 65535, 0))
        self.assertEqual(decoded.mask, (False, False, False, True, True, False))
        self.assertEqual(decoded.confidence, decoded.mask)
        self.assertEqual(atlas.stats.valid_pixels, 2)
        self.assertEqual(atlas.stats.invalid_depth_pixels, 3)
        self.assertEqual(atlas.stats.masked_pixels, 1)
        self.assertEqual(atlas.stats.near_clipped_pixels, 1)
        self.assertEqual(atlas.stats.far_clipped_pixels, 1)
        self.assertAlmostEqual(atlas.stats.valid_fraction, 2.0 / 6.0)

    def test_input_lengths_calibration_and_strict_decode_fail_closed(self) -> None:
        cases = [
            dict(depth=[1.0], mask=[1.0, 1.0]),
            dict(depth=[1.0, 2.0, 3.0], mask=[1.0, 1.0]),
            dict(depth=[1.0, 2.0], mask=[1.0]),
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(MoGe2TransportError):
                pack_moge2_atlas(
                    bytes(8),
                    values["depth"],
                    values["mask"],
                    source_width=2,
                    height=1,
                )
        with self.assertRaisesRegex(MoGe2TransportError, "depth_scale"):
            pack_moge2_atlas(
                bytes(4), [1.0], [1.0], source_width=1, height=1, depth_scale=0.0
            )
        with self.assertRaisesRegex(MoGe2TransportError, "range must remain finite"):
            pack_moge2_atlas(
                bytes(4),
                [1.0],
                [1.0],
                source_width=1,
                height=1,
                depth_scale=1e308,
            )
        valid = pack_moge2_atlas(
            bytes(4), [1.0], [1.0], source_width=1, height=1
        )
        corrupted = bytearray(valid.payload)
        corrupted[-1] = 128
        with self.assertRaisesRegex(MoGe2TransportError, "confidence plane"):
            decode_moge2_atlas(corrupted, atlas_width=2, height=1)

    def test_numpy_encoder_is_byte_exact_with_reference_encoder(self) -> None:
        try:
            import numpy as np
        except ImportError:
            self.skipTest("optional NumPy accelerator is not installed")
        source = bytes(range(48))
        depth = np.asarray(
            [
                1.234, math.nan, 0.0001, 100.0,
                2.0, -1.0, 65.535, math.inf,
                0.5, 1.5, 2.5, 3.5,
            ],
            dtype=np.float32,
        ).reshape(3, 4)
        mask = np.asarray(
            [
                1.0, 1.0, 1.0, 1.0,
                0.0, 1.0, 1.0, 1.0,
                math.nan, 0.5, 0.5001, 1.0,
            ],
            dtype=np.float32,
        ).reshape(3, 4)
        reference = pack_moge2_atlas(
            source,
            depth.reshape(-1),
            mask.reshape(-1),
            source_width=4,
            height=3,
        )
        accelerated = pack_moge2_atlas_numpy(
            source,
            depth,
            mask,
            source_width=4,
            height=3,
        )
        self.assertEqual(accelerated.payload, reference.payload)
        self.assertEqual(accelerated.stats, reference.stats)
        self.assertEqual(accelerated.depth_scale, reference.depth_scale)
        self.assertEqual(accelerated.depth_bias, reference.depth_bias)


class MoGe2AtlasMetadataTests(unittest.TestCase):
    def test_metadata_is_worldbus_valid_and_keeps_source_plane_calibration(self) -> None:
        atlas = pack_moge2_atlas(
            bytes(2 * 2 * 4),
            [1.0, 2.0, 3.0, math.nan],
            [1.0] * 4,
            source_width=2,
            height=2,
        )
        metadata = make_moge2_worldbus_metadata(
            atlas,
            frame_id=9,
            timestamp_ns=200,
            intrinsics=[2.0, 2.0, 1.0, 1.0],
            camera_to_world=IDENTITY,
            generation_id="prompt-7",
            producer_session_id="moge-worker-a",
            source_frame_id=42,
            source_timestamp_ns=100,
            source_producer_session_id="streamdiffusion-a",
            model_id="Ruicheng/moge-2-vits-normal",
            model_source_revision="0" * 40,
            model_revision="1" * 40,
            extra_extensions={"moge2_num_tokens": 1200},
        )
        frame = make_frame(metadata, atlas.payload)
        self.assertEqual(frame.metadata.width, 4)
        self.assertEqual(frame.metadata.height, 2)
        self.assertEqual(frame.metadata.intrinsics, (2.0, 2.0, 1.0, 1.0))
        self.assertEqual(frame.metadata.depth_scale_bias, (0.001, 0.0))
        self.assertEqual(frame.metadata.extensions["moge2_atlas_contract"], ATLAS_CONTRACT)
        self.assertEqual(
            frame.metadata.extensions["moge2_confidence_semantics"],
            CONFIDENCE_SEMANTICS,
        )
        self.assertEqual(frame.metadata.extensions["moge2_source_frame_id"], 42)
        self.assertEqual(frame.metadata.extensions["moge2_source_timestamp_ns"], "100")
        self.assertEqual(frame.metadata.extensions["moge2_valid_pixels"], 3)
        self.assertEqual(frame.metadata.extensions["moge2_num_tokens"], 1200)

    def test_metadata_rejects_reserved_extension_overrides(self) -> None:
        atlas = pack_moge2_atlas(
            bytes(4), [1.0], [1.0], source_width=1, height=1
        )
        with self.assertRaisesRegex(MoGe2TransportError, "reserved"):
            make_moge2_worldbus_metadata(
                atlas,
                frame_id=1,
                timestamp_ns=2,
                intrinsics=[1.0, 1.0, 0.5, 0.5],
                camera_to_world=IDENTITY,
                generation_id="prompt",
                producer_session_id="worker",
                source_frame_id=3,
                source_timestamp_ns=1,
                model_id="model",
                model_source_revision="source-revision",
                model_revision="model-revision",
                extra_extensions={"width": 100},
            )


if __name__ == "__main__":
    unittest.main()
