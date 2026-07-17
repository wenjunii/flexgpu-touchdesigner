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


def loopback_transport() -> dict[str, object]:
    return {
        "type": "touch_tcp",
        "peer_host": "127.0.0.1",
        "atlas_width": 1024,
        "atlas_height": 512,
        "atlas_fps": 10,
        "atlas_port": 12000,
    }


def network_transport() -> dict[str, object]:
    return {
        "type": "touch_tcp",
        "peer_host": "192.0.2.20",
        "atlas_width": 1024,
        "atlas_height": 512,
        "atlas_fps": 5,
        "atlas_port": 12000,
        "control_port": 12001,
        "heartbeat_port": 12002,
        "heartbeat_timeout_ms": 2000,
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
            self.assertEqual(
                Path(process.env["FLEXGPU_ROOT"]),
                Path(__file__).resolve().parents[1],
            )
            self.assertEqual(
                Path(process.env["FLEXGPU_SRC"]),
                Path(__file__).resolve().parents[1] / "src",
            )
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
                "transport": loopback_transport(),
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
        self.assertEqual(by_role["world"].env["FLEXGPU_TIER"], "4090")
        self.assertEqual(by_role["world"].env["FLEXGPU_MAX_POINTS"], "250000")

    def test_dual_local_auto_uses_safe_quality_for_each_heterogeneous_role(self) -> None:
        config = validate_config(
            {
                "topology": "dual_local",
                "tier": "auto",
                "gpu": {"ai": {"uuid": "GPU-5090"}, "render": {"index": 0}},
                "transport": loopback_transport(),
                "processes": {
                    "ai": {
                        "command": [sys.executable, "-c", "print('{tier}')"],
                        "touchdesigner": False,
                    },
                    "world": {
                        "command": [sys.executable, "-c", "print('{tier}')"],
                        "touchdesigner": False,
                    },
                },
            }
        )
        plan = build_process_plan(config, [GPU_3080, GPU_5090])
        by_role = {process.role: process for process in plan.processes}

        self.assertEqual(plan.tier, "5090")
        self.assertEqual(by_role["ai"].env["FLEXGPU_TIER"], "5090")
        self.assertEqual(by_role["ai"].env["FLEXGPU_MAX_POINTS"], "262144")
        self.assertIn("5090", by_role["ai"].command[-1])
        self.assertEqual(by_role["world"].env["FLEXGPU_TIER"], "3080ti_16gb")
        self.assertEqual(by_role["world"].env["FLEXGPU_MAX_POINTS"], "120000")
        self.assertEqual(by_role["world"].env["FLEXGPU_GEOMETRY_RESOLUTION"], "384")
        self.assertIn("3080ti_16gb", by_role["world"].command[-1])

    def test_dual_local_honors_explicit_zero_index_selector(self) -> None:
        config = validate_config(
            {
                "topology": "dual_local",
                "gpu": {"render": {"index": 0}, "ai": {"uuid": "GPU-4090"}},
                "transport": loopback_transport(),
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
                "transport": network_transport(),
                "processes": {"ai_worker": python_process("ai")},
            }
        )
        render_config = validate_config(
            {
                "topology": "dual_network",
                "node_role": "renderer",
                "transport": network_transport(),
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

    def test_process_defaults_and_role_definitions_reject_unknown_fields(self) -> None:
        cases = (
            (
                "processes.defaults",
                {
                    "defaults": {"gpu_affinty": False},
                    "world": python_process("world"),
                },
            ),
            (
                "processes.world",
                {
                    "world": {
                        **python_process("world"),
                        "gpu_affinty": False,
                    }
                },
            ),
        )
        for path, processes in cases:
            with self.subTest(path=path), self.assertRaises(ConfigError) as raised:
                validate_config({"topology": "single", "processes": processes})
            self.assertIn(
                "%s has unsupported field 'gpu_affinty'" % path,
                str(raised.exception),
            )

    def test_explicit_custom_tier_matches_public_schema_contract(self) -> None:
        config = validate_config(
            {
                "topology": "single",
                "tier": "custom",
                "processes": {"world": python_process("world")},
            }
        )
        plan = build_process_plan(config, [GPU_3080])
        self.assertEqual(plan.tier, "custom")
        self.assertEqual(plan.processes[0].env["FLEXGPU_TIER"], "custom")
        self.assertTrue(any("does not match a tuned" in warning for warning in plan.warnings))

    def test_process_env_cannot_override_launcher_gpu_or_role_identity(self) -> None:
        for key in ("CUDA_VISIBLE_DEVICES", "cuda_device_order", "FLEXGPU_ROLE", "FlexGpu_Gpu_Uuid"):
            with self.subTest(key=key):
                config = validate_config(
                    {
                        "topology": "single",
                        "processes": {
                            "world": {
                                "command": [sys.executable, "-c", "pass"],
                                "touchdesigner": False,
                                "env": {key: "attacker-controlled"},
                            }
                        },
                    }
                )
                with self.assertRaisesRegex(PlanError, "launcher-reserved"):
                    build_process_plan(config, [GPU_3080])

    def test_touchdesigner_gpu_bus_selector_is_case_normalized_and_verified(self) -> None:
        config = validate_config(
            {
                "topology": "single",
                "processes": {
                    "world": {
                        "command": ["TouchDesigner.exe", "-GPUBUSID", "00:01:00:00"],
                        "touchdesigner": True,
                    }
                },
            }
        )
        command = build_process_plan(config, [GPU_3080]).processes[0].command
        self.assertEqual(command[1:3], ("-gpubusid", "0:1:0:0"))

    def test_touchdesigner_rejects_duplicate_or_wrong_gpu_selectors(self) -> None:
        commands = (
            ["TouchDesigner.exe", "-gpubusid", "0:1:0:0", "-GPUBUSID", "0:1:0:0"],
            ["TouchDesigner.exe", "-gpubusid", "0:2:0:0"],
            ["TouchDesigner.exe", "-gpuformonitor", "0"],
        )
        for command in commands:
            with self.subTest(command=command):
                config = validate_config(
                    {
                        "topology": "single",
                        "processes": {
                            "world": {"command": command, "touchdesigner": True}
                        },
                    }
                )
                with self.assertRaises(PlanError):
                    build_process_plan(config, [GPU_3080])

    def test_public_plan_redacts_secret_env_and_argv_but_internal_spec_keeps_them(self) -> None:
        sentinel = "FLEXGPU-SECRET-SENTINEL"
        argv_only = "ARGV-ONLY-SENTINEL"
        license_sentinel = "PAID-LICENSE-SENTINEL"
        uri_password = "URI-PASSWORD-SENTINEL"
        query_secret = "QUERY-LICENSE-SENTINEL"
        service_auth_env_name = "SERVICE_" + "TOKEN"
        paid_entitlement_env_name = "LICENSE_" + "KEY"
        credentialed_endpoint = (
            "https://user" + ":%s@example.invalid/hook?license_token=%s"
        ) % (uri_password, query_secret)
        config = validate_config(
            {
                "topology": "single",
                "processes": {
                    "world": {
                        "command": [
                            sys.executable,
                            "--api-key",
                            sentinel,
                            "https://example.invalid/?token=" + argv_only,
                        ],
                        "touchdesigner": False,
                        "env": {
                            service_auth_env_name: sentinel,
                            paid_entitlement_env_name: license_sentinel,
                            "PUBLIC_COPY": license_sentinel,
                            "SERVICE_ENDPOINT": credentialed_endpoint,
                        },
                    }
                },
            }
        )
        plan = build_process_plan(config, [GPU_3080])
        process = plan.processes[0]
        self.assertIn(sentinel, process.command)
        self.assertEqual(process.env[service_auth_env_name], sentinel)
        self.assertEqual(process.env[paid_entitlement_env_name], license_sentinel)
        public_plan = plan.to_dict()
        public_env = public_plan["processes"][0]["env"]
        self.assertEqual(public_env[paid_entitlement_env_name], "<redacted>")
        self.assertEqual(public_env["PUBLIC_COPY"], "<redacted>")
        self.assertEqual(
            public_env["SERVICE_ENDPOINT"],
            "https://user:<redacted>@example.invalid/hook?license_token=<redacted>",
        )
        public = str(public_plan)
        self.assertNotIn(sentinel, public)
        self.assertNotIn(argv_only, public)
        self.assertNotIn(license_sentinel, public)
        self.assertNotIn(uri_password, public)
        self.assertNotIn(query_secret, public)
        self.assertIn("<redacted>", public)


if __name__ == "__main__":
    unittest.main()
