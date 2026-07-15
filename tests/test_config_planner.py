from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flexgpu.config import validate_config  # noqa: E402
from flexgpu.models import ConfigError, GPUInfo, PlanError  # noqa: E402
from flexgpu.planner import build_process_plan  # noqa: E402


GPU_3080 = GPUInfo(
    0,
    "GPU-3080",
    "00000000:01:00.0",
    "NVIDIA GeForce RTX 3080 Ti Laptop GPU",
    16384,
    "555.1",
)
GPU_4090 = GPUInfo(
    1,
    "GPU-4090",
    "00000000:02:00.0",
    "NVIDIA GeForce RTX 4090",
    24564,
    "555.1",
)
GPU_5090 = GPUInfo(
    2,
    "GPU-5090",
    "00000000:03:00.0",
    "NVIDIA GeForce RTX 5090",
    32607,
    "555.1",
)


def python_process(role: str) -> dict[str, object]:
    return {
        "command": [sys.executable, "-c", "print(%r)" % role],
        "touchdesigner": False,
    }


class ConfigPlannerTests(unittest.TestCase):
    def test_single_is_one_unified_world_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "single.json")
            config = validate_config(
                {
                    "topology": "single_gpu",
                    "experience": "both",
                    "completion": "thickness_fog",
                    "gpu": {"render": {"index": 0}},
                    "processes": {
                        "world": {
                            "executable": "TouchDesigner.exe",
                            "project": "projects/FlexShow.toe",
                            "touchdesigner": True,
                        }
                    },
                },
                source,
            )
            plan = build_process_plan(config, [GPU_3080])
            self.assertEqual([process.role for process in plan.processes], ["world"])
            self.assertEqual(plan.tier, "3080ti_16gb")
            process = plan.processes[0]
            self.assertEqual(process.command[:3], ("TouchDesigner.exe", "-gpubusid", "0:1:0:0"))
            self.assertEqual(
                process.project_path,
                os.path.join(directory, "projects", "FlexShow.toe"),
            )
            self.assertNotIn("-config", process.command)
            self.assertNotIn("-role", process.command)
            self.assertEqual(process.env["FLEXGPU_ROLE"], "world")
            self.assertEqual(process.env["FLEXGPU_EXPERIENCE"], "combined")
            self.assertEqual(process.env["FLEXGPU_COMPLETION"], "fog")
            self.assertEqual(process.env["CUDA_VISIBLE_DEVICES"], "GPU-3080")
            self.assertEqual(process.env["FLEXGPU_DIFFUSION_HZ"], "10")
            self.assertEqual(process.env["FLEXGPU_GEOMETRY_RESOLUTION"], "384")
            self.assertEqual(process.env["FLEXGPU_MAX_POINTS"], "120000")
            self.assertEqual(process.env["FLEXGPU_VR_REFRESH_HZ"], "72")

    def test_dual_local_auto_assigns_largest_gpu_to_ai(self) -> None:
        config = validate_config(
            {
                "topology": "dual_same_pc",
                "experience": "vr",
                "processes": {
                    "ai": python_process("ai"),
                    "world": python_process("world"),
                },
            }
        )
        plan = build_process_plan(config, [GPU_4090, GPU_5090])
        self.assertEqual([process.role for process in plan.processes], ["world", "ai"])
        by_role = {process.role: process for process in plan.processes}
        self.assertEqual(by_role["ai"].gpu.uuid, "GPU-5090")
        self.assertEqual(by_role["world"].gpu.uuid, "GPU-4090")
        self.assertEqual(by_role["ai"].dependencies, ("world",))
        self.assertEqual(plan.tier, "5090")
        self.assertEqual(by_role["ai"].env["FLEXGPU_DIFFUSION_HZ"], "20")
        self.assertEqual(by_role["world"].env["FLEXGPU_MAX_POINTS"], "400000")

    def test_dual_local_honors_explicit_zero_index_selector(self) -> None:
        config = validate_config(
            {
                "topology": "dual_local",
                "gpu": {"render": {"index": 0}, "ai": {"uuid": "GPU-4090"}},
                "processes": {
                    "ai": python_process("ai"),
                    "world": python_process("world"),
                },
            }
        )
        plan = build_process_plan(config, [GPU_3080, GPU_4090])
        by_role = {process.role: process for process in plan.processes}
        self.assertEqual(by_role["world"].gpu.index, 0)
        self.assertEqual(by_role["ai"].gpu.index, 1)

    def test_dual_network_ai_and_render_nodes_need_only_local_role(self) -> None:
        ai_config = validate_config(
            {
                "topology": "network",
                "node_role": "generator",
                "processes": {"ai_worker": python_process("ai")},
            }
        )
        render_config = validate_config(
            {
                "topology": "dual_network",
                "node_role": "renderer",
                "processes": {"renderer": python_process("world")},
            }
        )
        self.assertEqual(
            [process.role for process in build_process_plan(ai_config, [GPU_3080]).processes],
            ["ai"],
        )
        self.assertEqual(
            [process.role for process in build_process_plan(render_config, [GPU_4090]).processes],
            ["world"],
        )

    def test_single_rejects_two_different_explicit_gpu_selectors(self) -> None:
        config = validate_config(
            {
                "topology": "single",
                "gpu": {"ai": 0, "render": 1},
                "processes": {"world": python_process("world")},
            }
        )
        with self.assertRaises(PlanError):
            build_process_plan(config, [GPU_3080, GPU_4090])

    def test_validation_aggregates_missing_role_and_bad_completion(self) -> None:
        with self.assertRaises(ConfigError) as raised:
            validate_config({"topology": "dual_local", "completion": "magic"})
        message = str(raised.exception)
        self.assertIn("completion", message)
        self.assertIn("processes.world", message)
        self.assertIn("processes.ai", message)


if __name__ == "__main__":
    unittest.main()
