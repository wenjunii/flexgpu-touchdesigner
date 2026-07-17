from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flexgpu.config import validate_config  # noqa: E402
from flexgpu.models import GPUInfo, RuntimeControlError  # noqa: E402
from flexgpu.planner import build_process_plan  # noqa: E402
from flexgpu.runtime import (  # noqa: E402
    MANIFEST_VERSION,
    TOUCHDESIGNER_BUILD_VERSION,
    _config_identity,
    _inspect_process,
    _launch_environment,
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


def _managed_test_python() -> str:
    """Avoid Windows venv redirectors whose Popen PID differs from the child PID."""

    if os.name == "nt" and sys.prefix != sys.base_prefix:
        base_executable = getattr(sys, "_base_executable", "")
        if isinstance(base_executable, str) and os.path.isfile(base_executable):
            return os.path.abspath(base_executable)
    return sys.executable


MANAGED_TEST_PYTHON = _managed_test_python()

SLEEP_SCRIPT = "import time; time.sleep(30)"
READY_SCRIPT = """
import datetime, json, os, time
path = os.environ['FLEXGPU_HEARTBEAT_PATH']
payload = {
    'version': 1,
    'session_id': os.environ['FLEXGPU_SESSION_ID'],
    'role': os.environ['FLEXGPU_ROLE'],
    'pid': os.getpid(),
    'state': 'ready',
    'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
temporary = path + '.' + str(os.getpid()) + '.tmp'
with open(temporary, 'w', encoding='utf-8') as handle:
    json.dump(payload, handle)
os.replace(temporary, path)
time.sleep(30)
"""
READY_IDENTITY_SCRIPT = """
import datetime, json, os, time
path = os.environ['FLEXGPU_HEARTBEAT_PATH']
payload = {
    'version': 1,
    'session_id': os.environ['FLEXGPU_SESSION_ID'],
    'role': os.environ['FLEXGPU_ROLE'],
    'pid': os.getpid(),
    'state': 'ready',
    'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'build': {'version': os.environ['FLEXGPU_EXPECTED_BUILD_VERSION']},
    'config': {'identity': os.environ['FLEXGPU_CONFIG_ID']},
}
temporary = path + '.' + str(os.getpid()) + '.tmp'
with open(temporary, 'w', encoding='utf-8') as handle:
    json.dump(payload, handle)
os.replace(temporary, path)
time.sleep(30)
"""
MALFORMED_SCRIPT = """
import os, time
with open(os.environ['FLEXGPU_HEARTBEAT_PATH'], 'w', encoding='utf-8') as handle:
    handle.write('{malformed')
time.sleep(30)
"""


def make_config(directory: str, script: str, *, supervisor: dict[str, object] | None = None):
    source = os.path.join(directory, "show.json")
    config = validate_config(
        {
            "topology": "single",
            "runtime_dir": os.path.join(directory, "runtime"),
            "processes": {
                "world": {
                    "command": [MANAGED_TEST_PYTHON, "-c", script],
                    "touchdesigner": False,
                }
            },
        },
        source,
    )
    return replace(config, supervisor=supervisor or {})


def make_ai_config(directory: str, script: str):
    source = os.path.join(directory, "ai.json")
    config = validate_config(
        {
            "topology": "dual_network",
            "node_role": "ai",
            "runtime_dir": os.path.join(directory, "runtime"),
            "transport": {
                "type": "touch_tcp",
                "peer_host": "192.0.2.20",
                "atlas_width": 1024,
                "atlas_height": 512,
                "atlas_fps": 5,
                "atlas_port": 12000,
                "control_port": 12001,
                "heartbeat_port": 12002,
                "heartbeat_timeout_ms": 2000,
            },
            "processes": {
                "ai": {
                    "command": [MANAGED_TEST_PYTHON, "-c", script],
                    "touchdesigner": False,
                }
            },
        },
        source,
    )
    return replace(
        config,
        supervisor={"heartbeat_timeout_ms": 10000, "readiness_timeout_ms": 2000},
    )


class RuntimeSecurityTests(unittest.TestCase):
    def _stop(self, config) -> None:
        with (
            mock.patch("flexgpu.runtime.GRACEFUL_STOP_SECONDS", 0.0),
            mock.patch("flexgpu.runtime.FORCED_STOP_SECONDS", 2.0),
        ):
            try:
                stop_managed(config, execute=True)
            except RuntimeControlError:
                pass

    def test_secret_never_appears_in_preview_manifest_or_launch_error(self) -> None:
        sentinel = "FLEXGPU-SECRET-SENTINEL-8E31"
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, SLEEP_SCRIPT)
            definition = config.processes["world"]
            config = replace(
                config,
                processes={
                    "world": replace(
                        definition,
                        command=(
                            MANAGED_TEST_PYTHON,
                            "-c",
                            SLEEP_SCRIPT,
                            "--token",
                            sentinel,
                        ),
                        env={"SERVICE_API_TOKEN": sentinel},
                    )
                },
            )
            plan = build_process_plan(config, [GPU])
            preview = start_plan(plan, config, execute=False)
            self.assertNotIn(sentinel, json.dumps(preview))
            result = start_plan(plan, config, execute=True)
            try:
                self.assertNotIn(sentinel, json.dumps(result))
                self.assertNotIn(
                    sentinel, Path(manifest_path(config)).read_text(encoding="utf-8")
                )
            finally:
                self._stop(config)

            missing = replace(
                definition,
                command=(sentinel, "--api-key", sentinel),
                env={"SERVICE_API_TOKEN": sentinel},
            )
            failed_config = replace(config, processes={"world": missing})
            failed_plan = build_process_plan(failed_config, [GPU])
            with self.assertRaises(RuntimeControlError) as raised:
                start_plan(failed_plan, failed_config, execute=True)
            self.assertNotIn(sentinel, str(raised.exception))
            self.assertNotIn(
                sentinel, Path(manifest_path(failed_config)).read_text(encoding="utf-8")
            )

    def test_process_without_heartbeat_is_alive_for_compatible_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, SLEEP_SCRIPT)
            plan = build_process_plan(config, [GPU])
            start_plan(plan, config, execute=True)
            try:
                status = runtime_status(config)
                self.assertEqual(status["processes"][0]["status"], "alive")
                self.assertEqual(status["summary"]["alive"], 1)
                self.assertEqual(status["summary"]["running"], 1)

                manifest_file = Path(manifest_path(config))
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                manifest["processes"][0]["started_at"] = (
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat()
                manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
                stale = runtime_status(config)
                self.assertEqual(stale["processes"][0]["status"], "stale")
            finally:
                self._stop(config)

    def test_touchdesigner_launch_injects_build_and_semantic_config_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, SLEEP_SCRIPT)
            process = replace(
                build_process_plan(config, [GPU]).processes[0],
                project_path=os.path.join(directory, "FlexShow.toe"),
            )
            heartbeat = os.path.join(directory, "runtime", "heartbeat.json")
            environment = _launch_environment(
                process, config, "session-1", heartbeat, 5000
            )
            self.assertEqual(
                environment["FLEXGPU_EXPECTED_BUILD_VERSION"],
                TOUCHDESIGNER_BUILD_VERSION,
            )
            self.assertEqual(environment["FLEXGPU_CONFIG_ID"], _config_identity(config))
            self.assertEqual(
                Path(environment["FLEXGPU_SRC"]),
                Path(__file__).resolve().parents[1] / "src",
            )

    def test_touchdesigner_identity_includes_all_cli_mode_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "show.json")
            raw = {
                "topology": "single",
                "experience": "installation",
                "completion": "fog",
                "tier": "3080ti_16gb",
                "runtime_dir": os.path.join(directory, "runtime"),
                "processes": {
                    "world": {
                        "command": [MANAGED_TEST_PYTHON, "-c", SLEEP_SCRIPT],
                        "touchdesigner": False,
                    }
                },
            }
            file_config = validate_config(raw, source)
            effective_config = validate_config(
                raw,
                source,
                overrides={
                    "experience": "vr",
                    "completion": "procedural",
                    "tier": "4090",
                },
            )
            original = build_process_plan(effective_config, [GPU])
            process = replace(
                original.processes[0],
                project_path=os.path.join(directory, "FlexShow.toe"),
            )
            environment = _launch_environment(
                process,
                effective_config,
                "override-session",
                os.path.join(directory, "runtime", "heartbeat.json"),
                5000,
            )
            self.assertEqual(process.env["FLEXGPU_EXPERIENCE"], "vr")
            self.assertEqual(process.env["FLEXGPU_COMPLETION"], "procedural")
            self.assertEqual(process.env["FLEXGPU_TIER"], "4090")
            self.assertNotEqual(
                _config_identity(file_config), _config_identity(effective_config)
            )
            self.assertEqual(
                environment["FLEXGPU_CONFIG_ID"],
                _config_identity(effective_config),
            )

    def test_changed_non_launch_config_cannot_reuse_active_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original_config = make_config(directory, SLEEP_SCRIPT)
            changed_raw = json.loads(json.dumps(original_config.raw))
            changed_raw["render"] = {"point_size_px": 7.0}
            changed_config = validate_config(
                changed_raw, original_config.source_path
            )
            original_plan = build_process_plan(original_config, [GPU])
            changed_plan = build_process_plan(changed_config, [GPU])
            original_process = original_plan.processes[0]
            changed_process = changed_plan.processes[0]
            self.assertEqual(original_process.command, changed_process.command)
            self.assertEqual(original_process.cwd, changed_process.cwd)
            self.assertEqual(original_process.env, changed_process.env)

            start_plan(original_plan, original_config, execute=True)
            try:
                with self.assertRaisesRegex(
                    RuntimeControlError, "semantic config differs"
                ):
                    start_plan(changed_plan, changed_config, execute=True)
            finally:
                self._stop(original_config)

    def test_touchdesigner_readiness_requires_expected_build_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, READY_IDENTITY_SCRIPT)
            original = build_process_plan(config, [GPU])
            process = replace(
                original.processes[0],
                project_path=os.path.join(directory, "FlexShow.toe"),
            )
            plan = replace(original, processes=(process,))
            start_plan(plan, config, execute=True, wait_ready_ms=3000)
            try:
                self.assertEqual(
                    runtime_status(config)["processes"][0]["status"], "ready"
                )
                heartbeat_path = next(
                    Path(manifest_path(config)).parent.glob("flexgpu-heartbeat-*.json")
                )
                payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
                payload["config"]["identity"] = "0" * 64
                heartbeat_path.write_text(json.dumps(payload), encoding="utf-8")
                status = runtime_status(config)["processes"][0]
                self.assertEqual(status["status"], "stale")
                self.assertIn("config identity", status["reason"])
            finally:
                self._stop(config)

    def test_inherited_environment_change_prevents_unsafe_process_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, SLEEP_SCRIPT)
            plan = build_process_plan(config, [GPU])
            with mock.patch.dict(os.environ, {"ART_SHOW_MODE": "first"}):
                start_plan(plan, config, execute=True)
                try:
                    os.environ["ART_SHOW_MODE"] = "second"
                    with self.assertRaisesRegex(
                        RuntimeControlError, "environment differs"
                    ):
                        start_plan(plan, config, execute=True)
                finally:
                    self._stop(config)

    def test_atomic_ready_heartbeat_and_frozen_stale_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(
                directory,
                READY_SCRIPT,
                supervisor={"heartbeat_timeout_ms": 10000, "readiness_timeout_ms": 5000},
            )
            plan = build_process_plan(config, [GPU])
            start_plan(plan, config, execute=True)
            try:
                ready = runtime_status(config)
                self.assertEqual(ready["state"], "ready")
                self.assertEqual(ready["processes"][0]["status"], "ready")
                heartbeat_path = next(
                    Path(manifest_path(config)).parent.glob("flexgpu-heartbeat-*.json")
                )
                payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
                payload["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
                temporary = heartbeat_path.with_suffix(".test.tmp")
                temporary.write_text(json.dumps(payload), encoding="utf-8")
                os.replace(temporary, heartbeat_path)
                stale = runtime_status(config)
                self.assertEqual(stale["state"], "stale")
                self.assertEqual(stale["processes"][0]["status"], "stale")
            finally:
                self._stop(config)

    def test_malformed_heartbeat_is_stale_and_required_wait_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, MALFORMED_SCRIPT)
            plan = build_process_plan(config, [GPU])
            start_plan(plan, config, execute=True)
            try:
                heartbeat = Path(manifest_path(config)).parent / next(
                    path.name
                    for path in Path(manifest_path(config)).parent.glob("flexgpu-heartbeat-*.json")
                )
                self.assertTrue(heartbeat.exists())
                self.assertEqual(runtime_status(config)["processes"][0]["status"], "stale")
            finally:
                self._stop(config)

        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, MALFORMED_SCRIPT)
            plan = build_process_plan(config, [GPU])
            with self.assertRaisesRegex(RuntimeControlError, "readiness timed out"):
                start_plan(plan, config, execute=True, wait_ready_ms=300)
            self.assertEqual(runtime_status(config)["state"], "degraded")

    def test_crash_before_heartbeat_fails_required_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, "raise SystemExit(7)")
            plan = build_process_plan(config, [GPU])
            with self.assertRaises(RuntimeControlError):
                start_plan(plan, config, execute=True, wait_ready_ms=500)
            status = runtime_status(config)
            self.assertIn(status["state"], {"degraded", "stopped"})

    def test_ai_recovery_counts_only_application_ready_attempt_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_ai_config(directory, READY_SCRIPT)
            plan = build_process_plan(config, [GPU])
            result = recover_managed(
                plan, config, attempts=1, execute=True, wait_ready_ms=3000
            )
            try:
                self.assertEqual(result["attempts_used"], 1)
                self.assertEqual(runtime_status(config)["processes"][0]["status"], "ready")
            finally:
                self._stop(config)

        with tempfile.TemporaryDirectory() as directory:
            config = make_ai_config(directory, MALFORMED_SCRIPT)
            plan = build_process_plan(config, [GPU])
            with self.assertRaisesRegex(RuntimeControlError, "exhausted 2 attempts"):
                recover_managed(
                    plan, config, attempts=2, execute=True, wait_ready_ms=300
                )
            manifest = json.loads(Path(manifest_path(config)).read_text(encoding="utf-8"))
            self.assertEqual(manifest["state"], "degraded")
            self.assertEqual(manifest["recovery"]["phase"], "failed")

    def test_runtime_dir_rejects_filesystem_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, SLEEP_SCRIPT)
            config = replace(config, runtime_dir=os.path.abspath(os.path.sep))
            with self.assertRaisesRegex(RuntimeControlError, "filesystem root"):
                runtime_status(config)

    def test_runtime_dir_rejects_unc_network_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, SLEEP_SCRIPT)
            config = replace(config, runtime_dir=r"\\server\share\flexgpu-runtime")
            with self.assertRaisesRegex(RuntimeControlError, "local storage"):
                runtime_status(config)

    def test_manifest_version_is_required_integer_and_explicitly_supported(self) -> None:
        cases = (
            (None, "missing"),
            (True, "boolean"),
            (str(MANIFEST_VERSION), "string"),
            (float(MANIFEST_VERSION), "float"),
            (0, "zero"),
            (2, "unreleased-v2"),
            (4, "unreleased-v4"),
            (5, "unreleased-v5"),
            (MANIFEST_VERSION + 1, "future"),
        )
        for version, label in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                config = make_config(directory, SLEEP_SCRIPT)
                path = Path(manifest_path(config))
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "config": config.source_path,
                    "processes": [],
                }
                if version is not None:
                    payload["version"] = version
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeControlError, "manifest version"):
                    runtime_status(config)

    def test_manifest_owner_path_and_current_config_identity_fail_closed(self) -> None:
        owner_cases = (
            (None, "missing"),
            (False, "boolean"),
            (7, "integer"),
            ("", "empty"),
            (" padded ", "padded"),
        )
        for owner, label in owner_cases:
            with self.subTest(owner=label), tempfile.TemporaryDirectory() as directory:
                config = make_config(directory, SLEEP_SCRIPT)
                path = Path(manifest_path(config))
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "version": MANIFEST_VERSION,
                    "config_sha256": _config_identity(config),
                    "processes": [],
                }
                if owner is not None:
                    payload["config"] = owner
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeControlError, "config owner path"):
                    runtime_status(config)

        identity_cases = (
            (None, "missing"),
            (False, "boolean"),
            (7, "integer"),
            ("0" * 63, "short"),
            ("G" * 64, "non-hex"),
        )
        for identity, label in identity_cases:
            with self.subTest(identity=label), tempfile.TemporaryDirectory() as directory:
                config = make_config(directory, SLEEP_SCRIPT)
                path = Path(manifest_path(config))
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "version": MANIFEST_VERSION,
                    "config": config.source_path,
                    "processes": [],
                }
                if identity is not None:
                    payload["config_sha256"] = identity
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(
                    RuntimeControlError, "semantic config identity"
                ):
                    runtime_status(config)

    def test_released_v3_is_read_only_and_never_upgraded_by_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, SLEEP_SCRIPT)
            plan = build_process_plan(config, [GPU])
            path = Path(manifest_path(config))
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 3,
                "config": config.source_path,
                "processes": [],
            }
            original = json.dumps(payload, sort_keys=True)
            path.write_text(original, encoding="utf-8")
            self.assertEqual(runtime_status(config)["state"], "stopped")
            self.assertEqual(stop_managed(config, execute=False)["targets"], [])
            with self.assertRaisesRegex(RuntimeControlError, "read-only"):
                start_plan(plan, config, execute=True)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_current_mutation_requires_matching_semantic_config_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, SLEEP_SCRIPT)
            plan = build_process_plan(config, [GPU])
            path = Path(manifest_path(config))
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": MANIFEST_VERSION,
                "config": config.source_path,
                "config_sha256": "0" * 64,
                "processes": [],
            }
            original = json.dumps(payload, sort_keys=True)
            path.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeControlError, "semantic config differs"):
                start_plan(plan, config, execute=True)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_genuine_identity_verified_v3_stop_does_not_relabel_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, SLEEP_SCRIPT)
            path = Path(manifest_path(config))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "config": config.source_path,
                        "processes": [
                            {
                                "role": "world",
                                "pid": 424242,
                                "identity": {
                                    "creation_token": "legacy-token",
                                    "executable": sys.executable,
                                    "command_line_sha256": "a" * 64,
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            stopped = {
                "stopped": [{"role": "world", "pid": 424242}],
                "graceful_requested": [],
                "graceful_stopped": [],
                "forced": [],
                "survivors": [],
                "errors": [],
            }
            with (
                mock.patch(
                    "flexgpu.runtime._record_process_status",
                    side_effect=[
                        ("match", ""),
                        ("match", ""),
                        ("dead", "process is no longer running"),
                    ],
                ),
                mock.patch(
                    "flexgpu.runtime._stop_verified_records", return_value=stopped
                ),
                mock.patch("flexgpu.runtime._atomic_manifest_write") as write,
            ):
                result = stop_managed(config, execute=True)
            self.assertTrue(result["legacy_manifest"])
            self.assertFalse(path.exists())
            write.assert_not_called()

    def test_invalid_manifest_executable_path_is_structured_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, SLEEP_SCRIPT)
            identity = _inspect_process(os.getpid())
            self.assertIsNotNone(identity)
            malformed_identity = dict(identity or {})
            malformed_identity["executable"] = "bad\x00path"
            path = Path(manifest_path(config))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "version": MANIFEST_VERSION,
                        "config": config.source_path,
                        "config_sha256": _config_identity(config),
                        "processes": [
                            {
                                "role": "world",
                                "pid": os.getpid(),
                                "identity": malformed_identity,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            status = runtime_status(config)
            self.assertEqual(status["state"], "ownership_error")
            self.assertEqual(status["processes"][0]["status"], "refused")
            self.assertIn("executable path is invalid", status["processes"][0]["reason"])
            preview = stop_managed(config, execute=False)
            self.assertEqual(preview["targets"], [])
            self.assertIn("executable path is invalid", preview["refused"][0]["reason"])
            with mock.patch("flexgpu.runtime.os.kill") as kill:
                with self.assertRaisesRegex(
                    RuntimeControlError, "ownership could not be verified"
                ):
                    stop_managed(config, execute=True)
                kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
