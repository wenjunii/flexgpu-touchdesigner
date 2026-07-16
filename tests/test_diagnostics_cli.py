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
    MANIFEST_VERSION,
    MAX_RECOVERY_ATTEMPTS,
    _RuntimeMutationLock,
    _config_identity,
    _effective_environment_fingerprint,
    _environment_fingerprint,
    _inspect_process,
    _pid_alive,
    _stop_verified_records,
    lock_path,
    manifest_path,
    recover_managed,
    runtime_status,
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


def network_ai_config(directory: str, command: list[str] | None = None):
    return validate_config(
        {
            "topology": "dual_network",
            "node_role": "ai",
            "runtime_dir": "runtime-ai",
            "gpu": {"ai": "auto", "render": "auto"},
            "processes": {
                "ai": {
                    "command": command
                    or [sys.executable, "-c", "import time; time.sleep(60)"],
                    "touchdesigner": False,
                    "cwd": ".",
                }
            },
            "transport": {
                "type": "touch_tcp",
                "bind_host": "127.0.0.1",
                "peer_host": "127.0.0.1",
                "atlas_width": 1024,
                "atlas_height": 512,
                "atlas_fps": 5,
                "atlas_port": 12000,
                "control_port": 12001,
                "heartbeat_port": 12002,
                "heartbeat_timeout_ms": 2000,
            },
        },
        os.path.join(directory, "ai-show.json"),
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

    def test_status_is_read_only_when_runtime_does_not_exist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = usable_config(directory)
            runtime_dir = os.path.dirname(manifest_path(config))
            result = runtime_status(config)
            self.assertEqual(result["state"], "stopped")
            self.assertEqual(result["summary"], {"running": 0, "dead": 0, "refused": 0})
            self.assertFalse(os.path.exists(runtime_dir))

    def test_execute_start_refuses_live_exclusive_mutation_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = usable_config(directory)
            plan = build_process_plan(config, [GPU])
            path = lock_path(config)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {"version": 1, "pid": os.getpid(), "lock_nonce": "other"}, handle
                )
            with self.assertRaisesRegex(RuntimeControlError, "mutation is active"):
                start_plan(plan, config, execute=True)
            self.assertFalse(os.path.exists(manifest_path(config)))

    def test_mutation_lock_is_removed_after_context_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = usable_config(directory)
            with _RuntimeMutationLock(config):
                self.assertTrue(os.path.isfile(lock_path(config)))
            self.assertFalse(os.path.exists(lock_path(config)))

    def test_dead_mutation_lock_owner_is_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = usable_config(directory)
            path = lock_path(config)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": 1,
                        "pid": 2_000_000_000,
                        "lock_nonce": "stale",
                    },
                    handle,
                )
            with _RuntimeMutationLock(config):
                record = json.loads(Path(path).read_text(encoding="utf-8"))
                self.assertNotEqual(record["lock_nonce"], "stale")
                self.assertEqual(record["pid"], os.getpid())
            self.assertFalse(os.path.exists(path))

    def test_execute_start_reuses_matching_current_manifest_owned_process(self) -> None:
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
                        "version": MANIFEST_VERSION,
                        "started_at": "test",
                        "config": config.source_path,
                        "config_sha256": _config_identity(config),
                        "processes": [
                            {
                                "role": process.role,
                                "pid": os.getpid(),
                                "command": list(process.command),
                                "cwd": process.cwd,
                                "identity": identity,
                                "environment_sha256": _effective_environment_fingerprint(
                                    process
                                ),
                            }
                        ],
                    },
                    handle,
                )
            result = start_plan(plan, config, execute=True)
            self.assertEqual(result["started"], [])
            self.assertEqual(result["reused"][0]["pid"], os.getpid())
            upgraded = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(upgraded["version"], MANIFEST_VERSION)
            self.assertEqual(upgraded["state"], "running")
            self.assertTrue(upgraded["session_id"])

    def test_execute_start_refuses_read_only_legacy_manifest(self) -> None:
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
                        "version": 3,
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
                                    process.env
                                ),
                            }
                        ],
                    },
                    handle,
                )
            with self.assertRaisesRegex(
                RuntimeControlError, "legacy runtime manifest version 3 is read-only"
            ):
                start_plan(plan, config, execute=True)

    def test_execute_start_writes_incremental_session_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = validate_config(
                {
                    "topology": "single",
                    "runtime_dir": "runtime",
                    "processes": {
                        "world": {
                            "command": [
                                sys.executable,
                                "-c",
                                "import time; time.sleep(60)",
                            ],
                            "touchdesigner": False,
                            "cwd": ".",
                        }
                    },
                },
                os.path.join(directory, "show.json"),
            )
            plan = build_process_plan(config, [GPU])
            states: list[str] = []
            from flexgpu import runtime as runtime_module

            original_write = runtime_module._atomic_manifest_write

            def capture(path, data):
                states.append(str(data.get("state")))
                return original_write(path, data)

            started = False
            try:
                with mock.patch("flexgpu.runtime._atomic_manifest_write", side_effect=capture):
                    result = start_plan(plan, config, execute=True)
                started = True
                self.assertTrue(result["session_id"])
                manifest = json.loads(Path(manifest_path(config)).read_text(encoding="utf-8"))
                self.assertEqual(manifest["version"], MANIFEST_VERSION)
                self.assertEqual(manifest["state"], "running")
                self.assertIn("starting", states)
                self.assertEqual(states[-1], "running")
                self.assertEqual(runtime_status(config)["state"], "running")
            finally:
                if started:
                    with mock.patch("flexgpu.runtime.GRACEFUL_STOP_SECONDS", 0.0):
                        stop_managed(config, execute=True)

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
                        "version": MANIFEST_VERSION,
                        "started_at": "test",
                        "config": config.source_path,
                        "config_sha256": _config_identity(config),
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

    def test_execute_start_refuses_reuse_when_working_directory_changed(self) -> None:
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
                        "version": MANIFEST_VERSION,
                        "session_id": "test-session",
                        "started_at": "test",
                        "config": config.source_path,
                        "config_sha256": _config_identity(config),
                        "processes": [
                            {
                                "role": process.role,
                                "pid": os.getpid(),
                                "command": list(process.command),
                                "cwd": os.path.join(directory, "different"),
                                "identity": identity,
                                "environment_sha256": _effective_environment_fingerprint(
                                    process
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
                        "version": MANIFEST_VERSION,
                        "started_at": "test",
                        "config": config.source_path,
                        "config_sha256": _config_identity(config),
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

    def test_windows_stop_requests_graceful_close_before_forcing(self) -> None:
        record = {
            "role": "ai",
            "pid": 1234,
            "identity": {
                "creation_token": "1",
                "executable": "python.exe",
                "command_line_sha256": "abc",
            },
        }
        target = {"role": "ai", "pid": 1234}
        events: list[str] = []
        waits = 0

        def wait_records(retained, _seconds):
            nonlocal waits
            waits += 1
            return list(retained) if waits == 1 else []

        with (
            mock.patch("flexgpu.runtime.os.name", "nt"),
            mock.patch("flexgpu.runtime._open_windows_process", return_value="handle"),
            mock.patch(
                "flexgpu.runtime._inspect_windows_process_handle",
                return_value=dict(record["identity"]),
            ),
            mock.patch("flexgpu.runtime._compare_recorded_identity", return_value=("match", "")),
            mock.patch(
                "flexgpu.runtime._request_windows_graceful_shutdown",
                side_effect=lambda _pid: events.append("graceful") or True,
            ),
            mock.patch(
                "flexgpu.runtime._terminate_windows_process",
                side_effect=lambda _handle: events.append("force") or True,
            ),
            mock.patch("flexgpu.runtime._wait_windows_records", side_effect=wait_records),
            mock.patch("flexgpu.runtime._close_windows_process"),
        ):
            result = _stop_verified_records([record], [target])
        self.assertEqual(events, ["graceful", "force"])
        self.assertEqual(result["forced"], [target])
        self.assertEqual(result["survivors"], [])

    def test_recovery_is_ai_only_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            single = usable_config(directory)
            single_plan = build_process_plan(single, [GPU])
            with self.assertRaisesRegex(RuntimeControlError, "no separate ai process"):
                recover_managed(single_plan, single, execute=False)
            ai_config = network_ai_config(directory)
            ai_plan = build_process_plan(ai_config, [GPU])
            with self.assertRaisesRegex(RuntimeControlError, "between 1 and"):
                recover_managed(
                    ai_plan,
                    ai_config,
                    attempts=MAX_RECOVERY_ATTEMPTS + 1,
                    execute=False,
                )
            with self.assertRaisesRegex(RuntimeControlError, "only the ai role"):
                recover_managed(ai_plan, ai_config, role="world", execute=False)
            self.assertFalse(os.path.exists(os.path.dirname(manifest_path(ai_config))))

    def test_ai_recovery_reuses_healthy_role_without_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = network_ai_config(directory)
            plan = build_process_plan(config, [GPU])
            started = False
            try:
                first = recover_managed(plan, config, execute=True)
                started = True
                first_pid = first["started"][0]["pid"]
                second = recover_managed(plan, config, execute=True)
                self.assertEqual(second["action"], "reuse")
                self.assertEqual(second["attempts_used"], 0)
                self.assertEqual(second["reused"][0]["pid"], first_pid)
                self.assertEqual(runtime_status(config)["summary"]["running"], 1)
            finally:
                if started:
                    with mock.patch("flexgpu.runtime.GRACEFUL_STOP_SECONDS", 0.0):
                        stop_managed(config, execute=True)

    def test_ai_restart_replaces_only_the_ai_pid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = network_ai_config(directory)
            plan = build_process_plan(config, [GPU])
            started = False
            try:
                first = recover_managed(plan, config, execute=True)
                started = True
                first_pid = first["started"][0]["pid"]
                with mock.patch("flexgpu.runtime.GRACEFUL_STOP_SECONDS", 0.0):
                    second = recover_managed(
                        plan,
                        config,
                        execute=True,
                        restart_running=True,
                    )
                second_pid = second["started"][0]["pid"]
                self.assertNotEqual(first_pid, second_pid)
                self.assertEqual(second["role"], "ai")
                self.assertEqual(second["stopped"]["stopped"][0]["pid"], first_pid)
                status = runtime_status(config)
                self.assertEqual([item["role"] for item in status["processes"]], ["ai"])
                self.assertEqual(status["processes"][0]["pid"], second_pid)
            finally:
                if started:
                    with mock.patch("flexgpu.runtime.GRACEFUL_STOP_SECONDS", 0.0):
                        stop_managed(config, execute=True)

    def test_recovery_attempts_are_bounded_and_leave_degraded_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = network_ai_config(directory)
            plan = build_process_plan(config, [GPU])
            with mock.patch(
                "flexgpu.runtime._launch_process",
                side_effect=RuntimeControlError("synthetic launch failure"),
            ) as launch:
                with self.assertRaisesRegex(RuntimeControlError, "exhausted 2 attempts"):
                    recover_managed(plan, config, attempts=2, execute=True)
            self.assertEqual(launch.call_count, 2)
            manifest = json.loads(Path(manifest_path(config)).read_text(encoding="utf-8"))
            self.assertEqual(manifest["state"], "degraded")
            self.assertEqual(manifest["recovery"]["phase"], "failed")
            self.assertEqual(manifest["processes"], [])

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
                            "version": MANIFEST_VERSION,
                            "config": config.source_path,
                            "config_sha256": _config_identity(config),
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
                        "version": MANIFEST_VERSION,
                        "config": os.path.join(directory, "another-show.json"),
                        "config_sha256": _config_identity(config),
                        "processes": [],
                    },
                    handle,
                )
            with self.assertRaises(RuntimeControlError):
                stop_managed(config, execute=False)

    def test_cli_contract_accepts_all_actions_and_overrides(self) -> None:
        parser = build_argument_parser()
        self.assertEqual(parser.parse_args(["discover"]).action, "discover")
        for action in ("validate", "plan", "diagnose", "start", "stop", "status", "recover"):
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
                "custom",
                "--execute",
                "--wait-ready-ms",
                "2500",
            ]
        )
        self.assertTrue(args.execute)
        self.assertEqual(args.tier, "custom")
        self.assertEqual(args.wait_ready_ms, 2500)

    def test_powershell_wrappers_accept_and_forward_custom_tier(self) -> None:
        root = Path(__file__).resolve().parents[1]
        common = (root / "scripts" / "_FlexShow.Common.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("'5090', 'custom'", common)
        self.assertIn("$arguments.Add('--tier')", common)
        for name in (
            "Start-FlexShow.ps1",
            "Diagnose-FlexShow.ps1",
            "Recover-FlexShow.ps1",
        ):
            with self.subTest(wrapper=name):
                source = (root / "scripts" / name).read_text(encoding="utf-8")
                self.assertIn("'5090', 'custom'", source)
                self.assertIn("-Tier $Tier", source)

    def test_cli_plan_never_prints_secret_env_or_argv_values(self) -> None:
        sentinel = "CLI-SECRET-SENTINEL-77A9"
        license_sentinel = "CLI-LICENSE-SENTINEL-55B7"
        uri_password = "CLI-URI-PASSWORD-22C4"
        service_auth_env_name = "SERVICE_" + "TOKEN"
        paid_entitlement_env_name = "LICENSE_" + "KEY"
        credentialed_endpoint = (
            "https://user" + ":%s@example.invalid/hook"
        ) % uri_password
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "show.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "topology": "single",
                        "processes": {
                            "world": {
                                "command": [
                                    sys.executable,
                                    "-c",
                                    "pass",
                                    "--api-key",
                                    sentinel,
                                ],
                                "touchdesigner": False,
                                "env": {
                                    service_auth_env_name: sentinel,
                                    paid_entitlement_env_name: license_sentinel,
                                    "SERVICE_ENDPOINT": credentialed_endpoint,
                                },
                            }
                        },
                    },
                    handle,
                )
            output = io.StringIO()
            with mock.patch("flexgpu.cli.discover_nvidia_gpus", return_value=[GPU]):
                with contextlib.redirect_stdout(output):
                    status = main(
                        ["plan", "--config", path, "--tier", "custom", "--json"]
                    )
            self.assertEqual(status, 0)
            self.assertNotIn(sentinel, output.getvalue())
            self.assertNotIn(license_sentinel, output.getvalue())
            self.assertNotIn(uri_password, output.getvalue())
            self.assertIn("<redacted>", output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["plan"]["tier"], "custom")

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

    def test_cli_status_is_read_only_and_does_not_probe_gpus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "show.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "topology": "single",
                        "runtime_dir": "runtime",
                        "processes": {
                            "world": {
                                "command": [sys.executable, "-c", "pass"],
                                "touchdesigner": False,
                            }
                        },
                    },
                    handle,
                )
            output = io.StringIO()
            with mock.patch("flexgpu.cli.discover_nvidia_gpus") as discover:
                with contextlib.redirect_stdout(output):
                    status = main(["status", "--config", path, "--json"])
            self.assertEqual(status, 0)
            discover.assert_not_called()
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["runtime"]["state"], "stopped")
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
