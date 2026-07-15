"""Small process supervisor used by the CLI's explicit execute mode."""

from __future__ import annotations

import ctypes
import hashlib
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


def _windows_kernel32():
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32, wintypes


def _open_windows_process(pid: int, terminate: bool = False):
    process_query_limited_information = 0x1000
    process_terminate = 0x0001
    kernel32, _wintypes = _windows_kernel32()
    access = process_query_limited_information | (process_terminate if terminate else 0)
    handle = kernel32.OpenProcess(access, False, int(pid))
    return handle or None


def _close_windows_process(handle: Any) -> None:
    kernel32, _wintypes = _windows_kernel32()
    kernel32.CloseHandle(handle)


def _windows_process_core_from_handle(handle: Any) -> dict[str, str] | None:
    """Return stable kernel identity for an already-open process handle."""

    kernel32, wintypes = _windows_kernel32()
    exit_code = wintypes.DWORD()
    if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
        return None
    if exit_code.value != 259:  # STILL_ACTIVE
        return None

    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel_time = wintypes.FILETIME()
    user_time = wintypes.FILETIME()
    if not kernel32.GetProcessTimes(
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    ):
        return None

    size = wintypes.DWORD(32768)
    image = ctypes.create_unicode_buffer(size.value)
    if not kernel32.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
        return None

    creation_token = (int(creation.dwHighDateTime) << 32) | int(
        creation.dwLowDateTime
    )
    return {
        "creation_token": str(creation_token),
        "executable": os.path.realpath(image.value),
    }


def _windows_process_core(pid: int) -> dict[str, str] | None:
    """Return stable kernel identity without trusting PID existence alone."""

    handle = _open_windows_process(pid)
    if handle is None:
        return None
    try:
        return _windows_process_core_from_handle(handle)
    finally:
        _close_windows_process(handle)


def _windows_command_line(pid: int) -> str | None:
    """Read the actual Win32 command line without interpolating user input."""

    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    powershell = os.path.join(
        system_root,
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )
    if not os.path.isfile(powershell):
        return None
    script = (
        "$ErrorActionPreference='Stop';"
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
        "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = %d';"
        "if($null -eq $p){exit 3};"
        "$p.CommandLine | ConvertTo-Json -Compress" % int(pid)
    )
    try:
        completed = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=5,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    try:
        value = json.loads(completed.stdout.lstrip("\ufeff").strip())
    except ValueError:
        return None
    return value if isinstance(value, str) and value else None


def _inspect_windows_process_handle(pid: int, handle: Any) -> dict[str, str] | None:
    """Inspect a PID while retaining the exact kernel process object."""

    before = _windows_process_core_from_handle(handle)
    if before is None:
        return None
    command_line = _windows_command_line(pid)
    after = _windows_process_core_from_handle(handle)
    if command_line is None or before != after:
        return None
    identity = dict(after)
    identity["command_line_sha256"] = hashlib.sha256(
        command_line.encode("utf-8")
    ).hexdigest()
    return identity


def _terminate_windows_process(handle: Any) -> bool:
    kernel32, _wintypes = _windows_kernel32()
    return bool(kernel32.TerminateProcess(handle, 1))


def _inspect_process(pid: int) -> dict[str, str] | None:
    """Inspect creation, executable and argv identity; fail closed on ambiguity."""

    if pid <= 0:
        return None
    if os.name == "nt":
        handle = _open_windows_process(pid)
        if handle is None:
            return None
        try:
            return _inspect_windows_process_handle(pid, handle)
        finally:
            _close_windows_process(handle)

    proc_root = "/proc/%d" % pid
    try:
        with open(os.path.join(proc_root, "stat"), "r", encoding="utf-8") as handle:
            first_stat = handle.read()
        close_paren = first_stat.rfind(")")
        creation_token = first_stat[close_paren + 2 :].split()[19]
        executable = os.path.realpath(os.readlink(os.path.join(proc_root, "exe")))
        with open(os.path.join(proc_root, "cmdline"), "rb") as handle:
            command_line = handle.read()
        with open(os.path.join(proc_root, "stat"), "r", encoding="utf-8") as handle:
            second_stat = handle.read()
        second_close = second_stat.rfind(")")
        second_token = second_stat[second_close + 2 :].split()[19]
    except (OSError, IndexError, ValueError):
        return None
    if not command_line or creation_token != second_token:
        return None
    return {
        "creation_token": creation_token,
        "executable": executable,
        "command_line_sha256": hashlib.sha256(command_line).hexdigest(),
    }


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
        # OpenProcess can still return a handle for a process object that has
        # terminated but has not been released. _windows_process_core checks
        # STILL_ACTIVE and also avoids unsafe 32-bit HANDLE truncation.
        return _windows_process_core(pid) is not None
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
    seen_roles: set[str] = set()
    seen_pids: set[int] = set()
    for index, item in enumerate(data.get("processes", [])):
        if not isinstance(item, dict):
            raise RuntimeControlError(
                "runtime manifest process %d is malformed: %s" % (index, path)
            )
        pid_value = item.get("pid")
        role_value = item.get("role")
        if type(pid_value) is not int:
            raise RuntimeControlError(
                "runtime manifest process %d has an invalid PID: %s" % (index, path)
            )
        if not isinstance(role_value, str) or not role_value.strip():
            raise RuntimeControlError(
                "runtime manifest process %d has an invalid role: %s" % (index, path)
            )
        pid = pid_value
        role = role_value.strip()
        if (
            pid <= 0
            or role != role_value
            or pid in seen_pids
            or role in seen_roles
        ):
            raise RuntimeControlError(
                "runtime manifest has an invalid or duplicate role/PID: %s" % path
            )
        seen_pids.add(pid)
        seen_roles.add(role)
    return data


def _record_process_status(item: Mapping[str, Any]) -> tuple[str, str]:
    """Return match, dead, or refuse for a manifest-owned process record."""

    pid = int(item.get("pid", 0))
    if not _pid_alive(pid):
        return "dead", "process is no longer running"
    current = _inspect_process(pid)
    if current is None:
        return "refuse", "live process identity could not be inspected"
    return _compare_recorded_identity(item, current)


def _compare_recorded_identity(
    item: Mapping[str, Any], current: Mapping[str, str]
) -> tuple[str, str]:
    recorded = item.get("identity")
    if not isinstance(recorded, dict):
        return "refuse", "legacy record has no process identity"
    required = ("creation_token", "executable", "command_line_sha256")
    if any(not isinstance(recorded.get(key), str) or not recorded.get(key) for key in required):
        return "refuse", "manifest process identity is incomplete"
    if recorded["creation_token"] != current["creation_token"]:
        return "refuse", "PID creation time no longer matches"
    if os.path.normcase(os.path.realpath(recorded["executable"])) != os.path.normcase(
        os.path.realpath(current["executable"])
    ):
        return "refuse", "process executable no longer matches"
    if recorded["command_line_sha256"] != current["command_line_sha256"]:
        return "refuse", "process command line no longer matches"
    return "match", ""


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
            status, reason = _record_process_status(item)
            if status == "dead":
                continue
            if status != "match":
                raise RuntimeControlError(
                    "refusing to reuse manifest PID %s: %s"
                    % (item.get("pid", "unknown"), reason)
                )
            role = str(item.get("role", ""))
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
            identity = None
            identity_deadline = time.monotonic() + 6.0
            while child.poll() is None and time.monotonic() < identity_deadline:
                identity = _inspect_process(child.pid)
                if identity is not None:
                    break
                time.sleep(0.05)
            if identity is None:
                raise RuntimeControlError(
                    "unable to capture a stable identity for %s process PID %s"
                    % (process.role, child.pid)
                )
            records.append(
                {
                    "role": process.role,
                    "pid": child.pid,
                    "command": list(process.command),
                    "cwd": process.cwd,
                    "log": log_path,
                    "gpu_uuid": process.gpu.uuid if process.gpu else "",
                    "identity": identity,
                }
            )
        manifest = {
            "version": 2,
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
    """Preview or stop only identity-verified processes from the manifest."""

    path = manifest_path(config)
    manifest = _read_manifest(path)
    records = manifest.get("processes", []) if manifest else []
    targets: list[dict[str, Any]] = []
    verified_records: list[Mapping[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for item in records:
        status, reason = _record_process_status(item)
        target = {"role": item.get("role", "unknown"), "pid": int(item["pid"])}
        if status == "match":
            targets.append(target)
            verified_records.append(item)
        elif status == "refuse":
            target["reason"] = reason
            refused.append(target)
    result: dict[str, Any] = {
        "mode": "execute" if execute else "dry-run",
        "manifest": path,
        "targets": targets,
        "refused": refused,
    }
    if not execute:
        return result
    if refused:
        raise RuntimeControlError(
            "refusing to stop any process because manifest ownership could not be verified: "
            + "; ".join(
                "%s(pid=%s): %s" % (item["role"], item["pid"], item["reason"])
                for item in refused
            )
        )
    errors: list[str] = []
    stop_grace_seconds = 8.0
    survivors: list[dict[str, Any]]
    if os.name == "nt":
        retained: list[tuple[Mapping[str, Any], dict[str, Any], Any]] = []
        try:
            recheck_failures: list[str] = []
            for item, target in zip(verified_records, targets):
                handle = _open_windows_process(int(item["pid"]), terminate=True)
                if handle is None:
                    recheck_failures.append(
                        "%s(pid=%s): unable to retain a termination handle"
                        % (item["role"], item["pid"])
                    )
                    continue
                current = _inspect_windows_process_handle(int(item["pid"]), handle)
                if current is None:
                    _close_windows_process(handle)
                    recheck_failures.append(
                        "%s(pid=%s): live process identity could not be retained"
                        % (item["role"], item["pid"])
                    )
                    continue
                status, reason = _compare_recorded_identity(item, current)
                if status != "match":
                    _close_windows_process(handle)
                    recheck_failures.append(
                        "%s(pid=%s): %s" % (item["role"], item["pid"], reason)
                    )
                    continue
                retained.append((item, target, handle))
            if recheck_failures:
                raise RuntimeControlError(
                    "process identity changed before shutdown; no processes were terminated: "
                    + "; ".join(recheck_failures)
                )

            for _item, target, handle in reversed(retained):
                if not _terminate_windows_process(handle):
                    errors.append(
                        "%s(pid=%s): TerminateProcess failed"
                        % (target["role"], target["pid"])
                    )

            # Retained handles continue to identify the original kernel objects
            # even if Windows reuses their numeric PIDs during this wait.
            deadline = time.monotonic() + stop_grace_seconds
            survivor_records = list(retained)
            while survivor_records and time.monotonic() < deadline:
                survivor_records = [
                    record
                    for record in survivor_records
                    if _windows_process_core_from_handle(record[2]) is not None
                ]
                if survivor_records:
                    time.sleep(0.05)
            survivors = [record[1] for record in survivor_records]
        finally:
            for _item, _target, handle in retained:
                _close_windows_process(handle)
    else:
        # POSIX has no Windows-style retained process handle in this dependency-
        # free supervisor, so fail closed on a final all-process identity check.
        recheck_failures = []
        for item in verified_records:
            status, reason = _record_process_status(item)
            if status != "match":
                recheck_failures.append(
                    "%s(pid=%s): %s" % (item["role"], item["pid"], reason)
                )
        if recheck_failures:
            raise RuntimeControlError(
                "process identity changed before shutdown; no signals were sent: "
                + "; ".join(recheck_failures)
            )
        for target in reversed(targets):
            try:
                os.kill(target["pid"], signal.SIGTERM)
            except OSError as exc:
                errors.append(
                    "%s(pid=%s): %s" % (target["role"], target["pid"], exc)
                )
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
