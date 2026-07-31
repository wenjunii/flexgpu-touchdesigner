"""Reversible live validation for the public FlexGPU SHOW_CONTROL component.

Run this inside TouchDesigner after the tracked runtime has been installed.
The validator changes one public parameter at a time, verifies its managed
target, and restores every original value in a ``finally`` block.  Worker
start/stop pulses are intentionally tested by the external PowerShell release
gate because waiting for a worker inside TouchDesigner's main thread would
prevent bridge callbacks from cooking.  When the optional combined-podcast
``show_control`` exists at the stable adapter boundary, its complete public
parameter inventory and safe value controls are checked without inspecting
private StreamDiffusionTD internals.
"""

from __future__ import print_function

import json
import math
import time
from pathlib import Path


ROOT_PATH = "/project1/flexgpu"
PIPELINE_PATH = ROOT_PATH + "/WORKING_PIPELINE"
CONTROL_PATH = PIPELINE_PATH + "/SHOW_CONTROL"
REPORT_VERSION = "flexgpu-show-controls-validation/v2"

OUTPUT_DIMENSION_TARGETS = (
    ("OUT_INSTALLATION", 1),
    ("OUT_TRIPLE_WRAP_LEFT", 1),
    ("OUT_TRIPLE_WRAP_CENTER", 1),
    ("OUT_TRIPLE_WRAP_RIGHT", 1),
    ("OUT_TRIPLE_ARTISTIC_LEFT", 1),
    ("OUT_TRIPLE_ARTISTIC_CENTER", 1),
    ("OUT_TRIPLE_ARTISTIC_RIGHT", 1),
    ("OUT_TRIPLE_WRAP", 3),
    ("OUT_TRIPLE_ARTISTIC", 3),
)


VALUE_CONTROLS = (
    "Geometryprovider",
    "Audioenabled",
    "Audiosource",
    "Displaymode",
    "Completionmode",
    "Fogdensity",
    "Brightness",
    "Contrast",
    "Saturation",
    "Gamma",
    "Hueshiftdegrees",
    "Temperature",
    "Tint",
    "Interactionradius",
    "Interactionfalloff",
    "Interactionstrength",
    "Interactionsmoothing",
    "Interactionresponse",
    "Interactiondecay",
    "Installationinteractionenabled",
    "Installationinteractionintensity",
    "Leftwallinteractionenabled",
    "Leftwallinteractionintensity",
    "Centerwallinteractionenabled",
    "Centerwallinteractionintensity",
    "Rightwallinteractionenabled",
    "Rightwallinteractionintensity",
    "Wrapyawdegrees",
    "Wrapfovdegrees",
    "Wrapcoverage",
    "Wrapnoise",
    "Camerainteractionenabled",
    "Camerasensorsource",
    "Cameraname",
    "Cameraindex",
    "Cameramirrorhorizontal",
    "Femtodeviceserial",
    "Sensorpositionscale",
    "Sensortrimxmetres",
    "Sensortrimymetres",
    "Sensortrimzmetres",
    "Sensortrimyawdegrees",
    "Sensortrimpitchdegrees",
    "Sensortrimrolldegrees",
    "Femtomirrorhorizontal",
    "Femtopositionscale",
    "Femtotrimxmetres",
    "Femtotrimymetres",
    "Femtotrimzmetres",
    "Femtotrimyawdegrees",
    "Femtotrimpitchdegrees",
    "Femtotrimrolldegrees",
    "Femtoaudiencenearmetres",
    "Femtoaudiencefarmetres",
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
    "Experience",
    "Vrinputsource",
    "Vrtargethz",
    "Vreyewidth",
    "Vreyeheight",
    "Vripdmetres",
    "Vrfovdegrees",
    "Vrheadxmetres",
    "Vrheadymetres",
    "Vrheadzmetres",
    "Vrheadyawdegrees",
    "Vrheadpitchdegrees",
    "Vrheadrolldegrees",
    "Vrhandenabled",
    "Vrhandgain",
    "Vrlefthandxmetres",
    "Vrlefthandymetres",
    "Vrlefthandzmetres",
    "Vrrighthandxmetres",
    "Vrrighthandymetres",
    "Vrrighthandzmetres",
)

PULSE_CONTROLS = (
    "Applyall",
    "Resetcolor",
    "Startmogeworker",
    "Stopmogeworker",
    "Startdepthanythingworker",
    "Stopdepthanythingworker",
    "Startcameradepthworker",
    "Stopcameradepthworker",
    "Resetsensorcalibrationtrim",
    "Resetfemtocalibrationtrim",
    "Resetvrheadpose",
    "Resetvrhands",
)

STATUS_CONTROLS = (
    "Effectivepointcloudscale",
    "Profilehint",
    "Workerpid",
    "Workerstatus",
    "Sensorworkerpid",
    "Sensorworkerstatus",
    "Femtostatus",
    "Vrstatus",
)

ADAPTER_VALUE_CONTROLS = (
    "Play",
    "Audioenabled",
    "Randomseeds",
    "Crossfadesec",
    "Audiosource",
    "Colorenabled",
    "Brightness",
    "Contrast",
    "Gamma",
    "Blacklevel",
    "Opacity",
    "Hue",
    "Saturation",
    "Value",
)

ADAPTER_PULSE_CONTROLS = (
    "Newseeds",
    "Restart",
    "Reload",
    "Resetcolor",
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


def _snapshot(node, names, label="SHOW_CONTROL"):
    values = {}
    for name in names:
        parameter = _parameter(node, name)
        if parameter is None:
            raise RuntimeError("%s is missing %s" % (label, name))
        values[name] = _value(node, name)
    return values


def _parameter_state(node, name):
    parameter = _parameter(node, name)
    if parameter is None:
        return None
    state = {"value": _value(node, name)}
    for attribute in ("expr", "mode"):
        try:
            state[attribute] = getattr(parameter, attribute)
        except Exception:
            pass
    return state


def _restore_parameter_state(node, name, state):
    if state is None:
        return
    parameter = _parameter(node, name)
    if parameter is None:
        return
    parameter.val = state["value"]
    if "expr" in state:
        parameter.expr = state["expr"]
    if "mode" in state:
        parameter.mode = state["mode"]


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


def _top_dimensions(node):
    if node is None:
        return None
    try:
        node.cook(force=True)
    except Exception:
        try:
            node.cook()
        except Exception:
            pass
    try:
        return [int(node.width), int(node.height)]
    except Exception:
        return None


def _output_dimension_report(pipeline, wall_width, wall_height):
    outputs = {}
    mismatches = {}
    for name, width_multiplier in OUTPUT_DIMENSION_TARGETS:
        expected = [
            int(wall_width) * int(width_multiplier),
            int(wall_height),
        ]
        actual = _top_dimensions(pipeline.op(name))
        item = {
            "actual": actual,
            "expected": expected,
            "status": "pass" if actual == expected else "fail",
        }
        outputs[name] = item
        if item["status"] != "pass":
            mismatches[name] = item
    return {
        "status": "pass" if not mismatches else "fail",
        "wall_width": int(wall_width),
        "wall_height": int(wall_height),
        "outputs": outputs,
        "mismatches": mismatches,
        "note": (
            "Actual TOP width/height are authoritative. Resolution parameters "
            "alone can remain at the requested values when the active "
            "TouchDesigner license clamps the cooked TOP."),
    }


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
    sensor_adapter = _assert_operator(
        sensor.op("DEPTH_SENSOR_ADAPTER"),
        sensor.path + "/DEPTH_SENSOR_ADAPTER")
    sensor_bridge = _assert_operator(
        sensor_adapter.op("DEPTH_ANYTHING_BRIDGE"),
        sensor_adapter.path + "/DEPTH_ANYTHING_BRIDGE")
    femto_adapter = _assert_operator(
        pipeline.op("SOURCES/FEMTO_MEGA_ADAPTER"),
        pipeline.path + "/SOURCES/FEMTO_MEGA_ADAPTER")
    render = _assert_operator(
        pipeline.op("POINT_RENDER"), PIPELINE_PATH + "/POINT_RENDER")
    vr = _assert_operator(
        pipeline.op("VR_OUTPUT"), PIPELINE_PATH + "/VR_OUTPUT")
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
    adapter_show_control = adapter.op("show_control")
    audio_switch = adapter.op("audiosource_switch")
    audio_out = adapter.op("audio_out")

    snapshot = _snapshot(control, VALUE_CONTROLS)
    bridge_snapshot = {
        "moge_enabled": _value(moge_bridge, "Enabled", False),
        "depth_enabled": _value(depth_bridge, "Enabled", False),
        "adapter_geometry": _value(adapter, "Geometrysource", "moge2"),
    }
    sensor_snapshot = {
        "mode": _value(sensor, "Mode", "disabled"),
        "adapter_enabled": _value(sensor_adapter, "Enabled", False),
        "adapter_source": _value(
            sensor_adapter, "Sensorsource", "depth_anything"),
        "mirror_horizontal": _value(
            sensor_bridge, "Mirrorhorizontal", True),
        "femto_enabled": _value(femto_adapter, "Enabled", False),
        "femto_serial": _value(femto_adapter, "Deviceserial", ""),
        "sensor_to_world": [
            _value(sensor, "Sensortoworld%d" % index, "")
            for index in range(4)
        ],
    }
    audio_snapshot = {
        "adapter_enabled": _value(adapter, "Audioenabled", False),
        "adapter_source": _value(adapter, "Audiosource", "voices"),
        "show_enabled": _value(
            adapter_show_control, "Audioenabled", None),
        "show_source": _value(
            adapter_show_control, "Audiosource", None),
        "switch_index": _parameter_state(audio_switch, "index"),
        "out_active": _parameter_state(audio_out, "active"),
    }
    adapter_control_snapshot = {}
    if adapter_show_control is not None:
        adapter_control_snapshot = _snapshot(
            adapter_show_control,
            ADAPTER_VALUE_CONTROLS,
            label="adapter show_control",
        )
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

        if adapter_show_control is None:
            _record(
                checks, "adapter_control_inventory",
                "missing", "present")
        else:
            adapter_names = set(
                _custom_parameter_names(adapter_show_control))
            expected_adapter_names = set(
                ADAPTER_VALUE_CONTROLS + ADAPTER_PULSE_CONTROLS)
            missing_adapter_names = sorted(
                expected_adapter_names - adapter_names)
            _record(
                checks, "adapter_control_inventory",
                missing_adapter_names, [],
                details={
                    "path": adapter_show_control.path,
                    "required_value_controls": len(ADAPTER_VALUE_CONTROLS),
                    "required_pulse_controls": len(ADAPTER_PULSE_CONTROLS),
                })
            for name in ADAPTER_PULSE_CONTROLS:
                _record(
                    checks, "adapter_" + name + "_present",
                    _parameter(adapter_show_control, name) is not None, True)
            for name, test_value in (
                    ("Randomseeds", not bool(_value(
                        adapter_show_control, "Randomseeds", True))),
                    ("Crossfadesec", 1.75),
                    ("Colorenabled", not bool(_value(
                        adapter_show_control, "Colorenabled", True))),
                    ("Brightness", 1.12),
                    ("Contrast", 1.18),
                    ("Gamma", 1.14),
                    ("Blacklevel", 0.03),
                    ("Opacity", 0.91),
                    ("Hue", 0.08),
                    ("Saturation", 0.82),
                    ("Value", 0.93)):
                _set(adapter_show_control, name, test_value)
                _record(
                    checks, "adapter_" + name,
                    _value(adapter_show_control, name), test_value)
            _record(
                checks, "adapter_Play_ui_test_required",
                _parameter(adapter_show_control, "Play") is not None, True,
                details={
                    "reason": (
                        "Play, Restart, Reload and New Seeds are exercised "
                        "through the live panel so pausing the timeline cannot "
                        "deadlock this synchronous validator.")
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

        _apply(callbacks, control, "Audioenabled", False)
        _record(
            checks, "Audioenabled_adapter",
            bool(_value(adapter, "Audioenabled", True)), False)
        if adapter_show_control is not None:
            _record(
                checks, "Audioenabled_adapter_show_control",
                bool(_value(
                    adapter_show_control, "Audioenabled", True)), False)
        if audio_out is not None:
            _record(
                checks, "Audioenabled_audio_out",
                bool(_value(audio_out, "active", True)), False)

        _apply(callbacks, control, "Audiosource", "soundscape")
        _record(
            checks, "Audiosource_adapter",
            _value(adapter, "Audiosource"), "soundscape")
        if adapter_show_control is not None:
            _record(
                checks, "Audiosource_adapter_show_control",
                _value(adapter_show_control, "Audiosource"), "soundscape")
        if audio_switch is not None:
            _record(
                checks, "Audiosource_exclusive_switch_index",
                int(_value(audio_switch, "index", -1)), 1)
            try:
                selected_audio = audio_switch.inputs[1].name
            except Exception:
                selected_audio = None
            _record(
                checks, "Audiosource_exclusive_switch_input",
                selected_audio, "soundscape_audio")

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

        color_tests = (
            ("Brightness", 0.12, "FLEXGPU_COLOR_BRIGHTNESS"),
            ("Contrast", 1.18, "FLEXGPU_COLOR_CONTRAST"),
            ("Saturation", 0.82, "FLEXGPU_COLOR_SATURATION"),
            ("Gamma", 1.14, "FLEXGPU_COLOR_GAMMA"),
            ("Hueshiftdegrees", 17.0, "FLEXGPU_COLOR_HUE_SHIFT"),
            ("Temperature", 0.16, "FLEXGPU_COLOR_TEMPERATURE"),
            ("Tint", -0.11, "FLEXGPU_COLOR_TINT"),
        )
        for name, test_value, marker in color_tests:
            _apply(callbacks, control, name, test_value)
            _record(
                checks, name, _value(control, name), test_value)
            grade_dats = [
                pipeline.op(
                    "INSTALLATION_OUTPUT/installation_grade_PIXEL"),
            ]
            for mode in ("WRAP", "ARTISTIC"):
                for side in ("LEFT", "CENTER", "RIGHT"):
                    grade_dats.append(triple.op(
                        "GRADE_%s_%s_PIXEL" % (mode, side)))
            for eye in ("LEFT", "RIGHT"):
                grade_dats.append(pipeline.op(
                    "STEREO_PREVIEW/GRADE_%s_EYE_PIXEL" % eye))
            for dat in grade_dats:
                _record(
                    checks,
                    "%s_shader_%s" % (name, dat.name),
                    _shader_contains(dat, marker, test_value), True)

        callbacks._reset_color_grade()
        for name, expected in (
                ("Brightness", 0.0),
                ("Contrast", 1.0),
                ("Saturation", 1.0),
                ("Gamma", 1.0),
                ("Hueshiftdegrees", 0.0),
                ("Temperature", 0.0),
                ("Tint", 0.0)):
            _record(
                checks, "Resetcolor_" + name,
                _value(control, name), expected)

        _apply(callbacks, control, "Experience", "combined")
        _record(
            checks, "Experience_vr_enabled",
            bool(_value(vr, "Enabled", False)), True)
        _record(
            checks, "Experience_render_vr_enabled",
            bool(_value(render, "Vrenabled", False)), True)
        _set_and_check(
            checks, callbacks, control, "Vrinputsource",
            "mock", vr, "Inputsource")
        for name, value, target, target_parameter in (
                ("Vrtargethz", 80, vr, "Targethz"),
                ("Vreyewidth", 960, vr, "Eyewidth"),
                ("Vreyeheight", 540, vr, "Eyeheight"),
                ("Vripdmetres", 0.066, vr, "Ipdmetres"),
                ("Vrfovdegrees", 82.0, vr, "Fovdegrees"),
                ("Vrheadxmetres", 0.12, render, "Vrheadxmetres"),
                ("Vrheadymetres", -0.08, render, "Vrheadymetres"),
                ("Vrheadzmetres", 0.16, render, "Vrheadzmetres"),
                ("Vrheadyawdegrees", 13.0, render, "Vrheadyawdegrees"),
                ("Vrheadpitchdegrees", -7.0, render,
                 "Vrheadpitchdegrees"),
                ("Vrheadrolldegrees", 4.0, render,
                 "Vrheadrolldegrees"),
                ("Vrhandgain", 0.73, vr, "Handgain"),
                ("Vrlefthandxmetres", -0.19, vr,
                 "Lefthandxmetres"),
                ("Vrlefthandymetres", 0.11, vr,
                 "Lefthandymetres"),
                ("Vrlefthandzmetres", -0.92, vr,
                 "Lefthandzmetres"),
                ("Vrrighthandxmetres", 0.21, vr,
                 "Righthandxmetres"),
                ("Vrrighthandymetres", 0.09, vr,
                 "Righthandymetres"),
                ("Vrrighthandzmetres", -0.95, vr,
                 "Righthandzmetres")):
            _set_and_check(
                checks, callbacks, control, name,
                value, target, target_parameter)
        _apply(callbacks, control, "Vrhandenabled", True)
        _record(
            checks, "Vrhandenabled",
            bool(_value(vr, "Handsenabled", False)), True)
        _record(
            checks, "Vrhandgain_shader",
            _shader_contains(
                sensor.op("interaction_field_PIXEL"),
                "FLEXGPU_VR_HAND_GAIN", 0.73),
            True)
        _record(
            checks, "Vrleft_eye_dimensions",
            _top_dimensions(vr.op("OUT_LEFT_EYE")), [960, 540])
        _record(
            checks, "Vrright_eye_dimensions",
            _top_dimensions(vr.op("OUT_RIGHT_EYE")), [960, 540])
        callbacks._reset_vr_head_pose()
        for name in (
                "Vrheadxmetres", "Vrheadymetres", "Vrheadzmetres",
                "Vrheadyawdegrees", "Vrheadpitchdegrees",
                "Vrheadrolldegrees"):
            _record(
                checks, "Resetvrheadpose_" + name,
                _value(control, name), 0.0)
        callbacks._reset_vr_hands()
        for name, expected in (
                ("Vrlefthandxmetres", -0.28),
                ("Vrlefthandymetres", 0.02),
                ("Vrlefthandzmetres", -1.15),
                ("Vrrighthandxmetres", 0.28),
                ("Vrrighthandymetres", 0.02),
                ("Vrrighthandzmetres", -1.15)):
            _record(
                checks, "Resetvrhands_" + name,
                _value(control, name), expected)
        _apply(callbacks, control, "Vrhandenabled", False)
        _apply(callbacks, control, "Experience", "installation")
        _record(
            checks, "Experience_installation_vr_disabled",
            bool(_value(vr, "Enabled", True)), False)

        _set_and_check(
            checks, callbacks, control, "Interactionradius",
            0.82, sensor, "Interactionradius")
        _record(
            checks, "Interactionradius_shader",
            _shader_contains(
                sensor.op("interaction_field_PIXEL"),
                "FLEXGPU_INTERACTION_RADIUS", 0.82),
            True)
        _set_and_check(
            checks, callbacks, control, "Interactionfalloff",
            1.7, sensor, "Interactionfalloff")
        _record(
            checks, "Interactionfalloff_shader",
            _shader_contains(
                sensor.op("interaction_field_PIXEL"),
                "FLEXGPU_INTERACTION_FALLOFF", 1.7),
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
        _set_and_check(
            checks, callbacks, control, "Interactionresponse",
            0.78, sensor, "Interactionresponse")
        _record(
            checks, "Interactionresponse_shader",
            _shader_contains(
                sensor.op("INTERACTION_SMOOTH_PIXEL"),
                "FLEXGPU_INTERACTION_RESPONSE", 0.78),
            True)
        _set_and_check(
            checks, callbacks, control, "Interactiondecay",
            0.68, sensor, "Interactiondecay")
        _record(
            checks, "Interactiondecay_shader",
            _shader_contains(
                sensor.op("INTERACTION_SMOOTH_PIXEL"),
                "FLEXGPU_INTERACTION_DECAY", 0.68),
            True)

        for (
                enabled_name, intensity_name, view,
                enabled_value, intensity_value) in (
                    (
                        "Installationinteractionenabled",
                        "Installationinteractionintensity",
                        "INSTALLATION", True, 6.3),
                    (
                        "Leftwallinteractionenabled",
                        "Leftwallinteractionintensity",
                        "LEFT", False, 7.4),
                    (
                        "Centerwallinteractionenabled",
                        "Centerwallinteractionintensity",
                        "CENTER", True, 8.5),
                    (
                        "Rightwallinteractionenabled",
                        "Rightwallinteractionintensity",
                        "RIGHT", False, 9.6)):
            _apply(callbacks, control, intensity_name, intensity_value)
            _apply(callbacks, control, enabled_name, enabled_value)
            _record(
                checks, intensity_name,
                _value(render, intensity_name), intensity_value)
            _record(
                checks, enabled_name,
                bool(_value(render, enabled_name)), enabled_value)
            _record(
                checks, enabled_name + "_shader",
                _shader_contains(
                    render.op("VIEW_POSITION_%s_PIXEL" % view),
                    "FLEXGPU_VIEW_INTERACTION_GAIN",
                    intensity_value if enabled_value else 0.0),
                True)

        _apply(callbacks, control, "Camerainteractionenabled", False)
        _record(
            checks, "Camerainteractionenabled_mode_disabled",
            _value(sensor, "Mode"), "disabled")
        _record(
            checks, "Camerainteractionenabled_adapter_disabled",
            bool(_value(sensor_adapter, "Enabled", True)), False)
        _apply(callbacks, control, "Camerasensorsource", "depth_anything")
        _apply(callbacks, control, "Camerainteractionenabled", True)
        _record(
            checks, "Camerainteractionenabled_mode_depth_sensor",
            _value(sensor, "Mode"), "depth_sensor")
        _record(
            checks, "Camerainteractionenabled_adapter_enabled",
            bool(_value(sensor_adapter, "Enabled", False)), True)
        _record(
            checks, "Camerasensorsource_depth_anything",
            _value(sensor_adapter, "Sensorsource"), "depth_anything")
        _record(
            checks, "Camerasensorsource_depth_bridge_enabled",
            bool(_value(sensor_bridge, "Enabled", False)), True)
        _record(
            checks, "Camerasensorsource_femto_disabled",
            bool(_value(femto_adapter, "Enabled", True)), False)
        _apply(callbacks, control, "Camerasensorsource", "femto_mega")
        _record(
            checks, "Camerasensorsource_femto_mega",
            _value(sensor_adapter, "Sensorsource"), "femto_mega")
        _record(
            checks, "Camerasensorsource_depth_bridge_disabled",
            bool(_value(sensor_bridge, "Enabled", True)), False)
        femto_enabled = bool(_value(femto_adapter, "Enabled", False))
        femto_status = str(
            _value(control, "Femtostatus", "") or "").strip()
        femto_fail_closed = any(
            marker in femto_status.lower()
            for marker in (
                "unavailable",
                "failed",
                "missing",
                "not found",
                "no usb",
            ))
        femto_state_is_consistent = (
            femto_enabled != femto_fail_closed)
        _record(
            checks, "Camerasensorsource_femto_enabled",
            femto_state_is_consistent, True,
            details={
                "enabled": femto_enabled,
                "status": femto_status,
                "disconnected_hardware_is_accepted_only_when_fail_closed":
                    femto_fail_closed,
            })
        _apply(callbacks, control, "Cameramirrorhorizontal", False)
        _record(
            checks, "Cameramirrorhorizontal",
            bool(_value(sensor_bridge, "Mirrorhorizontal", True)), False)
        for name, test_value in (
                ("Cameraname", "validator camera"),
                ("Cameraindex", 7)):
            _apply(callbacks, control, name, test_value)
            _record(
                checks, name, _value(control, name), test_value)

        calibration_shader = sensor.op(
            "CALIBRATE_SENSOR_POSITION_PIXEL")
        _apply(callbacks, control, "Camerasensorsource", "depth_anything")
        for name, test_value, marker in (
                ("Sensorpositionscale", 1.17,
                 "FLEXGPU_SENSOR_POSITION_SCALE"),
                ("Sensortrimxmetres", 0.12,
                 "FLEXGPU_SENSOR_TRIM_X"),
                ("Sensortrimymetres", -0.08,
                 "FLEXGPU_SENSOR_TRIM_Y"),
                ("Sensortrimzmetres", 0.21,
                 "FLEXGPU_SENSOR_TRIM_Z"),
                ("Sensortrimyawdegrees", 11.0,
                 "FLEXGPU_SENSOR_TRIM_YAW"),
                ("Sensortrimpitchdegrees", -7.0,
                 "FLEXGPU_SENSOR_TRIM_PITCH"),
                ("Sensortrimrolldegrees", 4.0,
                 "FLEXGPU_SENSOR_TRIM_ROLL")):
            _apply(callbacks, control, name, test_value)
            _record(
                checks, name, _value(control, name), test_value)
            _record(
                checks, name + "_shader",
                _shader_contains(
                    calibration_shader, marker, test_value), True)
        _record(
            checks, "Sensorcalibration_baseline_preserved",
            [
                _value(sensor, "Sensortoworld%d" % index, "")
                for index in range(4)
            ],
            sensor_snapshot["sensor_to_world"])
        callbacks._reset_sensor_calibration_trim("Sensor")
        for name, expected in (
                ("Sensorpositionscale", 1.0),
                ("Sensortrimxmetres", 0.0),
                ("Sensortrimymetres", 0.0),
                ("Sensortrimzmetres", 0.0),
                ("Sensortrimyawdegrees", 0.0),
                ("Sensortrimpitchdegrees", 0.0),
                ("Sensortrimrolldegrees", 0.0)):
            _record(
                checks, "Resetsensorcalibrationtrim_" + name,
                _value(control, name), expected)
        _record(
            checks, "Resetsensorcalibrationtrim_baseline_preserved",
            [
                _value(sensor, "Sensortoworld%d" % index, "")
                for index in range(4)
            ],
            sensor_snapshot["sensor_to_world"])

        _apply(callbacks, control, "Camerasensorsource", "femto_mega")
        _apply(callbacks, control, "Femtomirrorhorizontal", True)
        _record(
            checks, "Femtomirrorhorizontal",
            bool(_value(control, "Femtomirrorhorizontal")), True)
        _record(
            checks, "Femtomirrorhorizontal_shader",
            _shader_contains(
                femto_adapter.op("CONVERT_SENSOR_POSITION_PIXEL"),
                "FLEXGPU_FEMTO_MIRROR_HORIZONTAL", 1.0),
            True)
        for name, test_value, marker in (
                ("Femtopositionscale", 1.09,
                 "FLEXGPU_SENSOR_POSITION_SCALE"),
                ("Femtotrimxmetres", -0.14,
                 "FLEXGPU_SENSOR_TRIM_X"),
                ("Femtotrimymetres", 0.11,
                 "FLEXGPU_SENSOR_TRIM_Y"),
                ("Femtotrimzmetres", 0.33,
                 "FLEXGPU_SENSOR_TRIM_Z"),
                ("Femtotrimyawdegrees", -9.0,
                 "FLEXGPU_SENSOR_TRIM_YAW"),
                ("Femtotrimpitchdegrees", 6.0,
                 "FLEXGPU_SENSOR_TRIM_PITCH"),
                ("Femtotrimrolldegrees", -3.0,
                 "FLEXGPU_SENSOR_TRIM_ROLL")):
            _apply(callbacks, control, name, test_value)
            _record(
                checks, name, _value(control, name), test_value)
            _record(
                checks, name + "_shader",
                _shader_contains(
                    calibration_shader, marker, test_value), True)
        femto_validity_shader = femto_adapter.op(
            "DERIVE_SENSOR_VALIDITY_PIXEL")
        for name, test_value, marker in (
                ("Femtoaudiencenearmetres", 2.4,
                 "FLEXGPU_FEMTO_NEAR_METRES"),
                ("Femtoaudiencefarmetres", 4.6,
                 "FLEXGPU_FEMTO_FAR_METRES")):
            _apply(callbacks, control, name, test_value)
            _record(
                checks, name, _value(control, name), test_value)
            _record(
                checks, name + "_shader",
                _shader_contains(
                    femto_validity_shader, marker, test_value), True)
        _record(
            checks, "Femtocalibration_baseline_preserved",
            [
                _value(sensor, "Sensortoworld%d" % index, "")
                for index in range(4)
            ],
            sensor_snapshot["sensor_to_world"])
        callbacks._reset_sensor_calibration_trim("Femto")
        for name, expected in (
                ("Femtopositionscale", 1.0),
                ("Femtotrimxmetres", 0.0),
                ("Femtotrimymetres", 0.0),
                ("Femtotrimzmetres", 0.0),
                ("Femtotrimyawdegrees", 0.0),
                ("Femtotrimpitchdegrees", 0.0),
                ("Femtotrimrolldegrees", 0.0)):
            _record(
                checks, "Resetfemtocalibrationtrim_" + name,
                _value(control, name), expected)
        _record(
            checks, "Resetfemtocalibrationtrim_baseline_preserved",
            [
                _value(sensor, "Sensortoworld%d" % index, "")
                for index in range(4)
            ],
            sensor_snapshot["sensor_to_world"])

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
            _set(sensor, "Mode", sensor_snapshot["mode"])
            _set(
                sensor_adapter, "Enabled",
                sensor_snapshot["adapter_enabled"])
            _set(
                sensor_adapter, "Sensorsource",
                sensor_snapshot["adapter_source"])
            _set(
                sensor_bridge, "Mirrorhorizontal",
                sensor_snapshot["mirror_horizontal"])
            _set(
                femto_adapter, "Enabled",
                sensor_snapshot["femto_enabled"])
            _set(
                femto_adapter, "Deviceserial",
                sensor_snapshot["femto_serial"])
            _set(
                adapter, "Audioenabled",
                audio_snapshot["adapter_enabled"])
            _set(
                adapter, "Audiosource",
                audio_snapshot["adapter_source"])
            if adapter_show_control is not None:
                _set(
                    adapter_show_control, "Audioenabled",
                    audio_snapshot["show_enabled"])
                _set(
                    adapter_show_control, "Audiosource",
                    audio_snapshot["show_source"])
                for name, value in adapter_control_snapshot.items():
                    _set(adapter_show_control, name, value)
            _restore_parameter_state(
                audio_switch, "index", audio_snapshot["switch_index"])
            _restore_parameter_state(
                audio_out, "active", audio_snapshot["out_active"])
        except Exception:
            pass

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
    adapter_restored = {}
    for name, expected in adapter_control_snapshot.items():
        actual = _value(adapter_show_control, name)
        adapter_restored[name] = {
            "status": "pass" if _near(actual, expected) else "fail",
            "actual": actual,
            "expected": expected,
        }
    adapter_restoration_failures = [
        name for name, item in adapter_restored.items()
        if item["status"] != "pass"]
    output_dimensions = _output_dimension_report(
        pipeline,
        int(snapshot["Wallwidth"]),
        int(snapshot["Wallheight"]),
    )
    for name, item in output_dimensions["outputs"].items():
        _record(
            checks,
            "restored_output_dimensions_" + name,
            item["actual"],
            item["expected"],
            details={
                "target": PIPELINE_PATH + "/" + name,
                "actual_top_dimensions_are_authoritative": True,
            },
        )
    failures = [item for item in checks if item["status"] != "pass"]
    all_restoration_failures = (
        list(restoration_failures)
        + ["adapter." + name for name in adapter_restoration_failures]
    )
    result = {
        "version": REPORT_VERSION,
        "captured_ns": time.time_ns(),
        "duration_ms": round((time.time_ns() - started_ns) / 1_000_000.0, 3),
        "profile": expected_profile,
        "status": (
            "pass" if not failures and not all_restoration_failures else "fail"),
        "summary": {
            "pass": len(checks) - len(failures),
            "fail": len(failures),
            "restoration_failures": len(all_restoration_failures),
        },
        "controls": {
            "value": list(VALUE_CONTROLS),
            "pulse": list(PULSE_CONTROLS),
            "status": list(STATUS_CONTROLS),
            "adapter_value": list(ADAPTER_VALUE_CONTROLS),
            "adapter_pulse": list(ADAPTER_PULSE_CONTROLS),
        },
        "checks": checks,
        "restored": restored,
        "adapter_restored": adapter_restored,
        "restoration_failures": all_restoration_failures,
        "output_dimensions": output_dimensions,
        "worker_buttons": {
            "status": "external_validation_required",
            "reason": (
                "Worker start/stop is verified by the PowerShell release gate "
                "so TouchDesigner's main thread remains free to cook."),
        },
        "adapter_ui_buttons": {
            "status": "external_validation_required",
            "controls": [
                "Play", "Newseeds", "Restart", "Reload", "Resetcolor"],
            "reason": (
                "Exercise timeline and scene pulses in the visible panel, "
                "then verify two externally separated OUT_RGB samples."),
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
