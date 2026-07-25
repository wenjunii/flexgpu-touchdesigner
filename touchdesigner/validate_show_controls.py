"""Reversible live validation for the public FlexGPU SHOW_CONTROL component.

Run this inside TouchDesigner after the tracked runtime has been installed.
The validator changes one public parameter at a time, verifies its managed
target, and restores every original value in a ``finally`` block.  Worker
start/stop pulses are intentionally tested by the external PowerShell release
gate because waiting for a worker inside TouchDesigner's main thread would
prevent bridge callbacks from cooking.
"""

from __future__ import print_function

import json
import math
import time
from pathlib import Path


ROOT_PATH = "/project1/flexgpu"
PIPELINE_PATH = ROOT_PATH + "/WORKING_PIPELINE"
CONTROL_PATH = PIPELINE_PATH + "/SHOW_CONTROL"
REPORT_VERSION = "flexgpu-show-controls-validation/v1"


VALUE_CONTROLS = (
    "Geometryprovider",
    "Displaymode",
    "Completionmode",
    "Fogdensity",
    "Interactionstrength",
    "Interactionsmoothing",
    "Wrapyawdegrees",
    "Wrapfovdegrees",
    "Wrapcoverage",
    "Wrapnoise",
    "Wallwidth",
    "Wallheight",
    "Pointcloudscale",
    "Moge2scale",
    "Depthanythingscale",
    "Surfacefovdegrees",
    "Artisticyawdegrees",
    "Artisticoffsetmetres",
    "Artisticoffsetdirection",
    "Leftwallscale",
    "Leftwallpanhorizontaldegrees",
    "Leftwallpanverticaldegrees",
    "Centerwallscale",
    "Centerwallpanhorizontaldegrees",
    "Centerwallpanverticaldegrees",
    "Rightwallscale",
    "Rightwallpanhorizontaldegrees",
    "Rightwallpanverticaldegrees",
    "Qualityprofile",
    "Geometryresolution",
    "Pointbudget",
    "Pointsize",
    "Geometryfps",
    "Preservegeometryaspect",
    "Workspaceroot",
    "Gpuindex",
)

PULSE_CONTROLS = (
    "Applyall",
    "Startmogeworker",
    "Stopmogeworker",
    "Startdepthanythingworker",
    "Stopdepthanythingworker",
)

STATUS_CONTROLS = (
    "Effectivepointcloudscale",
    "Profilehint",
    "Workerpid",
    "Workerstatus",
)


def _operator(path, lookup=None):
    try:
        if callable(lookup):
            return lookup(path)
        return op(path)  # noqa: F821 - supplied by TouchDesigner
    except Exception:
        return None


def _parameter(node, name):
    if node is None:
        return None
    try:
        return getattr(node.par, name)
    except Exception:
        wanted = str(name).lower()
        try:
            for parameter in node.pars():
                if str(parameter.name).lower() == wanted:
                    return parameter
        except Exception:
            pass
    return None


def _value(node, name, fallback=None):
    parameter = _parameter(node, name)
    if parameter is None:
        return fallback
    try:
        return parameter.eval()
    except Exception:
        try:
            return parameter.val
        except Exception:
            return fallback


def _set(node, name, value):
    parameter = _parameter(node, name)
    if parameter is None:
        raise RuntimeError("%s is missing parameter %s" % (node.path, name))
    parameter.val = value


def _near(actual, expected, tolerance=1e-5):
    try:
        return math.isclose(
            float(actual), float(expected),
            rel_tol=tolerance, abs_tol=tolerance)
    except Exception:
        return actual == expected


def _record(checks, name, actual, expected, tolerance=1e-5, details=None):
    passed = _near(actual, expected, tolerance=tolerance)
    item = {
        "name": name,
        "status": "pass" if passed else "fail",
        "actual": actual,
        "expected": expected,
    }
    if details:
        item["details"] = details
    checks.append(item)
    return passed


def _assert_operator(node, path):
    if node is None:
        raise RuntimeError("required managed operator is missing: %s" % path)
    return node


def _snapshot(node, names):
    values = {}
    for name in names:
        parameter = _parameter(node, name)
        if parameter is None:
            raise RuntimeError("SHOW_CONTROL is missing %s" % name)
        values[name] = _value(node, name)
    return values


def _apply(callbacks, control, name, value):
    _set(control, name, value)
    callbacks.apply_parameter(name)


def _shader_contains(node, marker, value):
    if node is None:
        return False
    text = str(getattr(node, "text", ""))
    formatted = ("%.6g" % float(value))
    return marker in text and formatted in text


def _set_and_check(
        checks, callbacks, control, name, test_value,
        target, target_parameter, expected=None, tolerance=1e-5):
    _apply(callbacks, control, name, test_value)
    actual = _value(target, target_parameter)
    return _record(
        checks, name, actual,
        test_value if expected is None else expected,
        tolerance=tolerance,
        details={
            "target": getattr(target, "path", ""),
            "target_parameter": target_parameter,
        })


def _custom_parameter_names(control):
    names = []
    for parameter in control.customPars:
        names.append(str(parameter.name))
    return names


def _status_read_only(parameter):
    try:
        return bool(parameter.readOnly)
    except Exception:
        return False


def validate(
        report_path=None, expected_profile="3080ti_16gb",
        operator_lookup=None):
    """Exercise every public value control and restore the accepted state."""

    control = _assert_operator(
        _operator(CONTROL_PATH, operator_lookup), CONTROL_PATH)
    pipeline = _assert_operator(
        _operator(PIPELINE_PATH, operator_lookup), PIPELINE_PATH)
    callback_dat = _assert_operator(
        control.op("show_control_callbacks"),
        CONTROL_PATH + "/show_control_callbacks")
    callbacks = callback_dat.module

    adapter = _assert_operator(
        pipeline.op("SOURCES/STREAMDIFFUSION_ADAPTER"),
        PIPELINE_PATH + "/SOURCES/STREAMDIFFUSION_ADAPTER")
    completion = _assert_operator(
        pipeline.op("COMPLETION"), PIPELINE_PATH + "/COMPLETION")
    sensor = _assert_operator(
        pipeline.op("SENSOR_INTERACTION"),
        PIPELINE_PATH + "/SENSOR_INTERACTION")
    render = _assert_operator(
        pipeline.op("POINT_RENDER"), PIPELINE_PATH + "/POINT_RENDER")
    triple = _assert_operator(
        pipeline.op("TRIPLE_DISPLAY"), PIPELINE_PATH + "/TRIPLE_DISPLAY")
    reconstruction = _assert_operator(
        pipeline.op("RECONSTRUCTION"), PIPELINE_PATH + "/RECONSTRUCTION")
    ai_pipeline = _assert_operator(
        pipeline.parent().op("AI_PIPELINE"),
        ROOT_PATH + "/AI_PIPELINE")
    world_core = _assert_operator(
        pipeline.parent().op("WORLD_CORE"),
        ROOT_PATH + "/WORLD_CORE")
    moge_bridge = _assert_operator(
        adapter.op("MOGE2_BRIDGE"), adapter.path + "/MOGE2_BRIDGE")
    depth_bridge = _assert_operator(
        adapter.op("DEPTH_ANYTHING_GEOMETRY_BRIDGE"),
        adapter.path + "/DEPTH_ANYTHING_GEOMETRY_BRIDGE")

    snapshot = _snapshot(control, VALUE_CONTROLS)
    bridge_snapshot = {
        "moge_enabled": _value(moge_bridge, "Enabled", False),
        "depth_enabled": _value(depth_bridge, "Enabled", False),
        "adapter_geometry": _value(adapter, "Geometrysource", "moge2"),
    }
    checks = []
    started_ns = time.time_ns()

    try:
        custom_names = set(_custom_parameter_names(control))
        expected_names = set(
            VALUE_CONTROLS + PULSE_CONTROLS + STATUS_CONTROLS)
        _record(
            checks, "public_control_inventory",
            sorted(custom_names), sorted(expected_names),
            details={
                "custom_parameter_count": len(custom_names),
                "value_controls": len(VALUE_CONTROLS),
                "pulse_controls": len(PULSE_CONTROLS),
                "status_controls": len(STATUS_CONTROLS),
            })

        for name in STATUS_CONTROLS:
            _record(
                checks, name + "_read_only",
                _status_read_only(_parameter(control, name)), True)

        _set_and_check(
            checks, callbacks, control, "Geometryprovider",
            "depth_anything", adapter, "Geometrysource")
        _record(
            checks, "Geometryprovider_depth_bridge",
            bool(_value(depth_bridge, "Enabled", False)), True)
        _set_and_check(
            checks, callbacks, control, "Geometryprovider",
            "moge2", adapter, "Geometrysource")
        _record(
            checks, "Geometryprovider_moge_bridge",
            bool(_value(moge_bridge, "Enabled", False)), True)

        _set_and_check(
            checks, callbacks, control, "Displaymode",
            "panoramic_wrap", pipeline, "Displaymode")
        _set_and_check(
            checks, callbacks, control, "Completionmode",
            "fog", completion, "Mode")

        _set_and_check(
            checks, callbacks, control, "Fogdensity",
            0.47, completion, "Fogdensity")
        _record(
            checks, "Fogdensity_shader",
            _shader_contains(
                completion.op("fog_completion_PIXEL"),
                "FLEXGPU_FOG_DENSITY", 0.47),
            True)

        _set_and_check(
            checks, callbacks, control, "Interactionstrength",
            0.27, sensor, "Forcegain")
        _record(
            checks, "Interactionstrength_shader",
            _shader_contains(
                sensor.op("interaction_field_PIXEL"),
                "FLEXGPU_FORCE_GAIN", 0.27),
            True)
        _set_and_check(
            checks, callbacks, control, "Interactionsmoothing",
            0.41, sensor, "Interactionsmoothing")
        _record(
            checks, "Interactionsmoothing_shader",
            _shader_contains(
                sensor.op("INTERACTION_SMOOTH_PIXEL"),
                "FLEXGPU_INTERACTION_SMOOTHING", 0.41),
            True)

        for name, test_value, target_name in (
                ("Wrapyawdegrees", 22.0, "Wrapyawdegrees"),
                ("Wrapfovdegrees", 66.0, "Wrapfovdegrees"),
                ("Surfacefovdegrees", 55.0, "Surfacefovdegrees"),
                ("Artisticyawdegrees", 12.0, "Artisticyawdegrees"),
                ("Artisticoffsetmetres", 0.25, "Artisticoffsetmetres")):
            _set_and_check(
                checks, callbacks, control, name,
                test_value, render, target_name)

        _set_and_check(
            checks, callbacks, control, "Artisticoffsetdirection",
            "inward", render, "Artisticoffsetdirection")
        _record(
            checks, "Artisticoffsetdirection_left_camera",
            _value(render.op("CAMERA_ARTISTIC_LEFT"), "tx"), -0.25)
        _record(
            checks, "Artisticoffsetdirection_right_camera",
            _value(render.op("CAMERA_ARTISTIC_RIGHT"), "tx"), 0.25)

        for name, test_value, target_name in (
                ("Wrapcoverage", 0.44, "Wrapcoverage"),
                ("Wrapnoise", 0.33, "Wrapnoise")):
            _set_and_check(
                checks, callbacks, control, name,
                test_value, triple, target_name)
        _record(
            checks, "Wrapcoverage_shader",
            _shader_contains(
                triple.op("COVERAGE_WRAP_LEFT_PIXEL"),
                "FLEXGPU_WRAP_COVERAGE", 0.44),
            True)
        _record(
            checks, "Wrapnoise_shader",
            _shader_contains(
                triple.op("COVERAGE_WRAP_RIGHT_PIXEL"),
                "FLEXGPU_WRAP_NOISE", 0.33),
            True)

        _apply(callbacks, control, "Wallwidth", 1280)
        _apply(callbacks, control, "Wallheight", 720)
        installation_grade = pipeline.op(
            "INSTALLATION_OUTPUT/installation_grade")
        _record(
            checks, "Wallwidth",
            _value(installation_grade, "resolutionw"), 1280)
        _record(
            checks, "Wallheight",
            _value(installation_grade, "resolutionh"), 720)
        _record(
            checks, "Wall_mosaic_width",
            _value(triple.op("WRAP_MOSAIC"), "resolutionw"), 3840)

        wall_tests = {
            "Left": (1.10, 3.0, -2.0),
            "Center": (1.05, -1.0, 1.5),
            "Right": (0.95, -3.0, 2.0),
        }
        for side, values in wall_tests.items():
            scale, horizontal, vertical = values
            for suffix, test_value in (
                    ("wallscale", scale),
                    ("wallpanhorizontaldegrees", horizontal),
                    ("wallpanverticaldegrees", vertical)):
                name = side + suffix
                _set_and_check(
                    checks, callbacks, control, name,
                    test_value, render, name)
            camera = render.op("CAMERA_WRAP_" + side.upper())
            base_yaw = (
                22.0 if side == "Left" else
                -22.0 if side == "Right" else 0.0)
            _record(
                checks, side + "_camera_horizontal_pan",
                _value(camera, "ry"), base_yaw + horizontal)
            _record(
                checks, side + "_camera_vertical_pan",
                _value(camera, "rx"), vertical)

        _apply(callbacks, control, "Geometryprovider", "moge2")
        _apply(callbacks, control, "Pointcloudscale", 1.10)
        _apply(callbacks, control, "Moge2scale", 1.20)
        _record(
            checks, "Moge2_effective_scale",
            _value(control, "Effectivepointcloudscale"), 1.32)
        _record(
            checks, "Moge2_render_scale",
            _value(render, "Pointcloudscale"), 1.32)
        _apply(callbacks, control, "Geometryprovider", "depth_anything")
        _apply(callbacks, control, "Depthanythingscale", 0.90)
        _record(
            checks, "DepthAnything_effective_scale",
            _value(control, "Effectivepointcloudscale"), 0.99)
        _record(
            checks, "DepthAnything_render_scale",
            _value(render, "Pointcloudscale"), 0.99)

        _set_and_check(
            checks, callbacks, control, "Geometryresolution",
            320, reconstruction, "Geometryresolution")
        _record(
            checks, "Geometryresolution_ai_pipeline",
            _value(ai_pipeline, "Geometryresolution"), 320)
        _set_and_check(
            checks, callbacks, control, "Preservegeometryaspect",
            False, reconstruction, "Preservegeometryaspect")
        _set_and_check(
            checks, callbacks, control, "Pointbudget",
            130000, render, "Maxpoints")
        _record(
            checks, "Pointbudget_world_core",
            _value(world_core, "Pointbudget"), 130000)
        _set_and_check(
            checks, callbacks, control, "Pointsize",
            3.7, render, "Pointsize")
        _set_and_check(
            checks, callbacks, control, "Geometryfps",
            4, moge_bridge, "Capturefps")
        _record(
            checks, "Geometryfps_depth_bridge",
            _value(depth_bridge, "Capturefps"), 4)

        _apply(callbacks, control, "Qualityprofile", expected_profile)
        _record(
            checks, "Qualityprofile",
            _value(control, "Qualityprofile"), expected_profile)
        if expected_profile == "3080ti_16gb":
            for name, actual, expected in (
                    ("Qualityprofile_geometry",
                     _value(control, "Geometryresolution"), 384),
                    ("Qualityprofile_points",
                     _value(control, "Pointbudget"), 147456),
                    ("Qualityprofile_point_size",
                     _value(control, "Pointsize"), 4.2),
                    ("Qualityprofile_fps",
                     _value(control, "Geometryfps"), 5)):
                _record(checks, name, actual, expected)

        _record(
            checks, "Workspaceroot",
            Path(str(_value(control, "Workspaceroot", ""))).resolve().is_dir(),
            True)
        _record(
            checks, "Gpuindex",
            int(_value(control, "Gpuindex", -1)), 0)

        _parameter(control, "Applyall").pulse()
        callbacks.apply_all()
        _record(
            checks, "Applyall",
            _value(render, "Pointsize"),
            _value(control, "Pointsize"))

    finally:
        for name, value in snapshot.items():
            try:
                _set(control, name, value)
            except Exception:
                pass
        try:
            callbacks.apply_all()
        except Exception:
            pass
        try:
            _set(moge_bridge, "Enabled", bridge_snapshot["moge_enabled"])
            _set(depth_bridge, "Enabled", bridge_snapshot["depth_enabled"])
            _set(
                adapter, "Geometrysource",
                bridge_snapshot["adapter_geometry"])
        except Exception:
            pass

    failures = [item for item in checks if item["status"] != "pass"]
    restored = {}
    for name, expected in snapshot.items():
        actual = _value(control, name)
        restored[name] = {
            "status": "pass" if _near(actual, expected) else "fail",
            "actual": actual,
            "expected": expected,
        }
    restoration_failures = [
        name for name, item in restored.items()
        if item["status"] != "pass"]
    result = {
        "version": REPORT_VERSION,
        "captured_ns": time.time_ns(),
        "duration_ms": round((time.time_ns() - started_ns) / 1_000_000.0, 3),
        "profile": expected_profile,
        "status": (
            "pass" if not failures and not restoration_failures else "fail"),
        "summary": {
            "pass": len(checks) - len(failures),
            "fail": len(failures),
            "restoration_failures": len(restoration_failures),
        },
        "controls": {
            "value": list(VALUE_CONTROLS),
            "pulse": list(PULSE_CONTROLS),
            "status": list(STATUS_CONTROLS),
        },
        "checks": checks,
        "restored": restored,
        "restoration_failures": restoration_failures,
        "worker_buttons": {
            "status": "external_validation_required",
            "reason": (
                "Worker start/stop is verified by the PowerShell release gate "
                "so TouchDesigner's main thread remains free to cook."),
        },
    }
    if report_path:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    print("[FlexGPU show controls] %s: %d pass, %d fail, %d restore fail" % (
        result["status"], result["summary"]["pass"],
        result["summary"]["fail"],
        result["summary"]["restoration_failures"]))
    return result


if __name__ == "__main__":
    raise RuntimeError(
        "Run validate_show_controls.validate() inside TouchDesigner.")
