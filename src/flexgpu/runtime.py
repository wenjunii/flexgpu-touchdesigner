"""Small process supervisor used by the CLI's explicit execute mode."""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from .models import FlexConfig, ProcessPlan, RuntimeControlError


MANIFEST_NAME = "flexgpu-manifest.json"


def runtime_directory(config: FlexConfig) -> str:
    path = os.path.expandvars(os.path.expanduser(config.runtime_dir))
    if not os.path.isabs(path):
        path = os.path.join(config.base_dir, path)
    return os.path.abspath(path)


def manifest_path(config: FlexConfig) -> str:
    return os.path.join(runtime_directory(config), MANIFEST_NAME)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, int(pid)
        )
        if not handle:
            return False
        try:
            # OpenProcess can still return a handle for a process object that
            # has terminated but has not been fully released by Windows.  PID
            # existence alone therefore left stale manifests after a normal TD
            # shutdown.  STILL_ACTIVE (259) is the authoritative state.
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _read_manifest(path: str) -> dict[str, Any] | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RuntimeControlError("unable to read runtime manifest %s: %s" % (path, exc)) from exc
    if not isinstance(data, dict) or not isinstance(data.get("processes", []), list):
        raise RuntimeControlError("runtime manifest is malformed: %s" % path)
    return data


def _atomic_manifest_write(path: str, data: Mapping[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, delete=False, suffix=".tmp"
        ) as handle:
            temporary = handle.name
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def start_plan(
    plan: ProcessPlan, config: FlexConfig, execute: bool = False
) -> dict[str, Any]:
    """Preview or start a plan; mutation is impossible unless execute is true."""

    target_manifest = manifest_path(config)
    preview = {
        "mode": "execute" if execute else "dry-run",
        "manifest": target_manifest,
        "processes": [
            {"role": process.role, "command": list(process.command), "cwd": process.cwd}
            for process in plan.processes
        ],
    }
    if not execute:
        return preview

    existing = _read_manifest(target_manifest)
    active_by_role: dict[str, dict[str, Any]] = {}
    if existing:
        for item in existing.get("processes", []):
            if not _pid_alive(int(item.get("pid", 0))):
                continue
            role = str(item.get("role", ""))
            if not role or role in active_by_role:
                raise RuntimeControlError(
                    "runtime manifest contains ambiguous active process ownership"
                )
            active_by_role[role] = item
        existing_config = str(existing.get("config", ""))
        if active_by_role and existing_config and config.source_path:
            if os.path.normcase(os.path.abspath(existing_config)) != os.path.normcase(
                os.path.abspath(config.source_path)
            ):
                raise RuntimeControlError(
                    "runtime manifest belongs to a different config: %s" % existing_config
                )
        planned_roles = {process.role for process in plan.processes}
        unexpected = sorted(set(active_by_role).difference(planned_roles))
        if unexpected:
            raise RuntimeControlError(
                "runtime manifest owns active roles outside this plan: " + ", ".join(unexpected)
            )
        for process in plan.processes:
            current = active_by_role.get(process.role)
            if current and list(process.command) != current.get("command"):
                raise RuntimeControlError(
                    "active %s process command differs from the new plan; stop it before restarting"
                    % process.role
                )

    runtime_dir = runtime_directory(config)
    os.makedirs(runtime_dir, exist_ok=True)
    started: list[tuple[subprocess.Popen[Any], Any]] = []
    records: list[dict[str, Any]] = []
    reused: list[dict[str, Any]] = []
    try:
        for process in plan.processes:
            if process.role in active_by_role:
                record = dict(active_by_role[process.role])
                records.append(record)
                reused.append(record)
                continue
            log_path = os.path.join(runtime_dir, "%s.log" % process.role)
            log_handle = open(log_path, "a", encoding="utf-8", buffering=1)
            env = os.environ.copy()
            env.update(process.env)
            kwargs: dict[str, Any] = {
                "cwd": process.cwd,
                "env": env,
                "stdin": subprocess.DEVNULL,
                "stdout": log_handle,
                "stderr": subprocess.STDOUT,
                "shell": False,
            }
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                kwargs["start_new_session"] = True
            try:
                child = subprocess.Popen(list(process.command), **kwargs)
            except OSError:
                log_handle.close()
                raise
            started.append((child, log_handle))
            records.append(
                {
                    "role": process.role,
                    "pid": child.pid,
                    "command": list(process.command),
                    "cwd": process.cwd,
                    "log": log_path,
                    "gpu_uuid": process.gpu.uuid if process.gpu else "",
                }
            )
        manifest = {
            "version": 1,
            "started_at": (
                existing.get("started_at")
                if existing and reused
                else datetime.now(timezone.utc).isoformat()
            ),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "config": config.source_path,
            "topology": plan.topology,
            "experience": plan.experience,
            "processes": records,
        }
        if started or not existing:
            _atomic_manifest_write(target_manifest, manifest)
    except (OSError, RuntimeControlError) as exc:
        for child, _handle in reversed(started):
            if child.poll() is None:
                child.terminate()
        raise RuntimeControlError("unable to start process plan: %s" % exc) from exc
    finally:
        for _child, handle in started:
            handle.close()
    preview["started"] = [record for record in records if record not in reused]
    preview["reused"] = reused
    return preview


def stop_managed(config: FlexConfig, execute: bool = False) -> dict[str, Any]:
    """Preview or stop only PIDs recorded in this config's runtime manifest."""

    path = manifest_path(config)
    manifest = _read_manifest(path)
    records = manifest.get("processes", []) if manifest else []
    targets = [
        {"role": item.get("role", "unknown"), "pid": int(item.get("pid", 0))}
        for item in records
        if _pid_alive(int(item.get("pid", 0)))
    ]
    result: dict[str, Any] = {
        "mode": "execute" if execute else "dry-run",
        "manifest": path,
        "targets": targets,
    }
    if not execute:
        return result
    errors: list[str] = []
    for target in reversed(targets):
        try:
            os.kill(target["pid"], signal.SIGTERM)
        except OSError as exc:
            errors.append("%s(pid=%s): %s" % (target["role"], target["pid"], exc))
    # TouchDesigner may spend a few seconds flushing the .toe and shutting down
    # its GPU context.  Two seconds was too short on the validated 3080 Ti
    # laptop and left a stale manifest even though the process exited normally
    # just afterwards.
    stop_grace_seconds = 8.0
    deadline = time.monotonic() + stop_grace_seconds
    survivors = list(targets)
    while survivors and time.monotonic() < deadline:
        survivors = [target for target in survivors if _pid_alive(target["pid"])]
        if survivors:
            time.sleep(0.05)
    result["stopped"] = [target for target in targets if target not in survivors]
    result["survivors"] = survivors
    result["errors"] = errors
    if not survivors and not errors and os.path.isfile(path):
        os.unlink(path)
    if survivors:
        raise RuntimeControlError(
            "managed processes did not exit within %.0f seconds: %s"
            % (
                stop_grace_seconds,
                ", ".join(
                    "%s(pid=%s)" % (target["role"], target["pid"])
                    for target in survivors
                ),
            )
        )
    if errors:
        raise RuntimeControlError("some managed processes could not be stopped: " + "; ".join(errors))
    return result
