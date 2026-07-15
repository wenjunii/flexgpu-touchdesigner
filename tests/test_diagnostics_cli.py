from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flexgpu.cli import build_argument_parser, main  # noqa: E402
from flexgpu.config import validate_config  # noqa: E402
from flexgpu.diagnostics import diagnostic_summary, run_diagnostics  # noqa: E402
from flexgpu.models import GPUInfo, RuntimeControlError  # noqa: E402
from flexgpu.planner import build_process_plan  # noqa: E402
from flexgpu.runtime import (  # noqa: E402
    _environment_fingerprint,
    _inspect_process,
    _pid_alive,
    manifest_path,
    start_plan,
    stop_managed,
)


GPU = GPUInfo(
    0,
    "GPU-3080",
    "00000000:01:00.0",
    "NVIDIA GeForce RTX 3080 Ti Laptop GPU",
    16384,
    "555.1",
)


def usable_config(directory: str):
    return validate_config(
        {
            "topology": "single",
            "runtime_dir": "runtime",
            "processes": {
                "world": {
                    "command": [sys.executable, "-c", "print('world')"],
                    "touchdesigner": False,
                    "cwd": ".",
                }
            },
        },
        os.path.join(directory, "show.json"),
    )


class DiagnosticAndCliTests(unittest.TestCase):
    def test_exited_child_is_not_alive_while_process_handle_is_retained(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=5)
        # Keep the Popen object (and its Windows process handle) alive while
        # checking.  OpenProcess alone can otherwise misclassify this as active.
        self.assertFalse(_pid_alive(child.pid))

    def test_diagnostics_pass_for_available_dependency_free_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = usable_config(directory)
            plan = build_process_plan(config, [GPU])
            checks = run_diagnostics(config, [GPU], plan)
            self.assertEqual(diagnostic_summary(checks)["status"], "pass")

    def test_diagnostics_fail_for_missing_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = validate_config(
                {
                    "topology": "single",
                    "processes": {
                        "world": {
                            "executable": sys.executable,
                            "project": "missing.toe",
                            "touchdesigner": False,
                        }
                    },
                },
                os.path.join(directory, "show.json"),
            )
            checks = run_diagnostics(config, [GPU], build_process_plan(config, [GPU]))
            failures = [check.code for check in checks if check.level == "fail"]
            self.assertIn("process.world.project", failures)

    def test_start_and_stop_dry_runs_never_create_runtime_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = usable_config(directory)
            plan = build_process_plan(config, [GPU])
            start_result = start_plan(plan, config, execute=False)
            self.assertEqual(start_result["mode"], "dry-run")
            self.assertFalse(os.path.exists(manifest_path(config)))
            stop_result = stop_managed(config, execute=False)
            self.assertEqual(stop_result["targets"], [])
            self.assertFalse(os.path.exists(manifest_path(config)))

    def test_execute_start_reuses_matching_manifest_owned_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = usable_config(directory)
            plan = build_process_plan(config, [GPU])
            path = manifest_path(config)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            process = plan.processes[0]
            identity = _inspect_process(os.getpid())
            self.assertIsNotNone(identity)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": 2,
                        "started_at": "test",
                        "config": config.source_path,
                        "processes": [
                            {
                                "role": process.role,
                                "pid": os.getpid(),
                                "command": list(process.command),
                                "cwd": process.cwd,
                                "identity": identity,
                                "environment_sha256": _environment_fingerprint(process.env),
                            }
                        ],
                    },
                    handle,
                )
            result = start_plan(plan, config, execute=True)
            self.assertEqual(result["started"], [])
            self.assertEqual(result["reused"][0]["pid"], os.getpid())

    def test_execute_start_refuses_reuse_when_environment_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = usable_config(directory)
            plan = build_process_plan(config, [GPU])
            process = plan.processes[0]
            identity = _inspect_process(os.getpid())
            self.assertIsNotNone(identity)
            path = manifest_path(config)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": 2,
                        "started_at": "test",
                        "config": config.source_path,
                        "processes": [
                            {
                                "role": process.role,
                                "pid": os.getpid(),
                                "command": list(process.command),
                                "cwd": process.cwd,
                                "identity": identity,
                                "environment_sha256": _environment_fingerprint(
                                    {"FLEXGPU_EXPERIENCE": "different"}
                                ),
                            }
                        ],
                    },
                    handle,
                )
            with self.assertRaises(RuntimeControlError):
                start_plan(plan, config, execute=True)

    def test_stop_refuses_mismatched_pid_identity_without_signaling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = usable_config(directory)
            path = manifest_path(config)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            identity = _inspect_process(os.getpid())
            self.assertIsNotNone(identity)
            tampered = dict(identity or {})
            tampered["creation_token"] = "0"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": 2,
                        "started_at": "test",
                        "config": config.source_path,
                        "processes": [
                            {
                                "role": "world",
                                "pid": os.getpid(),
                                "command": [sys.executable],
                                "cwd": directory,
                                "identity": tampered,
                            }
                        ],
                    },
                    handle,
                )
            preview = stop_managed(config, execute=False)
            self.assertEqual(preview["targets"], [])
            self.assertEqual(preview["refused"][0]["pid"], os.getpid())
            with mock.patch("flexgpu.runtime.os.kill") as kill:
                with self.assertRaises(RuntimeControlError):
                    stop_managed(config, execute=True)
                kill.assert_not_called()

    def test_manifest_rejects_non_integer_pid_and_non_string_role(self) -> None:
        cases = ((True, "world"), (1.5, "world"), ("123", "world"), (123, None))
        for pid, role in cases:
            with self.subTest(pid=pid, role=role), tempfile.TemporaryDirectory() as directory:
                config = usable_config(directory)
                path = manifest_path(config)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "version": 2,
                            "processes": [{"role": role, "pid": pid, "identity": {}}],
                        },
                        handle,
                    )
                with self.assertRaises(RuntimeControlError):
                    stop_managed(config, execute=False)

    def test_stop_refuses_manifest_from_different_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = usable_config(directory)
            path = manifest_path(config)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": 2,
                        "config": os.path.join(directory, "another-show.json"),
                        "processes": [],
                    },
                    handle,
                )
            with self.assertRaises(RuntimeControlError):
                stop_managed(config, execute=False)

    def test_cli_contract_accepts_all_actions_and_overrides(self) -> None:
        parser = build_argument_parser()
        self.assertEqual(parser.parse_args(["discover"]).action, "discover")
        for action in ("validate", "plan", "diagnose", "start", "stop"):
            args = parser.parse_args([action, "--config", "show.json"])
            self.assertEqual(args.action, action)
        args = parser.parse_args(
            [
                "start",
                "--config",
                "show.json",
                "--experience",
                "combined",
                "--completion",
                "procedural",
                "--tier",
                "3080ti_16gb",
                "--execute",
            ]
        )
        self.assertTrue(args.execute)

    def test_cli_start_defaults_to_non_mutating_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "show.json")
            data = {
                "topology": "single",
                "runtime_dir": "runtime",
                "processes": {
                    "world": {
                        "command": [sys.executable, "-c", "print('world')"],
                        "touchdesigner": False,
                    }
                },
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            output = io.StringIO()
            with mock.patch("flexgpu.cli.discover_nvidia_gpus", return_value=[GPU]):
                with contextlib.redirect_stdout(output):
                    status = main(["start", "--config", path, "--json"])
            self.assertEqual(status, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "dry-run")
            self.assertFalse(os.path.exists(os.path.join(directory, "runtime")))

    def test_cli_start_dry_run_fails_when_preflight_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "show.json")
            data = {
                "topology": "single",
                "runtime_dir": "runtime",
                "processes": {
                    "world": {
                        "executable": sys.executable,
                        "project": "missing.toe",
                        "touchdesigner": False,
                    }
                },
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            output = io.StringIO()
            with mock.patch("flexgpu.cli.discover_nvidia_gpus", return_value=[GPU]):
                with contextlib.redirect_stdout(output):
                    status = main(["start", "--config", path, "--json"])
            self.assertEqual(status, 3)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "dry-run")
            self.assertEqual(payload["diagnostics"]["summary"]["status"], "fail")


if __name__ == "__main__":
    unittest.main()
