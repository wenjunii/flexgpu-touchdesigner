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
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .models import FlexConfig, ProcessPlan, ProcessSpec, RuntimeControlError


MANIFEST_NAME = "flexgpu-manifest.json"
LOCK_NAME = "flexgpu-runtime.lock"
MANIFEST_VERSION = 3
MAX_RECOVERY_ATTEMPTS = 3
GRACEFUL_STOP_SECONDS = 8.0
FORCED_STOP_SECONDS = 4.0
_UNWRITTEN_LOCK_GRACE_SECONDS = 30.0
_LOCAL_CHILDREN: dict[int, subprocess.Popen[Any]] = {}


def _environment_fingerprint(environment: Mapping[str, str]) -> str:
    """Hash launch settings without persisting possible secret values."""

    payload = json.dumps(
        sorted((str(key), str(value)) for key, value in environment.items()),
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


def _request_windows_graceful_shutdown(pid: int) -> bool:
    """Post ``WM_CLOSE`` to every top-level window owned by ``pid``.

    TouchDesigner is a GUI process, so a window-close request is the least
    surprising dependency-free shutdown mechanism available to the launcher.
    The caller retains an identity-verified process handle and may force-stop
    that exact kernel process only after the grace period expires.
    """

    if os.name != "nt" or pid <= 0:
        return False
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    enum_windows = user32.EnumWindows
    get_window_pid = user32.GetWindowThreadProcessId
    post_message = user32.PostMessageW
    enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    enum_windows.argtypes = [enum_callback, wintypes.LPARAM]
    enum_windows.restype = wintypes.BOOL
    get_window_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    get_window_pid.restype = wintypes.DWORD
    post_message.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    post_message.restype = wintypes.BOOL
    posted = 0

    @enum_callback
    def callback(window: Any, _parameter: Any) -> bool:
        nonlocal posted
        owner_pid = wintypes.DWORD()
        get_window_pid(window, ctypes.byref(owner_pid))
        if int(owner_pid.value) == int(pid):
            if post_message(window, 0x0010, 0, 0):  # WM_CLOSE
                posted += 1
        return True

    try:
        enum_windows(callback, 0)
    except (OSError, ValueError):
        return False
    return posted > 0


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


def lock_path(config: FlexConfig) -> str:
    return os.path.join(runtime_directory(config), LOCK_NAME)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_lock_record(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _lock_snapshot(path: str) -> dict[str, Any]:
    """Inspect a mutation lock without creating, deleting, or repairing it."""

    record = _read_lock_record(path)
    if record is None:
        return {"present": False, "active": False, "pid": None, "created_at": None}
    pid = record.get("pid")
    active = type(pid) is int and pid > 0 and _pid_alive(pid)
    return {
        "present": True,
        "active": bool(active),
        "pid": pid if type(pid) is int and pid > 0 else None,
        "created_at": record.get("created_at")
        if isinstance(record.get("created_at"), str)
        else None,
    }


class _RuntimeMutationLock:
    """Short-lived cross-process lock for manifest and process mutations.

    Creation with ``O_EXCL`` is atomic on supported local Windows filesystems.
    A well-formed lock is reclaimed only when its owner PID is no longer alive.
    A just-created but not-yet-written lock fails closed; an old malformed lock
    can be reclaimed after a bounded grace period.
    """

    def __init__(self, config: FlexConfig) -> None:
        self.path = lock_path(config)
        self.nonce = uuid.uuid4().hex
        self.acquired = False

    def _can_reclaim(self) -> bool:
        record = _read_lock_record(self.path)
        if record is None:
            return True
        pid = record.get("pid")
        if type(pid) is int and pid > 0:
            return not _pid_alive(pid)
        try:
            age = max(0.0, time.time() - os.path.getmtime(self.path))
        except OSError:
            return False
        return age >= _UNWRITTEN_LOCK_GRACE_SECONDS

    def __enter__(self) -> "_RuntimeMutationLock":
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for _attempt in range(3):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                if not self._can_reclaim():
                    owner = _lock_snapshot(self.path)
                    detail = (
                        " by PID %s" % owner["pid"]
                        if owner.get("active") and owner.get("pid")
                        else ""
                    )
                    raise RuntimeControlError(
                        "another FlexShow runtime mutation is active%s" % detail
                    )
                try:
                    os.unlink(self.path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise RuntimeControlError(
                        "unable to reclaim stale runtime lock: %s" % exc
                    ) from exc
                continue
            except OSError as exc:
                raise RuntimeControlError("unable to acquire runtime lock: %s" % exc) from exc

            record = {
                "version": 1,
                "pid": os.getpid(),
                "lock_nonce": self.nonce,
                "created_at": _utc_now(),
            }
            try:
                payload = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
                os.write(descriptor, payload)
                os.fsync(descriptor)
            except OSError as exc:
                os.close(descriptor)
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
                raise RuntimeControlError("unable to write runtime lock: %s" % exc) from exc
            os.close(descriptor)
            self.acquired = True
            return self
        raise RuntimeControlError("unable to acquire runtime lock after stale-lock recovery")

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        if not self.acquired:
            return
        record = _read_lock_record(self.path)
        if isinstance(record, dict) and record.get("lock_nonce") == self.nonce:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
        self.acquired = False


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


def _manifest_for_config(config: FlexConfig) -> dict[str, Any] | None:
    path = manifest_path(config)
    manifest = _read_manifest(path)
    if manifest:
        recorded_config = manifest.get("config")
        if isinstance(recorded_config, str) and recorded_config and config.source_path:
            if os.path.normcase(os.path.abspath(recorded_config)) != os.path.normcase(
                os.path.abspath(config.source_path)
            ):
                raise RuntimeControlError(
                    "runtime manifest belongs to a different config: %s" % recorded_config
                )
    return manifest


def _manifest_document(
    config: FlexConfig,
    records: Sequence[Mapping[str, Any]],
    *,
    state: str,
    session_id: str,
    started_at: str,
    plan: ProcessPlan | None = None,
    previous: Mapping[str, Any] | None = None,
    error: str = "",
    recovery: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    prior = previous or {}
    document: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "session_id": session_id,
        "state": state,
        "started_at": started_at,
        "updated_at": _utc_now(),
        "config": config.source_path,
        "topology": plan.topology if plan else prior.get("topology", config.topology),
        "experience": plan.experience if plan else prior.get("experience", config.experience),
        "completion": plan.completion if plan else prior.get("completion", config.completion),
        "planned_roles": (
            [process.role for process in plan.processes]
            if plan
            else list(prior.get("planned_roles", []))
        ),
        "processes": [dict(record) for record in records],
    }
    if error:
        document["last_error"] = error
    if recovery:
        document["recovery"] = dict(recovery)
    return document


def _replace_role_record(records: list[dict[str, Any]], record: Mapping[str, Any]) -> None:
    role = str(record.get("role", ""))
    records[:] = [item for item in records if item.get("role") != role]
    records.append(dict(record))


def _remove_role_record(records: list[dict[str, Any]], role: str) -> None:
    records[:] = [item for item in records if item.get("role") != role]


def _write_runtime_manifest(
    path: str,
    config: FlexConfig,
    records: Sequence[Mapping[str, Any]],
    *,
    state: str,
    session_id: str,
    started_at: str,
    plan: ProcessPlan | None = None,
    previous: Mapping[str, Any] | None = None,
    error: str = "",
    recovery: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    document = _manifest_document(
        config,
        records,
        state=state,
        session_id=session_id,
        started_at=started_at,
        plan=plan,
        previous=previous,
        error=error,
        recovery=recovery,
    )
    _atomic_manifest_write(path, document)
    return document


def _terminate_owned_child(child: subprocess.Popen[Any]) -> bool:
    """Best-effort rollback for a child represented by its original Popen handle."""

    if child.poll() is not None:
        return True
    try:
        if os.name == "nt":
            _request_windows_graceful_shutdown(child.pid)
        else:
            child.terminate()
        child.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if child.poll() is None:
        try:
            child.kill()
            child.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass
    stopped = child.poll() is not None
    if stopped:
        _LOCAL_CHILDREN.pop(child.pid, None)
    return stopped


def _launch_process(
    process: ProcessSpec,
    config: FlexConfig,
    persist: Callable[[Mapping[str, Any]], None],
) -> tuple[subprocess.Popen[Any], dict[str, Any]]:
    runtime_dir = runtime_directory(config)
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

    record: dict[str, Any] = {
        "role": process.role,
        "pid": child.pid,
        "command": list(process.command),
        "cwd": process.cwd,
        "log": log_path,
        "gpu_uuid": process.gpu.uuid if process.gpu else "",
        "environment_sha256": _environment_fingerprint(process.env),
        "launch_state": "identity_pending",
        "started_at": _utc_now(),
    }
    try:
        # Persist the PID immediately. If the controller itself crashes during
        # identity capture, status reports an ambiguous provisional record and
        # all later mutation fails closed instead of forgetting the child.
        persist(record)
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
        record["identity"] = identity
        record["launch_state"] = "running"
        persist(record)
        _LOCAL_CHILDREN[child.pid] = child
        return child, record
    except Exception:
        _terminate_owned_child(child)
        raise
    finally:
        log_handle.close()


def _classify_records(
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[Mapping[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    targets: list[dict[str, Any]] = []
    verified: list[Mapping[str, Any]] = []
    refused: list[dict[str, Any]] = []
    dead: list[dict[str, Any]] = []
    for item in records:
        status, reason = _record_process_status(item)
        target = {"role": item.get("role", "unknown"), "pid": int(item["pid"])}
        if status == "match":
            targets.append(target)
            verified.append(item)
        elif status == "refuse":
            target["reason"] = reason
            refused.append(target)
        else:
            target["reason"] = reason
            dead.append(target)
    return targets, verified, refused, dead


def runtime_status(config: FlexConfig) -> dict[str, Any]:
    """Return manifest/process ownership state without modifying the filesystem."""

    path = manifest_path(config)
    manifest = _manifest_for_config(config)
    lock = _lock_snapshot(lock_path(config))
    if manifest is None:
        return {
            "state": "stopped",
            "manifest_state": None,
            "manifest": path,
            "session_id": None,
            "processes": [],
            "summary": {"running": 0, "dead": 0, "refused": 0},
            "mutation_lock": lock,
        }

    processes: list[dict[str, Any]] = []
    counts = {"running": 0, "dead": 0, "refused": 0}
    for item in manifest.get("processes", []):
        status, reason = _record_process_status(item)
        public_status = (
            "running" if status == "match" else "refused" if status == "refuse" else status
        )
        counts[public_status] += 1
        entry = {
            "role": item.get("role"),
            "pid": item.get("pid"),
            "status": public_status,
            "launch_state": item.get("launch_state"),
            "gpu_uuid": item.get("gpu_uuid", ""),
        }
        if reason:
            entry["reason"] = reason
        processes.append(entry)

    manifest_state = str(manifest.get("state", "running" if processes else "stopped"))
    if counts["refused"]:
        state = "ownership_error"
    elif counts["dead"]:
        state = "degraded"
    elif manifest_state in {"starting", "recovering", "stopping"}:
        state = manifest_state
    elif manifest_state in {"failed", "degraded"}:
        state = "degraded"
    elif counts["running"]:
        state = "running"
    else:
        state = "stopped"
    return {
        "state": state,
        "manifest_state": manifest_state,
        "manifest": path,
        "session_id": manifest.get("session_id"),
        "started_at": manifest.get("started_at"),
        "updated_at": manifest.get("updated_at"),
        "processes": processes,
        "summary": counts,
        "mutation_lock": lock,
        "last_error": manifest.get("last_error"),
        "recovery": manifest.get("recovery"),
    }


def _wait_windows_records(
    retained: Sequence[tuple[Mapping[str, Any], dict[str, Any], Any]], seconds: float
) -> list[tuple[Mapping[str, Any], dict[str, Any], Any]]:
    deadline = time.monotonic() + max(0.0, seconds)
    survivors = list(retained)
    while survivors:
        survivors = [
            record
            for record in survivors
            if _windows_process_core_from_handle(record[2]) is not None
        ]
        if not survivors or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    return survivors


def _stop_verified_records(
    verified_records: Sequence[Mapping[str, Any]], targets: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """Gracefully request shutdown, then force only reverified exact processes."""

    errors: list[str] = []
    graceful_requested: list[dict[str, Any]] = []
    forced: list[dict[str, Any]] = []
    survivors: list[dict[str, Any]] = []
    graceful_stopped: list[dict[str, Any]] = []
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
                    "process identity changed before shutdown; no processes were signaled: "
                    + "; ".join(recheck_failures)
                )

            graceful_records: list[tuple[Mapping[str, Any], dict[str, Any], Any]] = []
            immediate_force_records: list[
                tuple[Mapping[str, Any], dict[str, Any], Any]
            ] = []
            for retained_record in reversed(retained):
                _item, target, _handle = retained_record
                try:
                    if _request_windows_graceful_shutdown(int(target["pid"])):
                        graceful_requested.append(target)
                        graceful_records.append(retained_record)
                    else:
                        immediate_force_records.append(retained_record)
                except Exception as exc:
                    errors.append(
                        "%s(pid=%s): graceful close failed: %s"
                        % (target["role"], target["pid"], exc)
                    )
                    immediate_force_records.append(retained_record)
            after_grace = _wait_windows_records(
                graceful_records, GRACEFUL_STOP_SECONDS
            )
            after_grace.extend(immediate_force_records)
            grace_survivor_targets = [record[1] for record in after_grace]
            graceful_stopped = [
                target for target in targets if target not in grace_survivor_targets
            ]

            for _item, target, handle in reversed(after_grace):
                if _terminate_windows_process(handle):
                    forced.append(target)
                else:
                    errors.append(
                        "%s(pid=%s): TerminateProcess failed"
                        % (target["role"], target["pid"])
                    )
            survivor_records = _wait_windows_records(after_grace, FORCED_STOP_SECONDS)
            survivors = [record[1] for record in survivor_records]
        finally:
            for _item, _target, handle in retained:
                _close_windows_process(handle)
    else:
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
                graceful_requested.append(target)
            except OSError as exc:
                errors.append("%s(pid=%s): %s" % (target["role"], target["pid"], exc))
        deadline = time.monotonic() + GRACEFUL_STOP_SECONDS
        after_grace = list(targets)
        while after_grace and time.monotonic() < deadline:
            after_grace = [target for target in after_grace if _pid_alive(target["pid"])]
            if after_grace:
                time.sleep(0.05)
        graceful_stopped = [target for target in targets if target not in after_grace]
        by_pid = {int(item["pid"]): item for item in verified_records}
        force_candidates: list[dict[str, Any]] = []
        for target in after_grace:
            item = by_pid[int(target["pid"])]
            status, reason = _record_process_status(item)
            if status == "match":
                force_candidates.append(target)
            elif status != "dead":
                errors.append(
                    "%s(pid=%s): identity changed before forced stop: %s"
                    % (target["role"], target["pid"], reason)
                )
        for target in reversed(force_candidates):
            try:
                os.kill(target["pid"], signal.SIGKILL)
                forced.append(target)
            except OSError as exc:
                errors.append("%s(pid=%s): %s" % (target["role"], target["pid"], exc))
        deadline = time.monotonic() + FORCED_STOP_SECONDS
        survivors = list(force_candidates)
        while survivors and time.monotonic() < deadline:
            survivors = [target for target in survivors if _pid_alive(target["pid"])]
            if survivors:
                time.sleep(0.05)

    stopped = [target for target in targets if target not in survivors]
    for target in stopped:
        child = _LOCAL_CHILDREN.pop(int(target["pid"]), None)
        if child is not None:
            try:
                child.wait(timeout=0.1)
            except (OSError, subprocess.TimeoutExpired):
                pass
    return {
        "stopped": stopped,
        "graceful_requested": graceful_requested,
        "graceful_stopped": graceful_stopped,
        "forced": forced,
        "survivors": survivors,
        "errors": errors,
    }


def start_plan(
    plan: ProcessPlan, config: FlexConfig, execute: bool = False
) -> dict[str, Any]:
    """Preview or start a plan; mutation is impossible unless execute is true."""

    target_manifest = manifest_path(config)
    preview: dict[str, Any] = {
        "mode": "execute" if execute else "dry-run",
        "manifest": target_manifest,
        "processes": [
            {"role": process.role, "command": list(process.command), "cwd": process.cwd}
            for process in plan.processes
        ],
    }
    if not execute:
        return preview

    with _RuntimeMutationLock(config):
        existing = _manifest_for_config(config)
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
                active_by_role[str(item.get("role", ""))] = dict(item)
            planned_roles = {process.role for process in plan.processes}
            unexpected = sorted(set(active_by_role).difference(planned_roles))
            if unexpected:
                raise RuntimeControlError(
                    "runtime manifest owns active roles outside this plan: "
                    + ", ".join(unexpected)
                )
            for process in plan.processes:
                current = active_by_role.get(process.role)
                if current and list(process.command) != current.get("command"):
                    raise RuntimeControlError(
                        "active %s process command differs from the new plan; stop it before restarting"
                        % process.role
                    )
                if current and current.get("environment_sha256") != _environment_fingerprint(
                    process.env
                ):
                    raise RuntimeControlError(
                        "active %s process environment differs from the new plan; stop it before applying overrides"
                        % process.role
                    )
                if current and os.path.normcase(os.path.abspath(str(current.get("cwd", "")))) != os.path.normcase(
                    os.path.abspath(process.cwd)
                ):
                    raise RuntimeControlError(
                        "active %s process working directory differs from the new plan; stop it before restarting"
                        % process.role
                    )

        reused = [dict(active_by_role[p.role]) for p in plan.processes if p.role in active_by_role]
        missing = [process for process in plan.processes if process.role not in active_by_role]
        if not missing:
            session_id = str((existing or {}).get("session_id") or uuid.uuid4().hex)
            if (
                not existing
                or existing.get("version") != MANIFEST_VERSION
                or existing.get("state") != "running"
            ):
                _write_runtime_manifest(
                    target_manifest,
                    config,
                    reused,
                    state="running",
                    session_id=session_id,
                    started_at=str((existing or {}).get("started_at") or _utc_now()),
                    plan=plan,
                    previous=existing,
                )
            preview["started"] = []
            preview["reused"] = reused
            preview["session_id"] = session_id
            return preview

        os.makedirs(runtime_directory(config), exist_ok=True)
        records = list(reused)
        session_id = (
            str(existing.get("session_id"))
            if existing and reused and existing.get("session_id")
            else uuid.uuid4().hex
        )
        started_at = (
            str(existing.get("started_at"))
            if existing and reused and existing.get("started_at")
            else _utc_now()
        )
        _write_runtime_manifest(
            target_manifest,
            config,
            records,
            state="starting",
            session_id=session_id,
            started_at=started_at,
            plan=plan,
            previous=existing,
        )
        started_children: list[tuple[subprocess.Popen[Any], str]] = []
        started_records: list[dict[str, Any]] = []

        def persist(record: Mapping[str, Any]) -> None:
            _replace_role_record(records, record)
            _write_runtime_manifest(
                target_manifest,
                config,
                records,
                state="starting",
                session_id=session_id,
                started_at=started_at,
                plan=plan,
                previous=existing,
            )

        try:
            for process in missing:
                child, record = _launch_process(process, config, persist)
                started_children.append((child, process.role))
                started_records.append(record)
            _write_runtime_manifest(
                target_manifest,
                config,
                records,
                state="running",
                session_id=session_id,
                started_at=started_at,
                plan=plan,
                previous=existing,
            )
        except (OSError, RuntimeControlError) as exc:
            for child, _role in reversed(started_children):
                _terminate_owned_child(child)
            for process in missing:
                item = next((value for value in records if value.get("role") == process.role), None)
                if item is not None and _record_process_status(item)[0] == "dead":
                    _remove_role_record(records, process.role)
            _write_runtime_manifest(
                target_manifest,
                config,
                records,
                state="failed",
                session_id=session_id,
                started_at=started_at,
                plan=plan,
                previous=existing,
                error="%s: %s" % (type(exc).__name__, exc),
            )
            raise RuntimeControlError("unable to start process plan: %s" % exc) from exc

        preview["started"] = started_records
        preview["reused"] = reused
        preview["session_id"] = session_id
        return preview


def stop_managed(config: FlexConfig, execute: bool = False) -> dict[str, Any]:
    """Preview or stop only identity-verified processes from the manifest."""

    path = manifest_path(config)
    manifest = _manifest_for_config(config)
    records = list(manifest.get("processes", [])) if manifest else []
    targets, verified, refused, dead = _classify_records(records)
    result: dict[str, Any] = {
        "mode": "execute" if execute else "dry-run",
        "manifest": path,
        "targets": targets,
        "refused": refused,
        "dead": dead,
    }
    if not execute:
        return result

    with _RuntimeMutationLock(config):
        manifest = _manifest_for_config(config)
        records = list(manifest.get("processes", [])) if manifest else []
        targets, verified, refused, dead = _classify_records(records)
        result.update({"targets": targets, "refused": refused, "dead": dead})
        if refused:
            raise RuntimeControlError(
                "refusing to stop any process because manifest ownership could not be verified: "
                + "; ".join(
                    "%s(pid=%s): %s" % (item["role"], item["pid"], item["reason"])
                    for item in refused
                )
            )
        if manifest is None:
            result.update(
                {
                    "stopped": [],
                    "graceful_requested": [],
                    "graceful_stopped": [],
                    "forced": [],
                    "survivors": [],
                    "errors": [],
                }
            )
            return result

        session_id = str(manifest.get("session_id") or uuid.uuid4().hex)
        started_at = str(manifest.get("started_at") or _utc_now())
        _write_runtime_manifest(
            path,
            config,
            records,
            state="stopping",
            session_id=session_id,
            started_at=started_at,
            previous=manifest,
        )
        stop_result = _stop_verified_records(verified, targets)
        result.update(stop_result)
        remaining: list[dict[str, Any]] = []
        for item in records:
            status, _reason = _record_process_status(item)
            if status != "dead":
                remaining.append(dict(item))
        if not remaining and not stop_result["errors"]:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        else:
            _write_runtime_manifest(
                path,
                config,
                remaining,
                state="degraded",
                session_id=session_id,
                started_at=started_at,
                previous=manifest,
                error="; ".join(stop_result["errors"]),
            )
        if stop_result["survivors"]:
            raise RuntimeControlError(
                "managed processes did not exit after graceful and forced shutdown: "
                + ", ".join(
                    "%s(pid=%s)" % (target["role"], target["pid"])
                    for target in stop_result["survivors"]
                )
            )
        if stop_result["errors"]:
            raise RuntimeControlError(
                "some managed processes could not be stopped: "
                + "; ".join(stop_result["errors"])
            )
        return result


def recover_managed(
    plan: ProcessPlan,
    config: FlexConfig,
    *,
    role: str = "ai",
    attempts: int = 1,
    restart_running: bool = False,
    execute: bool = False,
) -> dict[str, Any]:
    """Recover only the AI role; world/render is never restarted implicitly."""

    if role != "ai":
        raise RuntimeControlError("only the ai role supports targeted recovery")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= MAX_RECOVERY_ATTEMPTS:
        raise RuntimeControlError(
            "recovery attempts must be between 1 and %d" % MAX_RECOVERY_ATTEMPTS
        )
    process = next((item for item in plan.processes if item.role == role), None)
    if process is None:
        raise RuntimeControlError(
            "this plan has no separate ai process; refusing to restart the unified world/render role"
        )

    path = manifest_path(config)
    manifest = _manifest_for_config(config)
    records = list(manifest.get("processes", [])) if manifest else []
    current = next((item for item in records if item.get("role") == role), None)
    current_status = _record_process_status(current)[0] if current is not None else "missing"
    preview_by_role = {str(item.get("role")): item for item in records}
    dependency_states = {
        dependency: (
            _record_process_status(preview_by_role[dependency])[0]
            if dependency in preview_by_role
            else "missing"
        )
        for dependency in process.dependencies
    }
    unhealthy_dependencies = sorted(
        dependency for dependency, status in dependency_states.items() if status != "match"
    )
    action = (
        "refuse"
        if unhealthy_dependencies
        else "restart"
        if current_status == "match" and restart_running
        else "reuse"
        if current_status == "match"
        else "refuse"
        if current_status == "refuse"
        else "recover"
    )
    preview: dict[str, Any] = {
        "mode": "execute" if execute else "dry-run",
        "manifest": path,
        "role": role,
        "action": action,
        "attempt_limit": attempts,
        "restart_running": restart_running,
        "current_status": current_status,
        "dependencies": dependency_states,
    }
    if unhealthy_dependencies:
        preview["reason"] = (
            "unhealthy dependency will not be restarted automatically: "
            + ", ".join(unhealthy_dependencies)
        )
    if not execute:
        return preview

    with _RuntimeMutationLock(config):
        manifest = _manifest_for_config(config)
        records = [dict(item) for item in manifest.get("processes", [])] if manifest else []
        by_role = {str(item.get("role")): item for item in records}
        statuses = {
            item_role: _record_process_status(item)
            for item_role, item in by_role.items()
        }
        refused = [
            "%s(pid=%s): %s" % (item_role, by_role[item_role]["pid"], status[1])
            for item_role, status in statuses.items()
            if status[0] == "refuse"
        ]
        if refused:
            raise RuntimeControlError(
                "refusing AI recovery because runtime ownership could not be verified: "
                + "; ".join(refused)
            )
        for dependency in process.dependencies:
            dependency_status = statuses.get(dependency, ("missing", "dependency is absent"))
            if dependency_status[0] != "match":
                raise RuntimeControlError(
                    "%s dependency is not healthy; it will not be restarted automatically"
                    % dependency
                )

        current = by_role.get(role)
        current_state = statuses.get(role, ("missing", ""))[0]
        if current_state == "match" and not restart_running:
            preview["action"] = "reuse"
            preview["reused"] = [dict(current)] if current else []
            preview["attempts_used"] = 0
            return preview

        session_id = str((manifest or {}).get("session_id") or uuid.uuid4().hex)
        started_at = str((manifest or {}).get("started_at") or _utc_now())
        stopped: dict[str, Any] | None = None
        if current_state == "match" and current is not None:
            target = {"role": role, "pid": int(current["pid"])}
            _write_runtime_manifest(
                path,
                config,
                records,
                state="recovering",
                session_id=session_id,
                started_at=started_at,
                plan=plan,
                previous=manifest,
                recovery={"role": role, "phase": "stopping", "attempt_limit": attempts},
            )
            stopped = _stop_verified_records([current], [target])
            if stopped["survivors"] or stopped["errors"]:
                _write_runtime_manifest(
                    path,
                    config,
                    records,
                    state="degraded",
                    session_id=session_id,
                    started_at=started_at,
                    plan=plan,
                    previous=manifest,
                    error="targeted AI shutdown failed",
                    recovery={"role": role, "phase": "failed", "attempt_limit": attempts},
                )
                raise RuntimeControlError(
                    "targeted AI shutdown failed; world/render was left untouched"
                )
        _remove_role_record(records, role)
        _write_runtime_manifest(
            path,
            config,
            records,
            state="recovering",
            session_id=session_id,
            started_at=started_at,
            plan=plan,
            previous=manifest,
            recovery={"role": role, "phase": "starting", "attempt_limit": attempts},
        )

        attempt_errors: list[str] = []
        for attempt in range(1, attempts + 1):
            def persist(record: Mapping[str, Any], attempt_number: int = attempt) -> None:
                _replace_role_record(records, record)
                _write_runtime_manifest(
                    path,
                    config,
                    records,
                    state="recovering",
                    session_id=session_id,
                    started_at=started_at,
                    plan=plan,
                    previous=manifest,
                    recovery={
                        "role": role,
                        "phase": "starting",
                        "attempt": attempt_number,
                        "attempt_limit": attempts,
                    },
                )

            try:
                _child, record = _launch_process(process, config, persist)
            except (OSError, RuntimeControlError) as exc:
                attempt_errors.append("attempt %d: %s" % (attempt, exc))
                failed = next((item for item in records if item.get("role") == role), None)
                if failed is not None:
                    failed_status = _record_process_status(failed)[0]
                    if failed_status == "dead":
                        _remove_role_record(records, role)
                    else:
                        _write_runtime_manifest(
                            path,
                            config,
                            records,
                            state="degraded",
                            session_id=session_id,
                            started_at=started_at,
                            plan=plan,
                            previous=manifest,
                            error=attempt_errors[-1],
                            recovery={
                                "role": role,
                                "phase": "ambiguous",
                                "attempt": attempt,
                                "attempt_limit": attempts,
                            },
                        )
                        raise RuntimeControlError(
                            "AI recovery left an identity-ambiguous process; refusing another attempt"
                        ) from exc
                continue

            _write_runtime_manifest(
                path,
                config,
                records,
                state="running",
                session_id=session_id,
                started_at=started_at,
                plan=plan,
                previous=manifest,
                recovery={
                    "role": role,
                    "phase": "complete",
                    "attempt": attempt,
                    "attempt_limit": attempts,
                },
            )
            preview.update(
                {
                    "action": "restart" if restart_running else "recover",
                    "attempts_used": attempt,
                    "started": [record],
                    "stopped": stopped,
                    "session_id": session_id,
                }
            )
            return preview

        _write_runtime_manifest(
            path,
            config,
            records,
            state="degraded",
            session_id=session_id,
            started_at=started_at,
            plan=plan,
            previous=manifest,
            error="; ".join(attempt_errors),
            recovery={"role": role, "phase": "failed", "attempt_limit": attempts},
        )
        raise RuntimeControlError(
            "AI recovery exhausted %d attempt%s; world/render was left untouched: %s"
            % (attempts, "" if attempts == 1 else "s", "; ".join(attempt_errors))
        )
