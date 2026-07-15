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
from flexgpu.models import GPUInfo  # noqa: E402
from flexgpu.planner import build_process_plan  # noqa: E402
from flexgpu.runtime import _pid_alive, manifest_path, start_plan, stop_managed  # noqa: E402


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
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": 1,
                        "started_at": "test",
                        "config": config.source_path,
                        "processes": [
                            {
                                "role": process.role,
                                "pid": os.getpid(),
                                "command": list(process.command),
                                "cwd": process.cwd,
                            }
                        ],
                    },
                    handle,
                )
            result = start_plan(plan, config, execute=True)
            self.assertEqual(result["started"], [])
            self.assertEqual(result["reused"][0]["pid"], os.getpid())

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


if __name__ == "__main__":
    unittest.main()
