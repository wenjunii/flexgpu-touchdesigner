"""In-TouchDesigner validation for a locally rebuilt FlexGPU project.

This module is intentionally import-safe outside TouchDesigner.  ``validate``
must be called from TouchDesigner's Python runtime, where it inspects and cooks
the generated network, optionally saves synthetic preview frames, and writes an
atomic machine-local report.  Reports and captures belong under ignored
``runtime/`` and ``captures/`` paths; they are never public release artifacts.
"""

from __future__ import print_function

import json
import math
import os
import time


VALIDATION_VERSION = "flexgpu-td-validation/v1"
ROOT_PATH = "/project1/flexgpu"
PIPELINE_PATH = ROOT_PATH + "/WORKING_PIPELINE"

REQUIRED_OPERATORS = (
    ROOT_PATH + "/CONFIG",
    ROOT_PATH + "/STARTUP/runtime_helpers",
    PIPELINE_PATH,
    PIPELINE_PATH + "/SOURCES",
    PIPELINE_PATH + "/ROLE_BRIDGE",
    PIPELINE_PATH + "/RECONSTRUCTION",
    PIPELINE_PATH + "/SENSOR_INTERACTION",
    PIPELINE_PATH + "/TEMPORAL_WORLD",
    PIPELINE_PATH + "/COMPLETION",
    PIPELINE_PATH + "/POINT_RENDER",
    PIPELINE_PATH + "/TRIPLE_DISPLAY",
    PIPELINE_PATH + "/TELEMETRY/LIVE_HEALTH",
    PIPELINE_PATH + "/OUT_POSITION",
    PIPELINE_PATH + "/OUT_COLOR",
    PIPELINE_PATH + "/OUT_INTERACTION",
    PIPELINE_PATH + "/OUT_INSTALLATION",
    PIPELINE_PATH + "/OUT_TRIPLE_WRAP",
    PIPELINE_PATH + "/OUT_TRIPLE_ARTISTIC",
    PIPELINE_PATH + "/OUT_DISPLAY_ACTIVE",
    PIPELINE_PATH + "/OUT_TRIPLE_WRAP_LEFT",
    PIPELINE_PATH + "/OUT_TRIPLE_WRAP_CENTER",
    PIPELINE_PATH + "/OUT_TRIPLE_WRAP_RIGHT",
    PIPELINE_PATH + "/OUT_TRIPLE_ARTISTIC_LEFT",
    PIPELINE_PATH + "/OUT_TRIPLE_ARTISTIC_CENTER",
    PIPELINE_PATH + "/OUT_TRIPLE_ARTISTIC_RIGHT",
    PIPELINE_PATH + "/OUT_LEFT_EYE",
    PIPELINE_PATH + "/OUT_RIGHT_EYE",
    PIPELINE_PATH + "/OUT_STEREO_PREVIEW",
    PIPELINE_PATH + "/OUT_INTERACTION_DEBUG",
)

OUTPUTS = (
    "OUT_POSITION",
    "OUT_COLOR",
    "OUT_INTERACTION",
    "OUT_INSTALLATION",
    "OUT_TRIPLE_WRAP",
    "OUT_TRIPLE_ARTISTIC",
    "OUT_DISPLAY_ACTIVE",
    "OUT_TRIPLE_WRAP_LEFT",
    "OUT_TRIPLE_WRAP_CENTER",
    "OUT_TRIPLE_WRAP_RIGHT",
    "OUT_TRIPLE_ARTISTIC_LEFT",
    "OUT_TRIPLE_ARTISTIC_CENTER",
    "OUT_TRIPLE_ARTISTIC_RIGHT",
    "OUT_LEFT_EYE",
    "OUT_RIGHT_EYE",
    "OUT_STEREO_PREVIEW",
    "OUT_INTERACTION_DEBUG",
)

EXPECTED_OPERATOR_TYPES = {
    ROOT_PATH + "/CONFIG": ("base",),
    ROOT_PATH + "/STARTUP/runtime_helpers": ("text",),
    PIPELINE_PATH: ("base",),
    PIPELINE_PATH + "/SOURCES": ("base",),
    PIPELINE_PATH + "/ROLE_BRIDGE": ("base",),
    PIPELINE_PATH + "/RECONSTRUCTION": ("base",),
    PIPELINE_PATH + "/SENSOR_INTERACTION": ("base",),
    PIPELINE_PATH + "/TEMPORAL_WORLD": ("base",),
    PIPELINE_PATH + "/COMPLETION": ("base",),
    PIPELINE_PATH + "/POINT_RENDER": ("base",),
    PIPELINE_PATH + "/TRIPLE_DISPLAY": ("base",),
    PIPELINE_PATH + "/TELEMETRY/LIVE_HEALTH": ("table",),
    PIPELINE_PATH + "/OUT_POSITION": ("null",),
    PIPELINE_PATH + "/OUT_COLOR": ("null",),
    PIPELINE_PATH + "/OUT_INTERACTION": ("null",),
    PIPELINE_PATH + "/OUT_INSTALLATION": ("null",),
    PIPELINE_PATH + "/OUT_TRIPLE_WRAP": ("null",),
    PIPELINE_PATH + "/OUT_TRIPLE_ARTISTIC": ("null",),
    PIPELINE_PATH + "/OUT_DISPLAY_ACTIVE": ("null",),
    PIPELINE_PATH + "/OUT_TRIPLE_WRAP_LEFT": ("null",),
    PIPELINE_PATH + "/OUT_TRIPLE_WRAP_CENTER": ("null",),
    PIPELINE_PATH + "/OUT_TRIPLE_WRAP_RIGHT": ("null",),
    PIPELINE_PATH + "/OUT_TRIPLE_ARTISTIC_LEFT": ("null",),
    PIPELINE_PATH + "/OUT_TRIPLE_ARTISTIC_CENTER": ("null",),
    PIPELINE_PATH + "/OUT_TRIPLE_ARTISTIC_RIGHT": ("null",),
    PIPELINE_PATH + "/OUT_LEFT_EYE": ("null",),
    PIPELINE_PATH + "/OUT_RIGHT_EYE": ("null",),
    PIPELINE_PATH + "/OUT_STEREO_PREVIEW": ("null",),
    PIPELINE_PATH + "/OUT_INTERACTION_DEBUG": ("null",),
}

MAX_SIGNAL_SAMPLES = 262144
MIN_CAPTURE_BYTES = 128


def _op(path):
    resolver = globals().get("op")
    if resolver is None:
        try:
            import builtins

            resolver = getattr(builtins, "op", None)
        except Exception:
            pass
    if resolver is None:
        try:
            import __main__

            resolver = getattr(__main__, "op", None)
        except Exception:
            pass
    if resolver is None:
        try:
            import td

            resolver = getattr(td, "op", None)
        except Exception:
            pass
    if resolver is None:
        raise RuntimeError("TouchDesigner op() is unavailable")
    return resolver(path)


def _parameter(node, name):
    if node is None:
        return None
    try:
        return getattr(node.par, name)
    except Exception:
        pass
    try:
        wanted = str(name).lower()
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


def _messages(node, method_name):
    method = getattr(node, method_name, None)
    if not callable(method):
        return []
    try:
        result = method()
    except Exception as exc:
        return ["%s() inspection failed: %s" % (method_name, exc)]
    if not result:
        return []
    if isinstance(result, str):
        return [line.strip() for line in result.splitlines() if line.strip()]
    try:
        return [str(item).strip() for item in result if str(item).strip()]
    except Exception:
        return [str(result)]


def _walk(root, limit=10000):
    pending = [root]
    seen = set()
    result = []
    while pending:
        node = pending.pop()
        path = str(getattr(node, "path", ""))
        if path in seen:
            continue
        seen.add(path)
        result.append(node)
        if len(result) > limit:
            raise RuntimeError("managed network exceeded validation node limit")
        try:
            pending.extend(list(node.children))
        except Exception:
            pass
    return result


def _cook(node):
    method = getattr(node, "cook", None)
    if not callable(method):
        return
    try:
        method(force=True)
    except TypeError:
        method()


def _top_spec(node):
    result = {
        "path": str(getattr(node, "path", "")),
        "type": str(getattr(node, "type", "")),
    }
    for key in ("width", "height", "depth", "pixelFormat"):
        try:
            value = getattr(node, key)
        except Exception:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
        else:
            result[key] = str(value)
    return result


def _runtime_state(root):
    try:
        value = root.fetch("runtime_state", {})
    except Exception:
        value = {}
    return dict(value) if isinstance(value, dict) else {}


def _truthy(value):
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _positive_state_int(state, name):
    value = state.get(name)
    if isinstance(value, bool):
        raise ValueError("%s is not an integer" % name)
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("%s is not positive" % name)
    return parsed


def _active_output_dimensions(state):
    expected = {}
    if _truthy(state.get("world_active")):
        geometry = _positive_state_int(state, "geometry_resolution")
        for name in (
            "OUT_POSITION",
            "OUT_COLOR",
            "OUT_INTERACTION",
            "OUT_INTERACTION_DEBUG",
        ):
            expected[name] = (geometry, geometry)
        if _truthy(state.get("installation_active")):
            surface_width = _positive_state_int(
                state, "triple_surface_width")
            surface_height = _positive_state_int(
                state, "triple_surface_height")
            mosaic_size = (surface_width * 3, surface_height)
            expected["OUT_INSTALLATION"] = (
                _positive_state_int(state, "installation_width"),
                _positive_state_int(state, "installation_height"),
            )
            expected["OUT_TRIPLE_WRAP"] = mosaic_size
            expected["OUT_TRIPLE_ARTISTIC"] = mosaic_size
            display_mode = str(
                state.get("display_mode", "single")).strip().casefold()
            if display_mode == "single":
                expected["OUT_DISPLAY_ACTIVE"] = expected["OUT_INSTALLATION"]
            elif display_mode in (
                    "panoramic_wrap", "artistic_multi_angle"):
                expected["OUT_DISPLAY_ACTIVE"] = mosaic_size
            else:
                raise ValueError(
                    "display_mode is unsupported: %r" % display_mode)
            for mode in ("WRAP", "ARTISTIC"):
                for side in ("LEFT", "CENTER", "RIGHT"):
                    expected[
                        "OUT_TRIPLE_%s_%s" % (mode, side)
                    ] = (surface_width, surface_height)
        if _truthy(state.get("vr_active")):
            stereo_width = _positive_state_int(state, "stereo_width")
            stereo_height = _positive_state_int(state, "stereo_height")
            eye_width = max(64, stereo_width // 2)
            expected["OUT_LEFT_EYE"] = (eye_width, stereo_height)
            expected["OUT_RIGHT_EYE"] = (eye_width, stereo_height)
            expected["OUT_STEREO_PREVIEW"] = (stereo_width, stereo_height)
    return expected


def _experience_activation_contract(experience):
    name = str(experience).strip().casefold()
    modes = {
        "installation": {
            "world_active": True,
            "installation_active": True,
            "vr_active": False,
        },
        "vr": {
            "world_active": True,
            "installation_active": False,
            "vr_active": True,
        },
        "combined": {
            "world_active": True,
            "installation_active": True,
            "vr_active": True,
        },
    }
    if name not in modes:
        raise ValueError("unsupported expected experience %r" % experience)
    return modes[name]


def _active_signal_outputs(state):
    result = []
    if _truthy(state.get("installation_active")):
        result.extend((
            "OUT_INSTALLATION",
            "OUT_TRIPLE_WRAP",
            "OUT_TRIPLE_ARTISTIC",
            "OUT_DISPLAY_ACTIVE",
        ))
    if _truthy(state.get("vr_active")):
        result.extend(("OUT_LEFT_EYE", "OUT_RIGHT_EYE", "OUT_STEREO_PREVIEW"))
    return tuple(result)


def _active_capture_outputs(state):
    result = []
    if _truthy(state.get("installation_active")):
        result.extend((
            "OUT_INSTALLATION",
            "OUT_TRIPLE_WRAP",
            "OUT_TRIPLE_ARTISTIC",
            "OUT_DISPLAY_ACTIVE",
        ))
    if _truthy(state.get("vr_active")):
        result.append("OUT_STEREO_PREVIEW")
    return tuple(result)


def _signal_sample(node):
    """Read a bounded RGB sample and reject blank/non-finite visual output."""

    try:
        import numpy
    except Exception as exc:
        raise RuntimeError("NumPy is unavailable: %s" % exc)
    method = getattr(node, "numpyArray", None)
    if not callable(method):
        raise RuntimeError("TOP does not expose numpyArray()")
    try:
        array = method(delayed=False)
    except TypeError:
        array = method()
    if array is None:
        raise RuntimeError("TOP readback returned no pixels")
    values = numpy.asarray(array)
    if values.ndim < 2 or values.size == 0:
        raise RuntimeError("TOP readback has no image pixels")
    if values.ndim == 2:
        rgb = values.reshape(-1, 1)
    else:
        channels = int(values.shape[-1]) if values.ndim >= 3 else 1
        rgb = values[..., : min(3, channels)].reshape(-1, min(3, channels))
    stride = max(1, int(rgb.shape[0]) // MAX_SIGNAL_SAMPLES)
    sampled = rgb[::stride][:MAX_SIGNAL_SAMPLES]
    finite = bool(numpy.isfinite(sampled).all())
    if not finite:
        raise RuntimeError("TOP contains non-finite RGB samples")
    minimum = float(sampled.min())
    maximum = float(sampled.max())
    mean = float(sampled.mean())
    span = maximum - minimum
    stats = {
        "shape": [int(item) for item in values.shape],
        "sample_count": int(sampled.shape[0]),
        "rgb_min": minimum,
        "rgb_max": maximum,
        "rgb_mean": mean,
        "rgb_range": span,
        "has_signal": maximum > 1e-5 and span > 1e-6,
    }
    return stats, sampled


def _signal_stats(node):
    return _signal_sample(node)[0]


def _is_glsl(node):
    return "glsl" in str(getattr(node, "type", "")).casefold()


def _is_shader_compile_error(message):
    normalized = str(message).casefold()
    return "compil" in normalized and "error" in normalized


def _sensor_disabled_contract():
    """Exercise the real disabled route and restore the artist's sensor mode."""

    sensor_path = PIPELINE_PATH + "/SENSOR_INTERACTION"
    sensor = _op(sensor_path)
    circle = _op(sensor_path + "/SIMULATED_SENSOR_MASK")
    failures = []
    details = {"outputs": {}}
    if sensor is None:
        return False, {"failures": ["SENSOR_INTERACTION is missing"]}
    if circle is None:
        failures.append("SIMULATED_SENSOR_MASK Circle TOP is missing")
    else:
        for name in ("radiusx", "radiusy", "centerx", "centery"):
            if _parameter(circle, name) is None:
                failures.append("Circle TOP parameter %s is missing" % name)
        radius = (_value(circle, "radiusx"), _value(circle, "radiusy"))
        center = (_value(circle, "centerx"), _value(circle, "centery"))
        details["circle"] = {"radius": list(radius), "center": list(center)}
        try:
            if any(abs(float(value) - 0.16) > 1e-5 for value in radius):
                failures.append("Circle TOP radius is not 0.16 in both axes")
            if (abs(float(center[0])) > 0.241 or
                    abs(float(center[1])) > 0.181):
                failures.append("Circle TOP center is outside its zero-centered range")
        except (TypeError, ValueError):
            failures.append("Circle TOP radius/center parameters are not numeric")

    mode = _parameter(sensor, "Mode")
    if mode is None:
        failures.append("sensor Mode parameter is missing")
        return False, {"failures": failures, **details}
    try:
        menu_names = [str(item) for item in mode.menuNames]
    except Exception:
        menu_names = []
    details["menu_names"] = menu_names
    if "disabled" not in menu_names:
        failures.append("sensor Mode menu has no disabled route")

    previous_mode = _value(sensor, "Mode", "simulated")
    output_names = ("OUT_SENSOR_MASK", "OUT_SENSOR_POSITION", "OUT_INTERACTION")
    try:
        mode.val = "disabled"
        if str(_value(sensor, "Mode", "")).casefold() != "disabled":
            failures.append("sensor Mode could not be switched to disabled")
        for name in output_names:
            node = _op(sensor_path + "/" + name)
            if node is None:
                failures.append("disabled sensor output %s is missing" % name)
                continue
            try:
                _cook(node)
                stats, _sampled = _signal_sample(node)
                details["outputs"][name] = stats
                magnitude = max(abs(stats["rgb_min"]), abs(stats["rgb_max"]))
                if magnitude > 1e-6:
                    failures.append("%s is nonzero while sensor is disabled" % name)
            except Exception as exc:
                failures.append("%s disabled probe failed: %s" % (name, exc))
    finally:
        try:
            mode.val = previous_mode
        except Exception as exc:
            failures.append("sensor Mode restore failed: %s" % exc)
        for name in output_names:
            node = _op(sensor_path + "/" + name)
            if node is not None:
                try:
                    _cook(node)
                except Exception:
                    pass

    details["restored_mode"] = _value(sensor, "Mode", None)
    details["failures"] = failures
    return not failures, details


def _atomic_json(path, payload):
    absolute = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path))))
    directory = os.path.dirname(absolute)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    temporary = "%s.tmp-%d" % (absolute, os.getpid())
    with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, absolute)
    return absolute


def validate(
    expected_build="1.2.1",
    report_path=None,
    capture_dir=None,
    expected_experience=None,
):
    """Validate the generated network and return a JSON-compatible report."""

    root = _op(ROOT_PATH)
    pipeline = _op(PIPELINE_PATH)
    if root is None or pipeline is None:
        raise RuntimeError("FlexGPU v1.2.1 network is not built")

    checks = []

    def check(name, passed, details=None):
        item = {"name": name, "status": "pass" if passed else "fail"}
        if details not in (None, "", [], {}):
            item["details"] = details
        checks.append(item)
        return passed

    actual_build = str(_value(pipeline, "Buildversion", ""))
    check(
        "build_version",
        actual_build == str(expected_build),
        {"expected": str(expected_build), "actual": actual_build},
    )

    missing = [path for path in REQUIRED_OPERATORS if _op(path) is None]
    check("required_operators", not missing, {"missing": missing})

    type_mismatches = {}
    for path, permitted in EXPECTED_OPERATOR_TYPES.items():
        node = _op(path)
        if node is None:
            continue
        actual = str(getattr(node, "type", "")).strip().casefold()
        if actual not in permitted:
            type_mismatches[path] = {
                "expected": list(permitted),
                "actual": actual,
            }
    check("managed_operator_types", not type_mismatches, type_mismatches)

    state = _runtime_state(root)
    state_failures = []
    if not state:
        state_failures.append("runtime_state is missing")
    actual_experience = str(state.get("experience", ""))
    for flag in ("world_active", "installation_active", "vr_active"):
        if flag not in state:
            state_failures.append("%s is missing" % flag)
    if expected_experience is not None:
        expected_name = str(expected_experience).strip().casefold()
        if actual_experience != expected_name:
            state_failures.append(
                "experience is %r, expected %r" %
                (actual_experience, expected_name)
            )
        try:
            activation_contract = _experience_activation_contract(expected_name)
        except ValueError as exc:
            state_failures.append(str(exc))
        else:
            for flag, expected_value in activation_contract.items():
                actual_value = _truthy(state.get(flag))
                if actual_value != expected_value:
                    state_failures.append(
                        "%s is %s, expected %s for %s" %
                        (flag, actual_value, expected_value, expected_name)
                    )
    try:
        expected_dimensions = _active_output_dimensions(state)
    except Exception as exc:
        expected_dimensions = {}
        state_failures.append("active output contract is invalid: %s" % exc)
    check(
        "runtime_state",
        not state_failures,
        {
            "failures": state_failures,
            "experience": actual_experience,
            "world_active": state.get("world_active"),
            "installation_active": state.get("installation_active"),
            "vr_active": state.get("vr_active"),
        },
    )

    managed_nodes = _walk(root)
    shader_cook_failures = {}
    for node in managed_nodes:
        if not _is_glsl(node):
            continue
        path = str(getattr(node, "path", ""))
        try:
            _cook(node)
        except Exception as exc:
            shader_cook_failures[path] = str(exc)
    check("managed_shader_cook", not shader_cook_failures, shader_cook_failures)

    try:
        sensor_disabled_passed, sensor_disabled_details = (
            _sensor_disabled_contract())
    except Exception as exc:
        sensor_disabled_passed = False
        sensor_disabled_details = {"failures": [str(exc)]}
    check(
        "sensor_disabled_contract",
        sensor_disabled_passed,
        sensor_disabled_details,
    )

    output_specs = {}
    output_errors = {}
    for name in OUTPUTS:
        node = _op(PIPELINE_PATH + "/" + name)
        if node is None:
            continue
        try:
            _cook(node)
        except Exception as exc:
            output_errors[name] = ["cook failed: %s" % exc]
        errors = _messages(node, "errors")
        if errors:
            output_errors.setdefault(name, []).extend(errors)
        output_specs[name] = _top_spec(node)
    check("output_cook", not output_errors, output_errors)
    check(
        "output_dimensions",
        all(
            int(spec.get("width", 0) or 0) > 0 and
            int(spec.get("height", 0) or 0) > 0
            for spec in output_specs.values()
        ) and len(output_specs) == len(OUTPUTS),
        output_specs,
    )

    exact_dimension_failures = {}
    for name, expected in expected_dimensions.items():
        spec = output_specs.get(name, {})
        actual = (
            int(spec.get("width", 0) or 0),
            int(spec.get("height", 0) or 0),
        )
        if actual != expected:
            exact_dimension_failures[name] = {
                "expected": list(expected),
                "actual": list(actual),
            }
    check(
        "active_output_dimensions",
        not exact_dimension_failures and bool(expected_dimensions),
        {
            "expected": dict(
                (name, list(value)) for name, value in expected_dimensions.items()
            ),
            "failures": exact_dimension_failures,
        },
    )

    signals = {}
    signal_samples = {}
    signal_failures = {}
    signal_outputs = _active_signal_outputs(state)
    for name in signal_outputs:
        node = _op(PIPELINE_PATH + "/" + name)
        if node is None:
            signal_failures[name] = "active visual output is missing"
            continue
        try:
            stats, sampled = _signal_sample(node)
            signals[name] = stats
            signal_samples[name] = sampled
            if not stats["has_signal"]:
                signal_failures[name] = "active visual output is blank or constant"
        except Exception as exc:
            signal_failures[name] = str(exc)
    check(
        "active_visual_signal",
        bool(signal_outputs) and not signal_failures,
        {"active": list(signal_outputs), "failures": signal_failures, "stats": signals},
    )

    stereo_difference = {}
    stereo_difference_failures = []
    if _truthy(state.get("vr_active")):
        left_sample = signal_samples.get("OUT_LEFT_EYE")
        right_sample = signal_samples.get("OUT_RIGHT_EYE")
        if left_sample is None or right_sample is None:
            stereo_difference_failures.append("left/right eye readback is unavailable")
        elif tuple(left_sample.shape) != tuple(right_sample.shape):
            stereo_difference_failures.append("left/right eye sample shapes differ")
        else:
            difference = abs(left_sample - right_sample)
            stereo_difference = {
                "mean_absolute_rgb_difference": float(difference.mean()),
                "max_absolute_rgb_difference": float(difference.max()),
            }
            if (
                stereo_difference["mean_absolute_rgb_difference"] <= 1e-7
                or stereo_difference["max_absolute_rgb_difference"] <= 1e-6
            ):
                stereo_difference_failures.append(
                    "left/right eye images are identical or materially indistinguishable"
                )
    check(
        "stereo_eye_difference",
        not _truthy(state.get("vr_active")) or not stereo_difference_failures,
        {
            "failures": stereo_difference_failures,
            "difference": stereo_difference,
        },
    )

    render_root = PIPELINE_PATH + "/POINT_RENDER/"
    metric_render_names = (
        "METRIC_RENDER_CENTER",
        "METRIC_RENDER_LEFT_EYE",
        "METRIC_RENDER_RIGHT_EYE",
    )
    metric_camera_names = (
        "CAMERA_CENTER_METRIC",
        "CAMERA_LEFT_METRIC",
        "CAMERA_RIGHT_METRIC",
    )
    metric_renders = {
        name: _op(render_root + name) for name in metric_render_names
    }
    metric_cameras = {
        name: _op(render_root + name) for name in metric_camera_names
    }
    mono_fallback = _op(render_root + "METRIC_MONO_FALLBACK")
    render_failures = []
    required_metric_renders = []
    required_metric_cameras = []
    if _truthy(state.get("installation_active")):
        required_metric_renders.append("METRIC_RENDER_CENTER")
        required_metric_cameras.append("CAMERA_CENTER_METRIC")
    if _truthy(state.get("vr_active")):
        required_metric_renders.extend(
            ("METRIC_RENDER_LEFT_EYE", "METRIC_RENDER_RIGHT_EYE")
        )
        required_metric_cameras.extend(
            ("CAMERA_LEFT_METRIC", "CAMERA_RIGHT_METRIC")
        )
    missing_renders = [
        name for name in required_metric_renders if metric_renders.get(name) is None
    ]
    missing_cameras = [
        name for name in required_metric_cameras if metric_cameras.get(name) is None
    ]
    if missing_renders or missing_cameras:
        render_failures.append({
            "missing_active_metric_renders": missing_renders,
            "missing_active_metric_cameras": missing_cameras,
            "mono_fallback_is_not_stereo": mono_fallback is not None,
        })
    for name, node in metric_renders.items():
        if node is not None and bool(_value(node, "normalizegeo", False)):
            render_failures.append({"normalized_metric_render": name})
    if mono_fallback is not None and bool(_value(mono_fallback, "normalizegeo", False)):
        render_failures.append({"normalized_mono_fallback": True})
    for name, node in metric_cameras.items():
        if node is None:
            continue
        rotations = tuple(float(_value(node, axis, 0.0) or 0.0) for axis in ("rx", "ry", "rz"))
        if any(abs(value) > 1e-6 for value in rotations):
            render_failures.append({"toe_in_camera": name, "rotation": rotations})
    camera_translations = {}
    if _truthy(state.get("vr_active")):
        point_render = _op(PIPELINE_PATH + "/POINT_RENDER")
        try:
            ipd = float(_value(point_render, "Ipdmetres", 0.0) or 0.0)
            left = metric_cameras.get("CAMERA_LEFT_METRIC")
            right = metric_cameras.get("CAMERA_RIGHT_METRIC")
            left_translation = tuple(
                float(_value(left, axis, 0.0) or 0.0) for axis in ("tx", "ty", "tz")
            )
            right_translation = tuple(
                float(_value(right, axis, 0.0) or 0.0) for axis in ("tx", "ty", "tz")
            )
            camera_translations = {
                "ipd_metres": ipd,
                "left": list(left_translation),
                "right": list(right_translation),
            }
            tolerance = max(1e-6, abs(ipd) * 1e-4)
            valid_translation = (
                math.isfinite(ipd)
                and 0.0 < ipd <= 0.2
                and all(math.isfinite(value) for value in left_translation + right_translation)
                and abs(left_translation[0] + ipd * 0.5) <= tolerance
                and abs(right_translation[0] - ipd * 0.5) <= tolerance
                and all(abs(value) <= tolerance for value in left_translation[1:])
                and all(abs(value) <= tolerance for value in right_translation[1:])
            )
            if not valid_translation:
                render_failures.append({
                    "invalid_parallel_eye_translation": camera_translations,
                })
        except Exception as exc:
            render_failures.append({"eye_translation_inspection_failed": str(exc)})
    check(
        "metric_rendering",
        not render_failures,
        {
            "failures": render_failures,
            "metric_renders": [name for name, node in metric_renders.items() if node is not None],
            "metric_cameras": [name for name, node in metric_cameras.items() if node is not None],
            "mono_fallback": mono_fallback is not None,
            "camera_translations": camera_translations,
        },
    )

    triple_failures = []
    triple_camera_details = {}
    if _truthy(state.get("installation_active")):
        point_render = _op(PIPELINE_PATH + "/POINT_RENDER")
        expected_wrap_yaw = float(
            _value(point_render, "Wrapyawdegrees", 45.0) or 0.0)
        expected_art_yaw = float(
            _value(point_render, "Artisticyawdegrees", 18.0) or 0.0)
        expected_art_offset = float(
            _value(point_render, "Artisticoffsetmetres", 0.45) or 0.0)
        for mode, expected_transforms in (
            ("WRAP", {
                # TouchDesigner positive camera Y rotation looks toward the
                # audience's left while the camera faces -Z.
                "LEFT":(0.0, expected_wrap_yaw),
                "CENTER":(0.0, 0.0),
                "RIGHT":(0.0, -expected_wrap_yaw),
            }),
            ("ARTISTIC", {
                "LEFT":(-expected_art_offset, -expected_art_yaw),
                "CENTER":(0.0, 0.0),
                "RIGHT":(expected_art_offset, expected_art_yaw),
            }),
        ):
            for side, (expected_tx, expected_ry) in expected_transforms.items():
                camera_name = "CAMERA_%s_%s" % (mode, side)
                render_name = "METRIC_RENDER_%s_%s" % (mode, side)
                camera = _op(render_root + camera_name)
                render_node = _op(render_root + render_name)
                if camera is None or render_node is None:
                    triple_failures.append({
                        "missing_triple_view": {
                            "camera":camera_name if camera is None else "",
                            "render":render_name if render_node is None else "",
                        },
                    })
                    continue
                transform = tuple(float(
                    _value(camera, axis, 0.0) or 0.0)
                    for axis in ("tx", "ty", "tz", "rx", "ry", "rz"))
                triple_camera_details[camera_name] = list(transform)
                tolerance = 1e-4
                expected = (
                    expected_tx, 0.0, 0.0, 0.0, expected_ry, 0.0)
                if (
                    not all(math.isfinite(value) for value in transform)
                    or any(
                        abs(actual - target) > tolerance
                        for actual, target in zip(transform, expected))
                ):
                    triple_failures.append({
                        "invalid_triple_camera_transform": {
                            "camera":camera_name,
                            "expected":list(expected),
                            "actual":list(transform),
                        },
                    })
    check(
        "triple_display_cameras",
        not _truthy(state.get("installation_active")) or not triple_failures,
        {
            "failures":triple_failures,
            "cameras":triple_camera_details,
            "panoramic_contract":"shared origin with different yaw",
            "artistic_contract":"translated and rotated side cameras",
        },
    )

    managed_errors = {}
    managed_warnings = {}
    for node in managed_nodes:
        path = str(getattr(node, "path", ""))
        errors = _messages(node, "errors")
        warnings = _messages(node, "warnings")
        if errors:
            managed_errors[path] = errors
        if warnings:
            managed_warnings[path] = warnings
    check("managed_operator_errors", not managed_errors, managed_errors)
    shader_compile_failures = {
        path: warnings
        for path, warnings in managed_warnings.items()
        if any(_is_shader_compile_error(message) for message in warnings)
    }
    check(
        "managed_shader_compilation",
        not shader_compile_failures,
        shader_compile_failures,
    )

    captures = {}
    if capture_dir:
        absolute_capture = os.path.abspath(
            os.path.expanduser(os.path.expandvars(str(capture_dir)))
        )
        if not os.path.isdir(absolute_capture):
            os.makedirs(absolute_capture)
        capture_failures = {}
        capture_outputs = _active_capture_outputs(state)
        for name in capture_outputs:
            node = _op(PIPELINE_PATH + "/" + name)
            if node is None:
                capture_failures[name] = "active visual output is missing"
                continue
            output = os.path.join(absolute_capture, name.lower() + ".png")
            try:
                # The validator owns these deterministic filenames. Remove a
                # previous run first so a no-op/failed save cannot be mistaken
                # for a fresh capture.
                if os.path.lexists(output):
                    os.unlink(output)
                node.save(output)
                size = os.path.getsize(output) if os.path.isfile(output) else 0
                captures[name] = {"path": output, "bytes": int(size)}
                if size < MIN_CAPTURE_BYTES:
                    capture_failures[name] = (
                        "capture is missing or smaller than %d bytes" %
                        MIN_CAPTURE_BYTES
                    )
            except Exception as exc:
                capture_failures[name] = "capture failed: %s" % exc
        check(
            "synthetic_captures",
            bool(capture_outputs) and not capture_failures,
            {"captures": captures, "failures": capture_failures},
        )

    report = {
        "version": VALIDATION_VERSION,
        "captured_ns": time.time_ns(),
        "status": "pass" if all(item["status"] == "pass" for item in checks) else "fail",
        "build_version": actual_build,
        "checks": checks,
        "outputs": output_specs,
        "warnings": managed_warnings,
        "runtime": {
            "experience": actual_experience,
            "world_active": state.get("world_active"),
            "installation_active": state.get("installation_active"),
            "vr_active": state.get("vr_active"),
        },
        "signals": signals,
        "captures": captures,
    }
    if report_path:
        absolute_report = os.path.abspath(
            os.path.expanduser(os.path.expandvars(str(report_path)))
        )
        report["report_path"] = absolute_report
        _atomic_json(absolute_report, report)
    return report


if __name__ == "__main__":
    raise SystemExit("Run validate() inside TouchDesigner's Python runtime")
