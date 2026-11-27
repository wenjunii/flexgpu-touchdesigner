"""Install the opt-in, observer-only Show Control readiness page."""
from __future__ import annotations

import json
from pathlib import Path
import stat

try:
    import runtime_pipeline as rp
    from streamdiffusion_host_runtime import machine_tag
except ImportError:
    from touchdesigner import runtime_pipeline as rp
    from touchdesigner.streamdiffusion_host_runtime import machine_tag


OWNER = "flexgpu-show-readiness/v1"
PAGE = "Readiness"
NODE = "SHOW_READINESS"
PROFILES = {"3080ti_16gb": "3080", "4090": "4090", "5090": "5090"}
FIELDS = (
    ("Rdyoverall", "Show Readiness", "summary"),
    ("Rdynext", "Next action", "next"),
    ("Rdygenerator", "Generator (public flags)", "source"),
    ("Rdygeometry", "Geometry", "geometry"),
    ("Rdywalls", "Active wall TOPs", "wall_sizes"),
    ("Rdyaudio", "Audio routing", "audio"),
    ("Rdyinteraction", "Interaction", "interaction"),
)
CALLBACKS = """def onStart():
    parent().op('readiness_runtime').module.reset()
    _observe(True)
    return

def onFrameEnd(frame):
    _observe(False)
    return

def _observe(force):
    parent().op('readiness_runtime').module.tick(
        parent(), passive=passive, lookup=op, app_seconds=absTime.seconds, force=force)
"""


def _target(root, expected_profile, backup_path, project=None):
    if root is None or root.path != "/project1/flexgpu":
        raise ValueError("Verified FlexGPU root required")
    if expected_profile not in PROFILES:
        raise ValueError("Choose the current machine's explicit profile")
    current = project if project is not None else rp._symbol("project")
    if current is None:
        raise RuntimeError("Open the machine-local working copy in TouchDesigner")
    folder = Path(current.folder).resolve(strict=True)
    name = str(current.name)
    if Path(name).name != name or machine_tag(name) != PROFILES[expected_profile]:
        raise ValueError("Working TOE tag does not match the selected profile")
    source = (folder / name).resolve(strict=True)
    backup = Path(backup_path).resolve(strict=True)
    if (not all(p.parent == folder and p.is_file() and p.stat().st_size >= 1024
                and p.suffix.lower() == ".toe" and machine_tag(p.name) == PROFILES[expected_profile]
                for p in (source, backup)) or source.samefile(backup)):
        raise ValueError("A distinct saved same-folder machine-local rollback TOE is required")
    pipe = root.op("WORKING_PIPELINE")
    show = pipe.op("SHOW_CONTROL") if pipe is not None else None
    if show is None or rp._value(show, "Qualityprofile", "") != expected_profile:
        raise ValueError("Live Show Control profile does not match the working copy")
    return pipe, show


def _owned(root):
    if root is None or root.path != "/project1/flexgpu":
        raise ValueError("Verified FlexGPU root required")
    observer = root.op("WORKING_PIPELINE/" + NODE)
    if observer is None or observer.fetch("managed_owner", None) != OWNER:
        raise RuntimeError("Readiness observer is not installed")
    return observer


def _expression(path, runtime, key):
    # Sample freshness requires this process's embedded module, not only a
    # saved Table DAT. Register time and play dependencies on the readout.
    return "op(%r).module.readout(op(%r), %r, absTime.seconds, op('/local/time').par.play.eval())" % (
        runtime, path, key)


def install(root, *, expected_profile, backup_path):
    """Add only an owned observer and read-only UI; never save or start work."""
    pipe, show = _target(root, expected_profile, backup_path)
    old = pipe.op(NODE)
    owns = old is not None and old.fetch("managed_owner", None) == OWNER
    page = next((p for p in show.customPages if p.name == PAGE), None)
    used = [name for name, _, _ in FIELDS if rp._par(show, name) is not None]
    if ((old is not None and not owns) or (not owns and (page is not None or used))
            or (owns and any(rp._par(show, n).page.name != PAGE for n in used))):
        raise RuntimeError("Readiness operators or parameters are user owned")
    source = Path(__file__).with_name("show_readiness_runtime.py").read_text(encoding="utf-8")
    compile(source, "readiness_runtime", "exec")
    compile(CALLBACKS, "readiness_callbacks", "exec")
    report = rp.BuildReport()
    observer = rp._ensure(pipe, "baseCOMP", NODE, report)
    observer.store("managed_owner", OWNER)
    rp._style(observer, show.nodeX + 340, show.nodeY, (0.2, 0.42, 0.48),
              "Passive show readiness and bounded rehearsal observer", 245, 90)
    callback = rp._ensure(observer, "executeDAT", "readiness_callbacks", report)
    rp._set(callback, "active", False)
    status = rp._table(observer, "STATUS", [
        ("metric", "value"), ("observed_at", "-10000"),
    ] + [(key, "not current") for _, _, key in FIELDS], report)
    dat = rp._text(observer, "readiness_runtime", source, report)
    if report.warnings:
        raise RuntimeError("Readiness setup incomplete: " + "; ".join(report.warnings))
    page = rp._page(show, PAGE)
    for name, label, metric in FIELDS:
        parameter = rp._custom(show, page, "Str", name, "not current", label=label)
        if parameter is None:
            raise RuntimeError("Readiness page is incomplete; do not save a partial installation")
        parameter.readOnly = False
        try:
            if not rp._expr(show, name, _expression(status.path, dat.path, metric)):
                raise RuntimeError("Readiness page is incomplete; do not save a partial installation")
        finally:
            parameter.readOnly = True
    callback.text = CALLBACKS
    for name in ("start", "frameend", "active"):
        if not rp._set(callback, name, True):
            raise RuntimeError("Readiness callback could not be enabled; do not save")
    dat.module.reset()
    result = refresh(root)
    return dict(status="installed", observer=observer.path, fields=len(FIELDS),
                snapshot=result, saved=False, models_started=False)


def refresh(root):
    """Explicit passive sample while the show is paused; no force cooks."""
    observer = _owned(root)
    return observer.op("readiness_runtime").module.tick(
        observer, passive=rp._symbol("passive"), lookup=rp._symbol("op"),
        app_seconds=rp._symbol("absTime").seconds, force=True)


def start_rehearsal(root, seconds=120):
    return _owned(root).op("readiness_runtime").module.start_rehearsal(seconds)


def _report_path(path, repo):
    runtime = repo / "runtime"
    requested = Path(path)
    path = requested if requested.is_absolute() else repo / requested
    resolved = path.resolve()
    root = runtime.resolve()
    if (root != runtime or not resolved.is_relative_to(root) or resolved == root
            or resolved.suffix.lower() != ".json" or resolved.exists()):
        raise ValueError("Use a new JSON report inside this checkout's ignored runtime folder")
    # Refuse any traversed aliases, including a junction resolving elsewhere
    # inside runtime; report output must not use an existing alias path.
    if path.absolute() != resolved:
        raise ValueError("Report path cannot use symlinks, junctions or traversal")
    for parent in (resolved, *resolved.parents):
        if parent.exists():
            flags = getattr(parent.lstat(), "st_file_attributes", 0)
            if parent.is_symlink() or flags & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
                raise ValueError("Report path cannot use a reparse point")
        if parent == runtime:
            break
    return resolved


def save_rehearsal(root, report_path):
    """Finish and write only an explicitly requested ignored report (no media)."""
    path = _report_path(report_path, Path(__file__).resolve().parents[1])
    observer = _owned(root)
    report = observer.op("readiness_runtime").module.finish_rehearsal()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, allow_nan=False)
        handle.write("\n")
    return dict(status="saved", report=str(path), samples=report["sample_count"],
                live_visual_acceptance=False)
