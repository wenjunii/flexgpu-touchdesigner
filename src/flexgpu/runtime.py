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

from .models import (
    FlexConfig,
    ProcessPlan,
    ProcessSpec,
    RuntimeControlError,
    redact_command,
    redact_text,
    sensitive_environment_values,
)


MANIFEST_NAME = "flexgpu-manifest.json"
LOCK_NAME = "flexgpu-runtime.lock"
MANIFEST_VERSION = 6
# Version 3 is the only released legacy manifest format. It may be inspected
# read-only and may be removed by the explicit identity-verified stop path, but
# it is never upgraded in place or reused to launch/recover processes.
LEGACY_READ_ONLY_MANIFEST_VERSIONS = frozenset({3})
SUPPORTED_MANIFEST_VERSIONS = frozenset(
    {MANIFEST_VERSION, *LEGACY_READ_ONLY_MANIFEST_VERSIONS}
)
HEARTBEAT_VERSION = 1
TOUCHDESIGNER_BUILD_VERSION = "1.2.1"
DEFAULT_HEARTBEAT_TIMEOUT_MS = 5000
MAX_RECOVERY_ATTEMPTS = 3
GRACEFUL_STOP_SECONDS = 8.0
FORCED_STOP_SECONDS = 4.0
_UNWRITTEN_LOCK_GRACE_SECONDS = 30.0
_LOCAL_CHILDREN: dict[int, subprocess.Popen[Any]] = {}
_RUNTIME_ENV_KEYS = {
    "CUDA_DEVICE_ORDER",
    "CUDA_VISIBLE_DEVICES",
    "FLEXGPU_CONFIG",
    "FLEXGPU_ROLE",
    "FLEXGPU_TOPOLOGY",
    "FLEXGPU_EXPERIENCE",
    "FLEXGPU_COMPLETION",
    "FLEXGPU_TIER",
    "FLEXGPU_GPU_INDEX",
    "FLEXGPU_GPU_UUID",
    "FLEXGPU_GPU_BUS_ID",
    "FLEXGPU_TD_BUS_ID",
    "FLEXGPU_DIFFUSION_RESOLUTION",
    "FLEXGPU_DIFFUSION_HZ",
    "FLEXGPU_GEOMETRY_RESOLUTION",
    "FLEXGPU_GEOMETRY_HZ",
    "FLEXGPU_MAX_POINTS",
    "FLEXGPU_VR_REFRESH_HZ",
}
_EPHEMERAL_RUNTIME_ENV_KEYS = {
    "FLEXGPU_SESSION_ID",
    "FLEXGPU_HEARTBEAT_PATH",
    "FLEXGPU_HEARTBEAT_TIMEOUT_MS",
    "FLEXGPU_EXPECTED_BUILD_VERSION",
    "FLEXGPU_CONFIG_ID",
}


def _process_sensitive_values(process: ProcessSpec) -> tuple[str, ...]:
    return sensitive_environment_values(process.env) + sensitive_environment_values(os.environ)


def _environment_fingerprint(environment: Mapping[str, str]) -> str:
    """Hash launch settings without persisting possible secret values."""

    payload = json.dumps(
        sorted((str(key), str(value)) for key, value in environment.items()),
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _effective_launch_environment(process: ProcessSpec) -> dict[str, str]:
    """Return inherited plus planned env, excluding per-session launcher values."""

    environment = {str(key): str(value) for key, value in os.environ.items()}
    environment.update({str(key): str(value) for key, value in process.env.items()})
    for key in _EPHEMERAL_RUNTIME_ENV_KEYS:
        environment.pop(key, None)
    return environment


def _effective_environment_fingerprint(process: ProcessSpec) -> str:
    """Fingerprint the environment the child would really inherit."""

    return _environment_fingerprint(_effective_launch_environment(process))


def _config_identity(config: FlexConfig) -> str:
    """Hash the validated semantic config without persisting its contents."""

    payload = json.dumps(
        config.raw,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _valid_config_identity(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _assert_manifest_config_identity_for_reuse(
    manifest: Mapping[str, Any], config: FlexConfig
) -> None:
    """Fail closed unless an active manifest proves the same semantic config."""

    recorded = manifest.get("config_sha256")
    if not _valid_config_identity(recorded):
        raise RuntimeControlError(
            "active runtime manifest has no valid semantic config identity; "
            "stop it before upgrading or reusing its processes"
        )
    if recorded != _config_identity(config):
        raise RuntimeControlError(
            "active runtime manifest semantic config differs from the current config; "
            "stop it before applying config changes"
        )


def _command_fingerprint(command: Sequence[str]) -> str:
    payload = json.dumps([str(item) for item in command], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _path_fingerprint(path: str) -> str:
    return hashlib.sha256(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()


def _supervisor_settings(config: FlexConfig) -> tuple[int, int, bool]:
    settings = config.supervisor if isinstance(config.supervisor, Mapping) else {}
    heartbeat_timeout = settings.get("heartbeat_timeout_ms", DEFAULT_HEARTBEAT_TIMEOUT_MS)
    readiness_timeout = settings.get("readiness_timeout_ms", 0)
    require_ready = settings.get("require_ready", False)
    if type(heartbeat_timeout) is not int or not 250 <= heartbeat_timeout <= 600000:
        heartbeat_timeout = DEFAULT_HEARTBEAT_TIMEOUT_MS
    if type(readiness_timeout) is not int or not 0 <= readiness_timeout <= 600000:
        readiness_timeout = 0
    if not isinstance(require_ready, bool):
        require_ready = False
    if require_ready and readiness_timeout == 0:
        readiness_timeout = heartbeat_timeout
    return heartbeat_timeout, readiness_timeout, require_ready


def _readiness_wait_ms(config: FlexConfig, override: int | None) -> int:
    _heartbeat_timeout, configured, required = _supervisor_settings(config)
    if override is None:
        return configured
    if isinstance(override, bool) or not isinstance(override, int) or not 0 <= override <= 600000:
        raise RuntimeControlError("readiness wait must be between 0 and 600000 milliseconds")
    if required and override == 0:
        raise RuntimeControlError("readiness wait cannot be zero when supervisor.require_ready is true")
    return override


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


def _is_network_runtime_path(path: str) -> bool:
    """Return true for UNC paths and Windows drives mapped to remote storage."""

    normalized = os.path.normpath(str(path)).replace("/", "\\")
    if normalized.startswith("\\\\"):
        return True
    if os.name != "nt":
        return False
    absolute = os.path.abspath(path)
    drive = os.path.splitdrive(absolute)[0]
    if not drive:
        return False
    root = drive if drive.endswith(("\\", "/")) else drive + "\\"
    try:
        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
        get_drive_type.argtypes = [ctypes.c_wchar_p]
        get_drive_type.restype = ctypes.c_uint
        return int(get_drive_type(root)) == 4  # DRIVE_REMOTE
    except (AttributeError, OSError, ValueError):
        return False


def runtime_directory(config: FlexConfig) -> str:
    path = os.path.expandvars(os.path.expanduser(config.runtime_dir))
    if _is_network_runtime_path(path):
        raise RuntimeControlError(
            "runtime_dir must use local storage; network and UNC paths are unsafe"
        )
    if not os.path.isabs(path):
        path = os.path.join(config.base_dir, path)
    path = os.path.abspath(path)
    if _is_network_runtime_path(path):
        raise RuntimeControlError(
            "runtime_dir must use local storage; network and UNC paths are unsafe"
        )
    drive, tail = os.path.splitdrive(path)
    if path == os.path.abspath(os.path.sep) or (drive and tail in {"\\", "/"}):
        raise RuntimeControlError("runtime_dir must not be a filesystem root")
    if os.path.lexists(path):
        if _is_link_or_reparse(path):
            raise RuntimeControlError("runtime_dir must not be a symlink or reparse point")
        if not os.path.isdir(path):
            raise RuntimeControlError("runtime_dir exists but is not a directory")
        if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            raise RuntimeControlError("runtime_dir is not accessible to the current user")
    return path


def _is_link_or_reparse(path: str) -> bool:
    if os.path.islink(path):
        return True
    try:
        attributes = getattr(os.stat(path, follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _ensure_runtime_directory(config: FlexConfig) -> str:
    path = runtime_directory(config)
    os.makedirs(path, mode=0o700, exist_ok=True)
    # Recheck after creation to catch a concurrently substituted junction.
    if _is_link_or_reparse(path) or not os.path.isdir(path):
        raise RuntimeControlError("runtime_dir changed during creation")
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            raise RuntimeControlError("unable to secure runtime_dir: %s" % exc) from exc
    return path


def _assert_safe_runtime_file(path: str, runtime_dir: str) -> None:
    if os.path.normcase(os.path.dirname(os.path.abspath(path))) != os.path.normcase(
        os.path.abspath(runtime_dir)
    ):
        raise RuntimeControlError("runtime file escapes runtime_dir")
    if os.path.lexists(path) and _is_link_or_reparse(path):
        raise RuntimeControlError("runtime file must not be a symlink or reparse point")


def _open_runtime_text(path: str, mode: str, runtime_dir: str):
    _assert_safe_runtime_file(path, runtime_dir)
    flags = os.O_RDONLY if mode == "r" else os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    return os.fdopen(descriptor, mode, encoding="utf-8", buffering=1)


def manifest_path(config: FlexConfig) -> str:
    return os.path.join(runtime_directory(config), MANIFEST_NAME)


def lock_path(config: FlexConfig) -> str:
    return os.path.join(runtime_directory(config), LOCK_NAME)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_lock_record(path: str) -> dict[str, Any] | None:
    try:
        with _open_runtime_text(path, "r", os.path.dirname(path)) as handle:
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
        self.config = config
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
        _ensure_runtime_directory(self.config)
        _assert_safe_runtime_file(self.path, os.path.dirname(self.path))
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
        with _open_runtime_text(path, "r", os.path.dirname(path)) as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RuntimeControlError("unable to read runtime manifest %s: %s" % (path, exc)) from exc
    if not isinstance(data, dict) or not isinstance(data.get("processes"), list):
        raise RuntimeControlError("runtime manifest is malformed: %s" % path)
    version = data.get("version")
    if type(version) is not int:
        raise RuntimeControlError(
            "runtime manifest version is missing or not an integer: %s" % path
        )
    if version > MANIFEST_VERSION:
        raise RuntimeControlError(
            "runtime manifest version %d is newer than supported version %d: %s"
            % (version, MANIFEST_VERSION, path)
        )
    if version not in SUPPORTED_MANIFEST_VERSIONS:
        raise RuntimeControlError(
            "runtime manifest version %d is unsupported; supported versions are %s: %s"
            % (
                version,
                ", ".join(str(item) for item in sorted(SUPPORTED_MANIFEST_VERSIONS)),
                path,
            )
        )
    if "config_sha256" in data and not _valid_config_identity(
        data.get("config_sha256")
    ):
        raise RuntimeControlError(
            "runtime manifest has an invalid semantic config identity: %s" % path
        )
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
    recorded_executable = recorded["executable"]
    if (
        len(recorded_executable) > 32768
        or any(ord(character) < 32 or ord(character) == 127 for character in recorded_executable)
    ):
        return "refuse", "manifest process executable path is invalid"
    if recorded["creation_token"] != current["creation_token"]:
        return "refuse", "PID creation time no longer matches"
    try:
        recorded_path = os.path.normcase(os.path.realpath(recorded_executable))
        current_path = os.path.normcase(os.path.realpath(current["executable"]))
    except (OSError, TypeError, ValueError):
        return "refuse", "manifest process executable path is invalid"
    if recorded_path != current_path:
        return "refuse", "process executable no longer matches"
    if recorded["command_line_sha256"] != current["command_line_sha256"]:
        return "refuse", "process command line no longer matches"
    return "match", ""


def _atomic_manifest_write(path: str, data: Mapping[str, Any]) -> None:
    directory = os.path.dirname(path)
    if not os.path.isdir(directory) or _is_link_or_reparse(directory):
        raise RuntimeControlError("runtime directory is missing or unsafe")
    _assert_safe_runtime_file(path, directory)
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, delete=False, suffix=".tmp"
        ) as handle:
            temporary = handle.name
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _assert_manifest_owner_path(
    manifest: Mapping[str, Any], config: FlexConfig
) -> None:
    recorded_config = manifest.get("config")
    source_path = config.source_path
    if (
        not isinstance(recorded_config, str)
        or not recorded_config
        or recorded_config != recorded_config.strip()
    ):
        raise RuntimeControlError(
            "runtime manifest has no valid config owner path"
        )
    if not isinstance(source_path, str) or not source_path:
        raise RuntimeControlError("current config has no valid source path")
    if os.path.normcase(os.path.abspath(recorded_config)) != os.path.normcase(
        os.path.abspath(source_path)
    ):
        raise RuntimeControlError(
            "runtime manifest belongs to a different config: %s" % recorded_config
        )


def _assert_manifest_access(
    manifest: Mapping[str, Any], config: FlexConfig, access: str
) -> None:
    """Validate trust required for read, mutation/reuse, or explicit stop."""

    _assert_manifest_owner_path(manifest, config)
    version = manifest["version"]
    recorded_identity = manifest.get("config_sha256")
    if access == "read":
        # Released v3 documents predate the required semantic digest and are
        # intentionally inspectable only. Current documents must always carry
        # the identity that all current writers emit.
        if version == MANIFEST_VERSION and not _valid_config_identity(
            recorded_identity
        ):
            raise RuntimeControlError(
                "current runtime manifest has no valid semantic config identity"
            )
        return
    if access == "mutate":
        if version != MANIFEST_VERSION:
            raise RuntimeControlError(
                "legacy runtime manifest version %d is read-only; stop it with "
                "the explicit legacy stop policy before starting or recovering"
                % version
            )
        if not _valid_config_identity(recorded_identity):
            raise RuntimeControlError(
                "runtime manifest has no valid semantic config identity"
            )
        if recorded_identity != _config_identity(config):
            raise RuntimeControlError(
                "runtime manifest semantic config differs from the current config; "
                "stop it before applying config changes"
            )
        return
    if access == "stop":
        if version not in SUPPORTED_MANIFEST_VERSIONS:
            raise RuntimeControlError(
                "runtime manifest version %s cannot be stopped safely" % version
            )
        if version == MANIFEST_VERSION and not _valid_config_identity(
            recorded_identity
        ):
            raise RuntimeControlError(
                "runtime manifest has no valid semantic config identity; "
                "refusing to signal any recorded process"
            )
        # Released v3 documents did not contain config_sha256. Their exact
        # owner path plus the retained per-process kernel identity is the
        # explicit legacy stop authority. The stop path below never rewrites
        # or upgrades the v3 document.
        return
    raise RuntimeControlError("unknown runtime manifest access policy: %s" % access)


def _manifest_for_config(
    config: FlexConfig, *, access: str = "read"
) -> dict[str, Any] | None:
    path = manifest_path(config)
    manifest = _read_manifest(path)
    if manifest is not None:
        _assert_manifest_access(manifest, config, access)
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
    owner_path = config.source_path
    if (
        not isinstance(owner_path, str)
        or not owner_path
        or owner_path != owner_path.strip()
    ):
        raise RuntimeControlError(
            "refusing to write a runtime manifest without a valid config owner path"
        )
    # A start/recovery plan establishes the effective semantic identity. During
    # stop, preserve the identity already bound to surviving processes instead
    # of relabeling them with a config file that may have changed in place.
    config_sha256 = (
        _config_identity(config) if plan is not None else prior.get("config_sha256")
    )
    if not _valid_config_identity(config_sha256):
        raise RuntimeControlError(
            "refusing to write a runtime manifest without a valid semantic config identity"
        )
    document: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "session_id": session_id,
        "state": state,
        "started_at": started_at,
        "updated_at": _utc_now(),
        "config": owner_path,
        "config_sha256": config_sha256,
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


def _sanitize_process_record(
    record: Mapping[str, Any], process: ProcessSpec
) -> dict[str, Any]:
    """Drop unknown legacy fields and ensure no real launch values are emitted."""

    allowed = {
        "role",
        "pid",
        "log",
        "gpu_uuid",
        "environment_sha256",
        "launch_state",
        "started_at",
        "identity",
        "heartbeat",
    }
    result = {key: value for key, value in record.items() if key in allowed}
    secrets = _process_sensitive_values(process)
    result["command"] = redact_command(process.command, secrets)
    result["command_sha256"] = _command_fingerprint(process.command)
    result["cwd"] = redact_text(process.cwd, secrets)
    result["cwd_sha256"] = _path_fingerprint(process.cwd)
    result["environment_sha256"] = _effective_environment_fingerprint(process)
    return result


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


def _heartbeat_path(config: FlexConfig, role: str, session_id: str) -> str:
    safe_role = "".join(character for character in role if character.isalnum() or character in "_-")
    if safe_role != role or not safe_role:
        raise RuntimeControlError("process role is unsafe for heartbeat naming")
    return os.path.join(
        runtime_directory(config),
        "flexgpu-heartbeat-%s-%s.json" % (safe_role, session_id[:16]),
    )


def _launch_environment(
    process: ProcessSpec,
    config: FlexConfig,
    session_id: str,
    heartbeat_path: str,
    heartbeat_timeout_ms: int,
) -> dict[str, str]:
    """Build the real child environment after verifying launcher-owned values."""

    for key in process.env:
        upper = str(key).upper()
        if upper.startswith(("CUDA_", "FLEXGPU_")) and (
            str(key) != upper or upper not in _RUNTIME_ENV_KEYS
        ):
            raise RuntimeControlError(
                "process environment contains a non-launcher runtime variable: %s" % key
            )
    gpu = process.gpu
    expected = {
        "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
        "CUDA_VISIBLE_DEVICES": (gpu.uuid or str(gpu.index)) if gpu else "",
        "FLEXGPU_CONFIG": config.source_path,
        "FLEXGPU_ROLE": process.role,
        "FLEXGPU_TOPOLOGY": config.topology,
        "FLEXGPU_GPU_INDEX": str(gpu.index) if gpu else "",
        "FLEXGPU_GPU_UUID": gpu.uuid if gpu else "",
        "FLEXGPU_GPU_BUS_ID": gpu.bus_id if gpu else "",
        "FLEXGPU_TD_BUS_ID": gpu.td_bus_id if gpu else "",
    }
    for key, expected_value in expected.items():
        if process.env.get(key) != expected_value:
            raise RuntimeControlError(
                "launcher-owned %s does not match the resolved process identity" % key
            )
    environment = _effective_launch_environment(process)
    environment.update(
        {
            "FLEXGPU_SESSION_ID": session_id,
            "FLEXGPU_HEARTBEAT_PATH": heartbeat_path,
            "FLEXGPU_HEARTBEAT_TIMEOUT_MS": str(heartbeat_timeout_ms),
        }
    )
    if process.project_path:
        environment.update(
            {
                "FLEXGPU_EXPECTED_BUILD_VERSION": TOUCHDESIGNER_BUILD_VERSION,
                "FLEXGPU_CONFIG_ID": _config_identity(config),
            }
        )
    return environment


def _read_heartbeat(path: str, runtime_dir: str) -> dict[str, Any] | None:
    _assert_safe_runtime_file(path, runtime_dir)
    if not os.path.exists(path):
        return None
    try:
        if os.path.getsize(path) > 65536:
            raise ValueError("heartbeat exceeds 64 KiB")
        with _open_runtime_text(path, "r", runtime_dir) as handle:
            payload = json.load(handle)
    except (OSError, ValueError, RuntimeControlError) as exc:
        raise RuntimeControlError("application heartbeat is malformed") from exc
    if not isinstance(payload, dict):
        raise RuntimeControlError("application heartbeat is malformed")
    return payload


def _parse_heartbeat_time(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("missing updated_at")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("updated_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _heartbeat_status(
    item: Mapping[str, Any], manifest_session_id: str
) -> tuple[str, str, dict[str, Any]]:
    """Return alive, ready, or stale for an identity-matched process."""

    metadata = item.get("heartbeat")
    if not isinstance(metadata, Mapping):
        return "alive", "application heartbeat is not enabled for this record", {}
    path = metadata.get("path")
    timeout_ms = metadata.get("timeout_ms", DEFAULT_HEARTBEAT_TIMEOUT_MS)
    required = bool(metadata.get("required", False))
    expected_build = metadata.get("expected_build_version", "")
    expected_config_id = metadata.get("expected_config_id", "")
    log_path = item.get("log")
    expected_name = "flexgpu-heartbeat-%s-%s.json" % (
        str(item.get("role", "")),
        manifest_session_id[:16],
    )
    if (
        not isinstance(path, str)
        or not path
        or not isinstance(log_path, str)
        or os.path.normcase(os.path.dirname(os.path.abspath(path)))
        != os.path.normcase(os.path.dirname(os.path.abspath(log_path)))
        or os.path.basename(path) != expected_name
        or type(timeout_ms) is not int
        or not 250 <= timeout_ms <= 600000
        or not isinstance(expected_build, str)
        or len(expected_build) > 64
        or not isinstance(expected_config_id, str)
        or (
            expected_config_id
            and (
                len(expected_config_id) != 64
                or any(character not in "0123456789abcdef" for character in expected_config_id)
            )
        )
    ):
        return "stale", "heartbeat metadata is malformed", {}
    runtime_dir = os.path.dirname(path)
    try:
        payload = _read_heartbeat(path, runtime_dir)
    except RuntimeControlError:
        return "stale", "application heartbeat is malformed", {}
    if payload is None:
        if required:
            return "stale", "application heartbeat has not been published", {}
        try:
            started = _parse_heartbeat_time(item.get("started_at"))
            startup_age_ms = (
                datetime.now(timezone.utc) - started
            ).total_seconds() * 1000.0
        except (TypeError, ValueError, OverflowError):
            return "stale", "application heartbeat has not been published", {}
        details = {"startup_age_ms": round(max(0.0, startup_age_ms), 1)}
        if startup_age_ms > timeout_ms:
            return "stale", "application heartbeat was not published before timeout", details
        return "alive", "application heartbeat has not been published", details
    try:
        if payload.get("version") != HEARTBEAT_VERSION:
            raise ValueError("version")
        if payload.get("session_id") != manifest_session_id:
            raise ValueError("session")
        if payload.get("role") != item.get("role"):
            raise ValueError("role")
        if payload.get("pid") != item.get("pid"):
            raise ValueError("pid")
        updated = _parse_heartbeat_time(payload.get("updated_at"))
        age_ms = (datetime.now(timezone.utc) - updated).total_seconds() * 1000.0
        if age_ms < -30000:
            raise ValueError("future timestamp")
    except (TypeError, ValueError, OverflowError):
        return "stale", "application heartbeat identity is malformed", {}
    details = {
        "heartbeat_age_ms": round(max(0.0, age_ms), 1),
        "application_state": payload.get("state"),
    }
    if age_ms > timeout_ms:
        return "stale", "application heartbeat is stale", details
    if expected_build:
        build = payload.get("build")
        actual_build = build.get("version") if isinstance(build, Mapping) else None
        if actual_build != expected_build:
            details["expected_build_version"] = expected_build
            details["actual_build_version"] = actual_build
            return "stale", "application build identity does not match the launch plan", details
    if expected_config_id:
        config_identity = payload.get("config")
        actual_config_id = (
            config_identity.get("identity")
            if isinstance(config_identity, Mapping)
            else None
        )
        if actual_config_id != expected_config_id:
            return "stale", "application config identity does not match the launch plan", details
    if payload.get("state") == "ready":
        return "ready", "", details
    return "alive", "application has not reported ready", details


def _wait_for_ready(
    item: Mapping[str, Any], session_id: str, timeout_ms: int
) -> None:
    if timeout_ms <= 0:
        return
    deadline = time.monotonic() + timeout_ms / 1000.0
    last_reason = "application heartbeat has not been published"
    while True:
        process_state, process_reason = _record_process_status(item)
        if process_state != "match":
            raise RuntimeControlError(
                "process exited or changed identity before readiness: %s" % process_reason
            )
        heartbeat_state, heartbeat_reason, _details = _heartbeat_status(item, session_id)
        if heartbeat_state == "ready":
            return
        last_reason = heartbeat_reason or heartbeat_state
        if time.monotonic() >= deadline:
            raise RuntimeControlError(
                "application readiness timed out after %d ms: %s" % (timeout_ms, last_reason)
            )
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _launch_process(
    process: ProcessSpec,
    config: FlexConfig,
    persist: Callable[[Mapping[str, Any]], None],
    *,
    session_id: str,
    wait_ready_ms: int = 0,
) -> tuple[subprocess.Popen[Any], dict[str, Any]]:
    runtime_dir = _ensure_runtime_directory(config)
    log_path = os.path.join(runtime_dir, "%s.log" % process.role)
    log_handle = _open_runtime_text(log_path, "a", runtime_dir)
    heartbeat_timeout_ms, _configured_wait, config_requires_ready = _supervisor_settings(config)
    heartbeat_path = _heartbeat_path(config, process.role, session_id)
    _assert_safe_runtime_file(heartbeat_path, runtime_dir)
    try:
        os.unlink(heartbeat_path)
    except FileNotFoundError:
        pass
    env = _launch_environment(
        process, config, session_id, heartbeat_path, heartbeat_timeout_ms
    )
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
    except OSError as exc:
        log_handle.close()
        secrets = _process_sensitive_values(process)
        raise RuntimeControlError(
            "unable to launch %s process: %s"
            % (process.role, redact_text(exc, secrets))
        ) from exc

    secrets = _process_sensitive_values(process)
    record: dict[str, Any] = {
        "role": process.role,
        "pid": child.pid,
        "command": redact_command(process.command, secrets),
        "command_sha256": _command_fingerprint(process.command),
        "cwd": redact_text(process.cwd, secrets),
        "cwd_sha256": _path_fingerprint(process.cwd),
        "log": log_path,
        "gpu_uuid": process.gpu.uuid if process.gpu else "",
        "environment_sha256": _effective_environment_fingerprint(process),
        "launch_state": "identity_pending",
        "started_at": _utc_now(),
        "heartbeat": {
            "version": HEARTBEAT_VERSION,
            "path": heartbeat_path,
            "timeout_ms": heartbeat_timeout_ms,
            "required": bool(config_requires_ready or wait_ready_ms > 0),
            "expected_build_version": (
                TOUCHDESIGNER_BUILD_VERSION if process.project_path else ""
            ),
            "expected_config_id": (
                _config_identity(config) if process.project_path else ""
            ),
        },
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
        _wait_for_ready(record, session_id, wait_ready_ms)
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
    counts = {
        "running": 0,
        "ready": 0,
        "alive": 0,
        "stale": 0,
        "dead": 0,
        "refused": 0,
    }
    session_id = str(manifest.get("session_id") or "")
    for item in manifest.get("processes", []):
        status, reason = _record_process_status(item)
        details: dict[str, Any] = {}
        if status == "match":
            counts["running"] += 1
            public_status, heartbeat_reason, details = _heartbeat_status(item, session_id)
            reason = heartbeat_reason
        else:
            public_status = "refused" if status == "refuse" else status
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
        entry.update(details)
        processes.append(entry)

    manifest_state = str(manifest.get("state", "running" if processes else "stopped"))
    if counts["refused"]:
        state = "ownership_error"
    elif counts["dead"]:
        state = "degraded"
    elif counts["stale"]:
        state = "stale"
    elif manifest_state in {"starting", "recovering", "stopping"}:
        state = manifest_state
    elif manifest_state in {"failed", "degraded"}:
        state = "degraded"
    elif counts["ready"] and not counts["alive"]:
        state = "ready"
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
    plan: ProcessPlan,
    config: FlexConfig,
    execute: bool = False,
    *,
    wait_ready_ms: int | None = None,
) -> dict[str, Any]:
    """Preview or start a plan; mutation is impossible unless execute is true."""

    target_manifest = manifest_path(config)
    readiness_wait_ms = _readiness_wait_ms(config, wait_ready_ms)
    preview: dict[str, Any] = {
        "mode": "execute" if execute else "dry-run",
        "manifest": target_manifest,
        "processes": [
            {
                "role": process.role,
                "command": redact_command(
                    process.command, _process_sensitive_values(process)
                ),
                "cwd": redact_text(
                    process.cwd, _process_sensitive_values(process)
                ),
            }
            for process in plan.processes
        ],
        "wait_ready_ms": readiness_wait_ms,
    }
    if not execute:
        return preview

    with _RuntimeMutationLock(config):
        existing = _manifest_for_config(config, access="mutate")
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
            if active_by_role:
                _assert_manifest_config_identity_for_reuse(existing, config)
            planned_roles = {process.role for process in plan.processes}
            unexpected = sorted(set(active_by_role).difference(planned_roles))
            if unexpected:
                raise RuntimeControlError(
                    "runtime manifest owns active roles outside this plan: "
                    + ", ".join(unexpected)
                )
            for process in plan.processes:
                current = active_by_role.get(process.role)
                command_matches = bool(
                    current
                    and (
                        current.get("command_sha256") == _command_fingerprint(process.command)
                        or (
                            not current.get("command_sha256")
                            and list(process.command) == current.get("command")
                        )
                    )
                )
                if current and not command_matches:
                    raise RuntimeControlError(
                        "active %s process command differs from the new plan; stop it before restarting"
                        % process.role
                    )
                environment_hash = current.get("environment_sha256") if current else None
                environment_matches = bool(
                    current
                    and environment_hash == _effective_environment_fingerprint(process)
                )
                if current and not environment_matches:
                    raise RuntimeControlError(
                        "active %s process environment differs from the new plan; stop it before applying overrides"
                        % process.role
                    )
                cwd_matches = bool(
                    current
                    and (
                        current.get("cwd_sha256") == _path_fingerprint(process.cwd)
                        or (
                            not current.get("cwd_sha256")
                            and os.path.normcase(os.path.abspath(str(current.get("cwd", ""))))
                            == os.path.normcase(os.path.abspath(process.cwd))
                        )
                    )
                )
                if current and not cwd_matches:
                    raise RuntimeControlError(
                        "active %s process working directory differs from the new plan; stop it before restarting"
                        % process.role
                    )

        reused = [dict(active_by_role[p.role]) for p in plan.processes if p.role in active_by_role]
        missing = [process for process in plan.processes if process.role not in active_by_role]
        session_id = str((existing or {}).get("session_id") or uuid.uuid4().hex)
        for record in reused:
            process = next(item for item in plan.processes if item.role == record.get("role"))
            replacement = _sanitize_process_record(record, process)
            record.clear()
            record.update(replacement)
            _wait_for_ready(record, session_id, readiness_wait_ms)
        if not missing:
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

        _ensure_runtime_directory(config)
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
                child, record = _launch_process(
                    process,
                    config,
                    persist,
                    session_id=session_id,
                    wait_ready_ms=readiness_wait_ms,
                )
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
        manifest = _manifest_for_config(config, access="stop")
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

        legacy_manifest = manifest["version"] != MANIFEST_VERSION
        result["legacy_manifest"] = legacy_manifest
        session_id = str(manifest.get("session_id") or uuid.uuid4().hex)
        started_at = str(manifest.get("started_at") or _utc_now())
        if not legacy_manifest:
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
        elif not legacy_manifest:
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
        else:
            # Never relabel a released legacy document as current. If the
            # identity-verified stop is incomplete, retain the original
            # manifest for operator inspection and fail below.
            result["legacy_manifest_retained"] = True
        if legacy_manifest and remaining and not (
            stop_result["survivors"] or stop_result["errors"]
        ):
            raise RuntimeControlError(
                "legacy manifest stop could not prove that every process exited; "
                "the original manifest was retained"
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
    wait_ready_ms: int | None = None,
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

    readiness_wait_ms = _readiness_wait_ms(config, wait_ready_ms)
    path = manifest_path(config)
    manifest = _manifest_for_config(config)
    preview_session_id = str((manifest or {}).get("session_id") or "")
    records = list(manifest.get("processes", [])) if manifest else []
    current = next((item for item in records if item.get("role") == role), None)
    current_status = _record_process_status(current)[0] if current is not None else "missing"
    current_readiness = (
        _heartbeat_status(current, preview_session_id)[0]
        if current is not None and current_status == "match"
        else current_status
    )
    preview_by_role = {str(item.get("role")): item for item in records}
    dependency_states = {
        dependency: (
            _record_process_status(preview_by_role[dependency])[0]
            if dependency in preview_by_role
            else "missing"
        )
        for dependency in process.dependencies
    }
    dependency_readiness = {
        dependency: (
            _heartbeat_status(preview_by_role[dependency], preview_session_id)[0]
            if dependency_states[dependency] == "match"
            else dependency_states[dependency]
        )
        for dependency in process.dependencies
    }
    unhealthy_dependencies = sorted(
        dependency
        for dependency, status in dependency_states.items()
        if status != "match"
        or (readiness_wait_ms > 0 and dependency_readiness[dependency] != "ready")
    )
    action = (
        "refuse"
        if unhealthy_dependencies
        else "restart"
        if current_status == "match"
        and (restart_running or (readiness_wait_ms > 0 and current_readiness != "ready"))
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
        "readiness_status": current_readiness,
        "dependencies": dependency_states,
        "dependency_readiness": dependency_readiness,
        "wait_ready_ms": readiness_wait_ms,
    }
    if unhealthy_dependencies:
        preview["reason"] = (
            "unhealthy dependency will not be restarted automatically: "
            + ", ".join(unhealthy_dependencies)
        )
    if not execute:
        return preview

    with _RuntimeMutationLock(config):
        manifest = _manifest_for_config(config, access="mutate")
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
        if manifest and any(status[0] == "match" for status in statuses.values()):
            _assert_manifest_config_identity_for_reuse(manifest, config)
        for dependency in process.dependencies:
            dependency_status = statuses.get(dependency, ("missing", "dependency is absent"))
            if dependency_status[0] != "match":
                raise RuntimeControlError(
                    "%s dependency is not healthy; it will not be restarted automatically"
                    % dependency
                )
            if readiness_wait_ms > 0:
                dependency_readiness_state = _heartbeat_status(
                    by_role[dependency],
                    str((manifest or {}).get("session_id") or ""),
                )[0]
                if dependency_readiness_state != "ready":
                    raise RuntimeControlError(
                        "%s dependency is alive but not application-ready; it will not be restarted automatically"
                        % dependency
                    )

        current = by_role.get(role)
        current_state = statuses.get(role, ("missing", ""))[0]
        current_is_ready = bool(
            current is not None
            and current_state == "match"
            and _heartbeat_status(
                current, str((manifest or {}).get("session_id") or "")
            )[0]
            == "ready"
        )
        if current_state == "match" and not restart_running and (
            readiness_wait_ms == 0 or current_is_ready
        ):
            preview["action"] = "reuse"
            preview["reused"] = (
                [_sanitize_process_record(current, process)] if current else []
            )
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
                _child, record = _launch_process(
                    process,
                    config,
                    persist,
                    session_id=session_id,
                    wait_ready_ms=readiness_wait_ms,
                )
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
