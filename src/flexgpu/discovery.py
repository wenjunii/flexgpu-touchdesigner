"""NVIDIA GPU discovery with no third-party Python dependencies."""

from __future__ import annotations

import csv
import os
import re
import shutil
import subprocess
from io import StringIO
from typing import Iterable, Sequence

from .models import DiscoveryError, GPUInfo, GPUSelector


NVIDIA_SMI_FIELDS = (
    "index",
    "uuid",
    "pci.bus_id",
    "name",
    "memory.total",
    "driver_version",
)

_NVIDIA_BUS_RE = re.compile(
    r"^(?P<domain>[0-9a-fA-F]{1,8}):(?P<bus>[0-9a-fA-F]{1,2}):"
    r"(?P<device>[0-9a-fA-F]{1,2})\.(?P<function>[0-9a-fA-F]+)$"
)
_TD_BUS_RE = re.compile(
    r"^(?P<domain>\d+):(?P<bus>\d+):(?P<device>\d+):(?P<function>\d+)$"
)


def _parse_memory_mib(value: str) -> int:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value.replace(",", ""))
    if not match:
        raise ValueError("missing memory value")
    return int(round(float(match.group(0))))


def parse_nvidia_smi_csv(text: str) -> list[GPUInfo]:
    """Parse the exact CSV query emitted by :func:`discover_nvidia_gpus`.

    Header rows, blank lines, unit suffixes, and quoted GPU names are accepted to
    make fixture collection and troubleshooting less fragile.
    """

    rows = csv.reader(StringIO(text), skipinitialspace=True)
    result: list[GPUInfo] = []
    errors: list[str] = []
    for line_number, row in enumerate(rows, 1):
        row = [cell.strip() for cell in row]
        if not row or not any(row):
            continue
        if row[0].lower() in {"index", "gpu index"}:
            continue
        if len(row) < len(NVIDIA_SMI_FIELDS):
            errors.append(
                "line %d: expected %d fields, received %d"
                % (line_number, len(NVIDIA_SMI_FIELDS), len(row))
            )
            continue
        try:
            result.append(
                GPUInfo(
                    index=int(row[0]),
                    uuid=row[1],
                    bus_id=row[2].upper(),
                    name=row[3],
                    memory_total_mib=_parse_memory_mib(row[4]),
                    driver_version=row[5],
                )
            )
        except (TypeError, ValueError) as exc:
            errors.append("line %d: %s" % (line_number, exc))
    if errors:
        raise DiscoveryError("Invalid nvidia-smi output: " + "; ".join(errors))
    return sorted(result, key=lambda gpu: gpu.index)


def find_nvidia_smi() -> str:
    """Locate ``nvidia-smi`` using PATH and NVIDIA's standard Windows path."""

    found = shutil.which("nvidia-smi") or shutil.which("nvidia-smi.exe")
    if found:
        return found
    if os.name == "nt":
        windows = os.environ.get("WINDIR", r"C:\Windows")
        candidate = os.path.join(windows, "System32", "nvidia-smi.exe")
        if os.path.isfile(candidate):
            return candidate
    raise DiscoveryError(
        "nvidia-smi was not found. Install an NVIDIA driver or pass its path explicitly."
    )


def discover_nvidia_gpus(
    nvidia_smi: str | None = None, timeout_seconds: float = 8.0
) -> list[GPUInfo]:
    """Query locally installed NVIDIA GPUs."""

    executable = nvidia_smi or find_nvidia_smi()
    command = [
        executable,
        "--query-gpu=" + ",".join(NVIDIA_SMI_FIELDS),
        "--format=csv,noheader,nounits",
    ]
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DiscoveryError("Unable to run nvidia-smi: %s" % exc) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise DiscoveryError(
            "nvidia-smi failed with exit code %d%s"
            % (completed.returncode, (": " + detail) if detail else "")
        )
    gpus = parse_nvidia_smi_csv(completed.stdout)
    if not gpus:
        raise DiscoveryError("nvidia-smi reported no NVIDIA GPUs")
    return gpus


def _bus_tuple(value: str) -> tuple[int, int, int, int]:
    value = value.strip()
    match = _NVIDIA_BUS_RE.match(value)
    if match:
        return tuple(int(match.group(name), 16) for name in ("domain", "bus", "device", "function"))
    match = _TD_BUS_RE.match(value)
    if match:
        return tuple(int(match.group(name), 10) for name in ("domain", "bus", "device", "function"))
    raise ValueError("unsupported PCI bus ID %r" % value)


def touchdesigner_bus_id(bus_id: str) -> str:
    """Convert NVIDIA's hex PCI notation to TD's numeric bus-ID notation."""

    try:
        return ":".join(str(part) for part in _bus_tuple(bus_id))
    except ValueError:
        # Preserve an unknown value so diagnostics can show the source rather than
        # silently assigning another GPU.
        return bus_id


def resolve_gpu_selector(
    selector: GPUSelector,
    gpus: Sequence[GPUInfo],
    exclude_indices: Iterable[int] = (),
) -> GPUInfo:
    """Resolve one normalized selector against a discovered GPU inventory."""

    excluded = set(exclude_indices)
    available = [gpu for gpu in gpus if gpu.index not in excluded]
    if not available:
        raise DiscoveryError("no eligible NVIDIA GPU remains for this role")
    if selector.kind == "auto":
        return max(available, key=lambda gpu: (gpu.memory_total_mib, -gpu.index))

    matches: list[GPUInfo] = []
    if selector.kind == "index":
        matches = [gpu for gpu in available if gpu.index == int(selector.value)]
    elif selector.kind == "uuid":
        wanted = str(selector.value).strip().lower()
        matches = [gpu for gpu in available if gpu.uuid.lower() == wanted]
    elif selector.kind == "bus_id":
        try:
            wanted_bus = _bus_tuple(str(selector.value))
            matches = [gpu for gpu in available if _bus_tuple(gpu.bus_id) == wanted_bus]
        except ValueError:
            wanted = str(selector.value).strip().lower()
            matches = [gpu for gpu in available if gpu.bus_id.lower() == wanted]
    else:
        raise DiscoveryError("unsupported GPU selector kind %r" % selector.kind)

    if len(matches) != 1:
        raise DiscoveryError(
            "GPU selector %s=%r matched %d eligible devices"
            % (selector.kind, selector.value, len(matches))
        )
    return matches[0]
