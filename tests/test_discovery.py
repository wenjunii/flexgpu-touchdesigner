from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flexgpu.discovery import (  # noqa: E402
    parse_nvidia_smi_csv,
    resolve_gpu_selector,
    touchdesigner_bus_id,
)
from flexgpu.models import DiscoveryError, GPUSelector  # noqa: E402
from flexgpu.presets import classify_gpu  # noqa: E402


SAMPLE = """\
0, GPU-LAPTOP, 00000000:01:00.0, NVIDIA GeForce RTX 3080 Ti Laptop GPU, 16384, 555.99
1, GPU-4090, 00000000:02:00.0, NVIDIA GeForce RTX 4090, 24564, 555.99
2, GPU-5090, 00000000:B5:00.0, NVIDIA GeForce RTX 5090, 32607, 555.99
"""


class DiscoveryTests(unittest.TestCase):
    def test_parse_and_classify_supported_gpus(self) -> None:
        gpus = parse_nvidia_smi_csv(SAMPLE)
        self.assertEqual([gpu.index for gpu in gpus], [0, 1, 2])
        self.assertEqual(
            [classify_gpu(gpu) for gpu in gpus],
            ["3080ti_16gb", "4090", "5090"],
        )
        self.assertEqual(gpus[0].memory_total_mib, 16384)

    def test_header_units_and_quoted_name_are_tolerated(self) -> None:
        text = (
            'index, uuid, pci.bus_id, name, memory.total, driver_version\n'
            '0, GPU-X, 00000000:01:00.0, "NVIDIA, Test GPU", 16384 MiB, 1.2\n'
        )
        gpu = parse_nvidia_smi_csv(text)[0]
        self.assertEqual(gpu.name, "NVIDIA, Test GPU")
        self.assertEqual(gpu.memory_total_mib, 16384)

    def test_malformed_csv_is_rejected(self) -> None:
        with self.assertRaises(DiscoveryError):
            parse_nvidia_smi_csv("0, GPU-X, too-short\n")

    def test_touchdesigner_bus_id_converts_hex_to_numeric_fields(self) -> None:
        self.assertEqual(touchdesigner_bus_id("00000000:B5:00.0"), "0:181:0:0")
        self.assertEqual(touchdesigner_bus_id("0:181:0:0"), "0:181:0:0")

    def test_selectors_resolve_by_index_uuid_and_bus(self) -> None:
        gpus = parse_nvidia_smi_csv(SAMPLE)
        self.assertEqual(resolve_gpu_selector(GPUSelector("index", 0), gpus).uuid, "GPU-LAPTOP")
        self.assertEqual(resolve_gpu_selector(GPUSelector("uuid", "gpu-4090"), gpus).index, 1)
        self.assertEqual(resolve_gpu_selector(GPUSelector("bus_id", "0:181:0:0"), gpus).index, 2)
        self.assertEqual(resolve_gpu_selector(GPUSelector(), gpus).index, 2)


if __name__ == "__main__":
    unittest.main()
