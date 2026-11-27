import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from touchdesigner import show_readiness as install
from touchdesigner import show_readiness_runtime as ready


def healthy():
    return {
        "profile": "3080ti_16gb", "experience": "installation", "transport": "local",
        "mode": "single", "show_mode": "single", "source_routes": (1, 1), "timeline": True,
        "stage": "connected", "host_present": True, "request_rgb": True, "rgb_gate": 1,
        "generator": "streaming", "provider": "moge2", "show_provider": "moge2",
        "bridge_present": True, "bridge_profile": "3080ti_16gb", "bridge_enabled": True,
        "bridge_state": "running", "bridge_error": "none", "atlas_published": True,
        "fresh_result": True, "frame_id": 90, "geometry_age_ms": 120.0, "capture_fps": 5.0,
        "source_valid": True, "routed_depth": 1, "epoch": "fixture-session",
        "wall_size": (1920, 1080), "walls": {"OUT_INSTALLATION": (1920, 1080, 42)},
        "audio": "muted", "interaction": "off", "frame_time_ms": 16.67,
    }


def advance(data):
    data["frame_id"] += 1
    data["walls"] = {k: (w, h, token + 1 if token is not None else None)
                     for k, (w, h, token) in data["walls"].items()}
    return data


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.monitor = ready.ReadinessMonitor()

    def test_live_needs_advancing_geometry_and_wall_cooks(self):
        d = healthy()
        self.assertEqual(self.monitor.evaluate(d, 10)["code"], "geometry_not_advancing")
        d["frame_id"] += 1
        self.assertEqual(self.monitor.evaluate(d, 11)["code"], "wall_not_observed")
        self.assertEqual(self.monitor.evaluate(advance(d), 12)["code"], "live")
        self.assertEqual(self.monitor.evaluate(d, 14)["code"], "geometry_not_advancing")

    def test_demo_mixed_input_and_split_mode_are_not_live(self):
        for extra, status in (
            ({"source_routes": (0, 0)}, "demo"),
            ({"source_routes": (1, 0)}, "source_mismatch"),
            ({"transport": "receive_tcp"}, "outside_scope"),
            ({"experience": "vr"}, "outside_scope"),
            ({"timeline": False}, "timeline_paused"),
        ):
            self.assertEqual(self.monitor.evaluate(dict(healthy(), **extra), 10)["code"], status)

    def test_action_prioritizes_generator_then_delivery_then_geometry(self):
        for extra, status in (
            ({"host_present": False}, "no_host"),
            ({"stage": "select_generator", "request_rgb": False}, "no_generator"),
            ({"generator": "stopped", "request_rgb": False}, "generator_inactive"),
            ({"stage": "preview", "request_rgb": False, "rgb_gate": 0}, "preview_only"),
            ({"stage": "connected", "rgb_gate": 0}, "rgb_disconnected"),
            ({"show_provider": "depth_anything"}, "provider_mismatch"),
            ({"bridge_profile": "5090"}, "profile_mismatch"),
            ({"bridge_enabled": False}, "bridge_disabled"),
            ({"bridge_state": "error"}, "bridge_error"),
            ({"geometry_age_ms": 1200.0, "fresh_result": False, "atlas_published": False}, "geometry_stale"),
            ({"fresh_result": False, "atlas_published": False, "bridge_error": "send_failed"}, "worker_unreachable"),
            ({"atlas_published": False, "fresh_result": True}, "atlas_wait"),
            ({"fresh_result": False, "atlas_published": False}, "geometry_wait"),
        ):
            with self.subTest(extra=extra):
                self.assertEqual(self.monitor.evaluate(dict(healthy(), **extra), 10)["code"], status)

    def test_sessions_and_counter_regressions_do_not_reuse_live_proof(self):
        d = healthy()
        self.monitor.evaluate(d, 1)
        self.assertEqual(self.monitor.evaluate(advance(d), 2)["code"], "live")
        d["epoch"] = "reconnected"
        self.assertEqual(self.monitor.evaluate(advance(d), 3)["code"], "geometry_not_advancing")
        self.assertEqual(self.monitor.evaluate(advance(d), 4)["code"], "live")
        d["frame_id"] = 2
        self.assertEqual(self.monitor.evaluate(d, 5)["code"], "geometry_not_advancing")
        self.assertEqual(self.monitor.evaluate(advance(d), 6)["code"], "live")

    def test_fresh_unrouted_frame_and_clamped_wall_cannot_pass(self):
        for values, expected in (
            ({"source_valid": False}, "atlas_not_routed"),
            ({"routed_depth": 0}, "atlas_not_routed"),
            ({"show_mode": "panoramic_wrap"}, "display_mismatch"),
            ({"walls": {"OUT_INSTALLATION": (1280, 720, 5)}}, "wall_size_mismatch"),
            ({"audio": "check route"}, "live_aux_check"),
            ({"interaction": "waiting for depth sensor"}, "live_aux_check"),
        ):
            with self.subTest(values=values):
                m = ready.ReadinessMonitor()
                d = dict(healthy(), **values)
                m.evaluate(d, 2)
                self.assertEqual(m.evaluate(advance(d), 3)["code"], expected)

    def test_triple_mode_checks_each_selected_physical_feed(self):
        walls = ready.WALLS["artistic_multi_angle"]
        d = dict(healthy(), mode="artistic_multi_angle", show_mode="artistic_multi_angle",
                 walls={name: (1920, 1080, 10) for name in walls}, provider="depth_anything",
                 show_provider="depth_anything")
        self.monitor.evaluate(d, 0)
        d = advance(d)
        d["walls"][walls[1]] = (1920, 1080, 10)
        self.assertEqual(self.monitor.evaluate(d, 1)["code"], "wall_not_observed")
        self.assertEqual(self.monitor.evaluate(advance(d), 2)["code"], "live")


class RecorderTests(unittest.TestCase):
    def test_rehearsal_is_bounded_and_captures_recovery_without_raw_values(self):
        d = healthy()
        m = ready.ReadinessMonitor()
        r = ready.Rehearsal(10, 6, started_utc="test")
        r.add(10, m.evaluate(d, 10))
        added = dict(m.evaluate(advance(d), 11), private_prompt="must not appear")
        r.add(11, added)
        r.add(11.1, added)
        d["bridge_state"] = "stale"
        d["geometry_age_ms"] = 1200
        r.add(12, m.evaluate(d, 12))
        d["bridge_state"], d["geometry_age_ms"] = "running", 60
        r.add(13, m.evaluate(advance(d), 13))
        r.add(14, m.evaluate(advance(d), 14))
        r.add(17, m.evaluate(advance(d), 17))
        report = r.finish(99)
        self.assertEqual(report["elapsed_s"], 6)
        self.assertEqual(report["sample_count"], 5)
        self.assertEqual(report["state_counts"], {"geometry_not_advancing": 2, "live": 2, "geometry_stale": 1})
        self.assertEqual(report["sampled_geometry_age_p95_ms"], 1200)
        self.assertNotIn("private_prompt", json.dumps(report))
        self.assertFalse(report["live_visual_acceptance"])

    def test_rehearsal_caps_duration_and_empty_samples_do_not_fake_metrics(self):
        with self.assertRaises(ValueError):
            ready.Rehearsal(0, 3601)
        with self.assertRaises(ValueError):
            ready.Rehearsal(0, True)
        report = ready.Rehearsal(0, 30).finish(40)
        self.assertEqual(report["sample_count"], 0)
        self.assertIsNone(report["sampled_frame_interval_p95_ms"])

    def test_observer_failure_is_safe_and_table_writes_are_throttled(self):
        ready.reset()
        observer = Mock()
        observer.parent.side_effect = RuntimeError("private path not for status")
        with patch.object(ready.time, "monotonic", side_effect=(1, 1.1)):
            failed = ready.tick(observer, passive=None, lookup=None, app_seconds=4)
            later = ready.tick(observer, passive=None, lookup=None, app_seconds=4.1)
        self.assertEqual(failed["code"], "observation_error")
        self.assertNotIn("private path", json.dumps(failed))
        self.assertIsNone(later)
        observer.op.return_value.clear.assert_called_once()
        ready.reset()

    def test_wall_dimensions_require_an_observed_cook(self):
        top = SimpleNamespace(width=128, height=128, cookAbsFrame=-1)
        self.assertEqual(ready._wall(top, lambda n: n), (None, None, None))
        top.width, top.height, top.cookAbsFrame = 1920, 1080, 10
        self.assertEqual(ready._wall(top, lambda n: n), (1920, 1080, 10))


class InstallGuards(unittest.TestCase):
    def test_working_copy_and_rollback_checks_precede_live_access(self):
        root = Mock(path="/project1/flexgpu")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            accepted = folder / "show-3080.47.toe"
            accepted.write_bytes(bytes(1024))
            with self.assertRaisesRegex(ValueError, "distinct"):
                install._target(root, "3080ti_16gb", accepted,
                                SimpleNamespace(folder=tmp, name=accepted.name))
            other = folder / "show-5090.48.toe"
            other.write_bytes(bytes(1024))
            with self.assertRaisesRegex(ValueError, "tag"):
                install._target(root, "3080ti_16gb", accepted,
                                SimpleNamespace(folder=tmp, name=other.name))
        root.op.assert_not_called()

    def test_report_path_rejects_existing_and_outside_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self.assertEqual(install._report_path("runtime/tests/valid.json", root),
                             root / "runtime/tests/valid.json")
            with self.assertRaises(ValueError):
                install._report_path("README.md", root)
            with self.assertRaises(ValueError):
                install._report_path("runtime/../existing.json", root)
            taken = root / "runtime/used.json"
            taken.parent.mkdir()
            taken.write_text("preserve", encoding="utf-8")
            with self.assertRaises(ValueError):
                install._report_path(taken, root)

    def test_display_expression_never_uses_a_saved_or_paused_live_sample(self):
        expr = install._expression("/status", "/runtime", "summary")
        values = {("observed_at", 1): "10.0", ("summary", 1): "Live"}
        timeline = SimpleNamespace(par=SimpleNamespace(play=Mock()))
        ops = {"/status": values, "/runtime": SimpleNamespace(module=ready), "/local/time": timeline}
        with patch.object(ready, "_status", {"code": "live"}):
            for t, t_wall, last, playing, expected in (
                    (9, 100, 100, True, "Observer not current"),
                    (10, 100, None, True, "Observer not current"),
                    (10, 100, 100, False, "Timeline paused — not live"),
                    (10, 104, 100, True, "Observer not current"),
                    (14, 100, 100, True, "Observer not current"),
                    (11, 101, 100, True, "Live")):
                with self.subTest(t=t, t_wall=t_wall, last=last, playing=playing), \
                        patch.object(ready, "_last_poll", last), patch.object(ready.time, "monotonic", return_value=t_wall):
                    timeline.par.play.eval.return_value = playing
                    self.assertEqual(eval(expr, dict(op=ops.get, absTime=SimpleNamespace(seconds=t))), expected)


if __name__ == "__main__":
    unittest.main()
