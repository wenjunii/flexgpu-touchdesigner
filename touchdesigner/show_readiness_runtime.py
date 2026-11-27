"""Embedded, passive operator status and bounded rehearsal sampling.

No network probes, image readbacks, force cooks, subprocesses or control
writes. Only the observer's own table is updated by the frame callback.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import math
import time


BRIDGES = {"moge2": "MOGE2_BRIDGE", "depth_anything": "DEPTH_ANYTHING_GEOMETRY_BRIDGE"}
PROVIDERS = {"moge2": "MoGe-2", "depth_anything": "Depth Anything"}
WALLS = {
    "single": ("OUT_INSTALLATION",),
    "panoramic_wrap": tuple("OUT_TRIPLE_WRAP_" + side for side in ("LEFT", "CENTER", "RIGHT")),
    "artistic_multi_angle": tuple("OUT_TRIPLE_ARTISTIC_" + side for side in ("LEFT", "CENTER", "RIGHT")),
}
PROFILES = ("3080ti_16gb", "4090", "5090")
INTERVAL_SECONDS = 1.0
REPORT_VERSION = "flexgpu-show-rehearsal/v1"


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _integer(value):
    value = _number(value)
    return int(value) if value is not None and value.is_integer() else None


def _par(node, name):
    try:
        return getattr(node.par, name).eval()
    except (AttributeError, TypeError, ValueError):
        return None


def _known_flag(value):
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    return None


def _cell(dat, metric):
    try:
        cell = dat[metric, 1]
        return str(cell) if cell is not None else ""
    except (AttributeError, IndexError, TypeError):
        return ""


def _choice(value, valid):
    return value if isinstance(value, str) and value in valid else "unknown"


def _generator_flags(host):
    # The documented custom activity toggles on the selected root are the
    # only private-component fields read; never traverse a payload's network.
    slot = host.op("PAYLOAD_SLOT")
    if slot is None or _par(host, "Source") != "slot":
        return "activity unknown"
    name = _par(host, "Payload")
    if isinstance(name, str) and name and name.isidentifier():
        candidate = slot.op(name)
        if candidate is None or candidate.parent() != slot or candidate.family != "COMP":
            return "not loaded"
    elif not name and not host.fetch("slot_selection_cleared", False):
        candidates = [node for node in slot.children if node.family == "COMP"]
        if len(candidates) != 1:
            return "not loaded" if not candidates else "activity unknown"
        candidate = candidates[0]
    else:
        return "not loaded"
    flags = []
    for key in ("Serveractive", "Streamactive"):
        p = getattr(candidate.par, key, None)
        if p is None or not getattr(p, "isCustom", False) or not getattr(p, "isToggle", False):
            return "activity unknown"
        flag = _known_flag(p.eval())
        if flag is None:
            return "activity unknown"
        flags.append(flag)
    return {
        (True, True): "streaming", (True, False): "server only",
        (False, False): "stopped", (False, True): "activity unknown",
    }[tuple(flags)]


def _wall(node, passive):
    if node is None or passive is None:
        return (None, None, None)
    try:
        p = passive(node)
        frame = _integer(p.cookAbsFrame)
        # An uncooked TOP can still report placeholder dimensions (128x128).
        if frame is None:
            return (None, None, None)
        return (_integer(p.width), _integer(p.height), frame)
    except (AttributeError, TypeError, ValueError):
        return (None, None, None)


def observe(root, *, passive, lookup):
    """Read actual route selectors rather than inferring from intended config."""
    pipe = root.op("WORKING_PIPELINE")
    show = pipe.op("SHOW_CONTROL")
    sources = pipe.op("SOURCES")
    adapter = sources.op("STREAMDIFFUSION_ADAPTER")
    host = adapter.op("STREAMDIFFUSION_HOST")
    role = pipe.op("ROLE_BRIDGE")
    mode = _choice(_par(pipe, "Displaymode"), WALLS)
    show_mode = _choice(_par(show, "Displaymode"), WALLS)
    provider = _choice(_par(adapter, "Geometrysource"), BRIDGES)
    expected_provider = _choice(_par(show, "Geometryprovider"), BRIDGES)
    profile = _choice(_par(show, "Qualityprofile"), PROFILES)
    bridge = adapter.op(BRIDGES[provider]) if provider in BRIDGES else None
    stats = bridge.op("STATUS") if bridge is not None else None
    stage = _choice(_par(host, "Stage"), (
        "select_generator", "configure_rgb", "preview", "connected", "offloading",
        "offloaded", "offloaded_removed", "offload_unconfirmed", "invalid_source",
    ))
    audio_on = _known_flag(_par(show, "Audioenabled"))
    out_on = _known_flag(_par(adapter.op("audio_out"), "active"))
    audio_mode = _choice(_par(show, "Audiosource"), ("voices", "soundscape", "combined"))
    switch = adapter.op("audiosource_switch")
    audio_index = {"voices": 0, "soundscape": 1, "combined": 2}.get(audio_mode)
    if audio_on is False and out_on is not True:
        audio = "muted"
    elif audio_on is True and out_on is True and audio_index is not None and _integer(_par(switch, "index")) == audio_index:
        audio = audio_mode + " routed"
    elif audio_on is None and out_on is None:
        audio = "not configured"
    else:
        audio = "check route"
    sensor = pipe.op("SENSOR_INTERACTION")
    sensor_mode = _choice(_par(sensor, "Mode"), ("disabled", "simulated", "depth_sensor"))
    if sensor_mode == "depth_sensor":
        provider_name = _choice(_par(show, "Camerasensorsource"), ("femto_mega", "depth_anything"))
        sensor_bridge = (
            sources.op("FEMTO_MEGA_ADAPTER") if provider_name == "femto_mega" else
            sensor.op("DEPTH_SENSOR_ADAPTER/DEPTH_ANYTHING_BRIDGE") if provider_name == "depth_anything" else None
        )
        interaction = "result valid" if _known_flag(_par(sensor_bridge, "Resultvalid")) is True else "waiting for depth sensor"
    else:
        interaction = {"disabled": "off", "simulated": "simulated"}.get(sensor_mode, "not verified")
    live_health = pipe.op("TELEMETRY/LIVE_HEALTH")
    return {
        "profile": profile, "mode": mode, "show_mode": show_mode,
        "experience": _choice(_par(show, "Experience"), ("installation", "vr", "combined")),
        "transport": _choice(_par(role, "Mode"), ("local", "send_shared", "receive_shared", "send_tcp", "receive_tcp")),
        "source_routes": tuple(_integer(_par(sources.op(name), "index")) for name in ("RGB_SOURCE", "DEPTH_SOURCE")),
        "timeline": _known_flag(_par(lookup("/local/time"), "play")),
        "stage": stage, "host_present": host is not None,
        "request_rgb": _known_flag(_par(host, "Streamenabled")),
        "rgb_gate": _integer(_par(host.op("STREAM_GATE"), "index")) if host is not None else None,
        "generator": _generator_flags(host) if host is not None else "activity unknown",
        "provider": provider, "show_provider": expected_provider, "bridge_present": bridge is not None,
        "bridge_profile": _choice(_par(bridge, "Profile"), PROFILES),
        "bridge_enabled": _known_flag(_par(bridge, "Enabled")),
        "bridge_state": _choice(_cell(stats, "state"), ("running", "stopped", "stale", "disabled", "error", "source_paused")),
        "bridge_error": _choice(_cell(stats, "last_error"), ("none", "send_failed", "receiver_start_failed", "config_invalid")),
        "atlas_published": _known_flag(_par(bridge, "Resultvalid")),
        "fresh_result": _cell(stats, "result_fresh").lower() == "true",
        "frame_id": _integer(_cell(stats, "latest_source_frame_id")),
        "geometry_age_ms": _number(_cell(stats, "result_age_ms")),
        "capture_fps": _number(_par(bridge, "Capturefps")),
        "routed_depth": _integer(_par(adapter.op(
            "GENERATED_GEOMETRY_DEPTH_ROUTE" if provider == "depth_anything" else "MOGE2_DEPTH_ROUTE"), "index")),
        "source_valid": _known_flag(_par(role, "Framevalid")),
        "epoch": host.fetch("stream_epoch", "") if host is not None else "",
        "wall_size": (_integer(_par(show, "Wallwidth")), _integer(_par(show, "Wallheight"))),
        "walls": {name: _wall(pipe.op(name), passive) for name in WALLS.get(mode, ())},
        "audio": audio, "interaction": interaction,
        "frame_time_ms": _number(_cell(live_health, "frame_time_ms")),
    }


class ReadinessMonitor:
    def __init__(self):
        self._progress = {}

    def _advancing(self, name, generation, token, now, ttl):
        previous = self._progress.get(name)
        changed = None
        if token is not None and previous is not None and previous[0] == generation:
            _, old_token, previous_change = previous
            if old_token is not None and token > old_token:
                changed = now
            elif token == old_token:
                changed = previous_change
        self._progress[name] = generation, token, changed
        return changed is not None and 0 <= now - changed <= ttl

    def evaluate(self, data, now):
        provider = data["provider"]
        fps = data["capture_fps"]
        ttl = max(1.0, 3.0 / fps) if fps and 0.1 <= fps <= 60 else None
        size, active = data["wall_size"], data["walls"]
        mode = data["mode"]
        valid_layout = bool(active) and all(size) and set(active) == set(WALLS.get(mode, ()))
        sizes_ok = valid_layout and all(v[:2] == size for v in active.values())
        mismatches = [k for k, v in active.items() if all(v[:2]) and v[:2] != size]
        wall_progress = [
            self._advancing("wall:" + name, (mode, size), values[2], now, 2.5)
            for name, values in active.items()
        ]
        output_advancing = bool(wall_progress) and all(wall_progress)
        age, frame = data["geometry_age_ms"], data["frame_id"]
        geometry_current = bool(
            ttl is not None and data["fresh_result"] and data["atlas_published"] is True
            and frame is not None and age is not None and age <= ttl * 1000
        )
        advancing = self._advancing(
            "geometry", (provider, data["epoch"], data["bridge_profile"], data["source_routes"]),
            frame if geometry_current and data["rgb_gate"] == 1 else None, now, ttl or 1.0,
        )
        report = {
            "code": "", "summary": "", "next": "", "level": "",
            "profile": data["profile"], "display_mode": mode, "provider": provider,
            "generator": data["generator"], "audio": data["audio"], "interaction": data["interaction"],
            "geometry": (PROVIDERS.get(provider, "Unknown provider") + " / " + data["bridge_state"]),
            "geometry_age_ms": age, "frame_id": frame, "geometry_advancing": advancing,
            "wall_size": size, "walls": {k: v[:2] for k, v in active.items()},
            "output_advancing": output_advancing, "frame_time_ms": data["frame_time_ms"],
        }

        def done(code, level, summary, action):
            return dict(report, code=code, level=level, summary=summary, next=action)

        if data["experience"] not in ("installation", "combined") or data["transport"] != "local":
            return done("outside_scope", "info", "Live installation not evaluated",
                        "Use this check for a local installation; VR-only and split transport are separate.")
        if data["source_routes"] == (0, 0):
            return done("demo", "info", "Demo source selected", "Use this for rehearsal, or select both live adapter inputs.")
        if data["source_routes"] != (1, 1):
            return done("source_mismatch", "blocked", "RGB and depth are not both on the live adapter",
                        "Review Source switches; a mixed/demo source cannot pass as live.")
        stage = data["stage"]
        if not data["host_present"]:
            return done("no_host", "blocked", "Managed generator host not found", "Check the installed StreamDiffusion host.")
        if stage in ("offloading", "offloaded", "offloaded_removed", "offload_unconfirmed"):
            return done(stage, "waiting", "Generator " + stage.replace("_", " "), "Finish the native stop/offload, then preview again.")
        if stage == "select_generator" or data["generator"] == "not loaded":
            return done("no_generator", "waiting", "No generator selected", "Choose a trusted local TOX and load the generator.")
        if stage in ("configure_rgb", "invalid_source", "unknown"):
            return done("rgb_not_configured", "blocked", "Public RGB preview not configured", "Check the host source and public RGB TOP.")
        if data["generator"] in ("stopped", "server only"):
            return done("generator_inactive", "waiting", "Generator is not streaming", "Start generation in its native controls and inspect the preview.")
        if data["request_rgb"] is False:
            return done("preview_only", "waiting", "Preview only — RGB is not sent to 3D", "Inspect Generation Preview, then enable Send RGB to 3D.")
        if data["timeline"] is not True:
            return done("timeline_paused", "waiting", "Timeline paused or not verified", "Run the timeline when you are ready to rehearse.")
        if data["request_rgb"] is not True or data["rgb_gate"] != 1 or stage != "connected":
            return done("rgb_disconnected", "blocked", "RGB delivery not connected", "Check the host preview and the Send RGB to 3D gate.")
        if provider not in BRIDGES or data["show_provider"] != provider or not data["bridge_present"]:
            return done("provider_mismatch", "blocked", "Geometry provider or route mismatch", "Check Show Controls and the selected bridge.")
        if data["profile"] not in PROFILES or data["bridge_profile"] != data["profile"]:
            return done("profile_mismatch", "blocked", "Worker profile does not match Show Control", "Check the selected machine's own GPU profile.")
        if data["bridge_enabled"] is not True:
            return done("bridge_disabled", "blocked", "Selected geometry bridge disabled", "Enable the selected bridge before starting its worker.")
        if data["bridge_state"] == "error":
            return done("bridge_error", "blocked", "Selected geometry bridge has an error", "Check its STATUS table and result listener.")
        if data["bridge_state"] == "stale" or (age is not None and ttl is not None and age > ttl * 1000):
            return done("geometry_stale", "blocked", "Geometry result is stale", "Check the selected worker and return listener; no stale atlas counts as live.")
        if not geometry_current:
            if data["bridge_error"] == "send_failed":
                return done("worker_unreachable", "blocked", "Geometry worker input is unreachable", "Check the selected worker console and receiver; don't start a second worker.")
            if data["fresh_result"] and not data["atlas_published"]:
                return done("atlas_wait", "waiting", "Result received, atlas upload not confirmed", "Check the bridge STATUS and recursive errors.")
            return done("geometry_wait", "waiting", "Waiting for fresh synchronized geometry", "Check the selected worker; a ready console is not a result.")
        if not advancing:
            return done("geometry_not_advancing", "waiting", "Waiting for advancing synchronized frames", "Check source/worker activity if the frame ID stays fixed.")
        if data["routed_depth"] != 1 or data["source_valid"] is not True or data["bridge_state"] != "running":
            return done("atlas_not_routed", "blocked", "Fresh atlas is not routed to the world", "Check the actual depth route and world frame validity.")
        if mode not in WALLS or mode != data["show_mode"]:
            return done("display_mismatch", "blocked", "Show Control and active display differ", "Check the selected wall mode.")
        if mismatches:
            return done("wall_size_mismatch", "blocked", "Active wall TOP size differs from configuration", "Check cooked TOP sizes, wall settings and TD license tier.")
        if not sizes_ok or not output_advancing:
            return done("wall_not_observed", "waiting", "Active wall feeds not yet verified cooking", "View or drive the selected wall outputs; no force cook is issued.")
        if data["audio"] == "check route" or data["interaction"] == "waiting for depth sensor":
            return done("live_aux_check", "waiting", "Live visuals — check audio or interaction", "Check the Audio and Interaction rows; visuals are advancing.")
        return done("live", "live", "Live — geometry and wall feeds advancing", "Inspect projected content and seams; this check does not inspect pixels.")


def _percentile95(values):
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], 3) if ordered else None


class Rehearsal:
    """At most one sample per second for one hour; explicit export only."""

    def __init__(self, now, seconds, *, started_utc=None):
        if type(seconds) is not int or not 2 <= seconds <= 3600:
            raise ValueError("Rehearsal duration must be an integer between 2 and 3600 seconds")
        self.start = now
        self.duration = seconds
        self.started_utc = started_utc or datetime.now(timezone.utc).isoformat()
        self.rows = []
        self.ended = None
        self._last_sample = None

    def add(self, now, status):
        if self.ended is not None:
            return
        if now >= self.start + self.duration:
            self.ended = self.start + self.duration
            return
        if now >= self.start and (self._last_sample is None or now - self._last_sample >= INTERVAL_SECONDS):
            self._last_sample = now
            self.rows.append(dict(
                elapsed_s=round(now - self.start, 3), code=status["code"], profile=status["profile"],
                provider=status["provider"], display_mode=status["display_mode"],
                frame_time_ms=status["frame_time_ms"], geometry_age_ms=status["geometry_age_ms"],
                source_frame_id=status["frame_id"], output_advancing=status["output_advancing"],
            ))

    def finish(self, now):
        if self.ended is None:
            self.ended = min(max(self.start, now), self.start + self.duration)
        ages = [r["geometry_age_ms"] for r in self.rows if r["geometry_age_ms"] is not None]
        frame_times = [r["frame_time_ms"] for r in self.rows if r["frame_time_ms"] is not None]
        return dict(
            version=REPORT_VERSION, started_utc=self.started_utc, requested_duration_s=self.duration,
            elapsed_s=round(self.ended - self.start, 3), sample_count=len(self.rows),
            state_counts=dict(Counter(r["code"] for r in self.rows)),
            sampled_geometry_age_p95_ms=_percentile95(ages),
            sampled_frame_interval_p95_ms=_percentile95(frame_times),
            live_visual_acceptance=False, samples=list(self.rows),
        )


_monitor = ReadinessMonitor()
_last_poll = None
_status = None
_rehearsal = None


def start_rehearsal(seconds=120):
    global _rehearsal
    now = time.monotonic()
    if _rehearsal is not None:
        raise RuntimeError("Finish the previous rehearsal before starting another")
    _rehearsal = Rehearsal(now, seconds)
    return {"status": "sampling", "duration_s": seconds}


def finish_rehearsal():
    global _rehearsal
    if _rehearsal is None:
        raise RuntimeError("No rehearsal has been started")
    result = _rehearsal.finish(time.monotonic())
    _rehearsal = None
    return result


def reset():
    global _monitor, _last_poll, _status, _rehearsal
    _monitor, _last_poll, _status, _rehearsal = ReadinessMonitor(), None, None, None


def readout(table, key, app_seconds, playing):
    """Never trust a saved table alone or hold Live across a timeline pause."""
    stamp = _number(_cell(table, "observed_at"))
    now = _number(app_seconds)
    current = (_last_poll is not None and _status is not None and now is not None
               and stamp is not None and 0 <= now - stamp <= 3
               and 0 <= time.monotonic() - _last_poll <= 3)
    was_live = _status is not None and _status["code"] in ("live", "live_aux_check")
    running = _known_flag(playing)
    if current and (not was_live or running is True):
        value = _cell(table, key)
        if value:
            return value
    if running is False:
        return {"summary": "Timeline paused — not live",
                "next": "Run the timeline or refresh the read-only observer."}.get(key, "not current")
    return {"summary": "Observer not current",
            "next": "Let the timeline update or refresh the read-only observer."}.get(key, "not current")


def tick(observer, *, passive, lookup, app_seconds, force=False):
    global _last_poll, _status
    now = time.monotonic()
    if not force and _last_poll is not None and now - _last_poll < INTERVAL_SECONDS:
        return None
    _last_poll = now
    try:
        root = observer.parent().parent()
        status = _monitor.evaluate(observe(root, passive=passive, lookup=lookup), now)
    except Exception:
        # Data/API failures fail closed. Raw callback values and local paths
        # are deliberately not rendered into the panel or rehearsal log.
        status = dict(code="observation_error", level="blocked", summary="Readiness observation failed",
                      next="Check the public operator paths and recursive errors.", profile="unknown", provider="unknown",
                      display_mode="unknown", generator="unknown", audio="unknown", interaction="unknown",
                      geometry="unknown", geometry_age_ms=None, frame_id=None, geometry_advancing=False,
                      wall_size=(None, None), walls={}, output_advancing=False, frame_time_ms=None)
    _status = status
    if _rehearsal is not None:
        _rehearsal.add(now, status)
    table = observer.op("STATUS")
    rows = [
        ("metric", "value"), ("summary", status["summary"]), ("next", status["next"]),
        ("source", status["generator"]), ("geometry", status["geometry"]),
        ("wall_sizes", "Rated %sx%s | %s" % (*[value or "?" for value in status["wall_size"]],
            "; ".join("%s: %sx%s" % (name.removeprefix("OUT_"), width or "?", height or "?")
                      for name, (width, height) in status["walls"].items()) or "not observed")),
        ("audio", status["audio"]), ("interaction", status["interaction"]),
        ("state", status["code"]), ("observed_at", str(app_seconds)),
    ]
    table.clear()
    for row in rows:
        table.appendRow(row)
    return dict(status)
