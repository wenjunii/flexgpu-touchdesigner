from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flexgpu.runtime import _pid_alive, _record_process_status  # noqa: E402


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux procfs behavior")
class LinuxProcessStatusTests(unittest.TestCase):
    def test_exit_during_identity_inspection_is_treated_as_dead(self):
        with (
            mock.patch("flexgpu.runtime._pid_alive", side_effect=[True, False]),
            mock.patch("flexgpu.runtime._inspect_process", return_value=None),
        ):
            status, _reason = _record_process_status({"pid": 123})
        self.assertEqual(status, "dead")

    def test_live_process_with_unreadable_identity_is_still_refused(self):
        with (
            mock.patch("flexgpu.runtime._pid_alive", side_effect=[True, True]),
            mock.patch("flexgpu.runtime._inspect_process", return_value=None),
        ):
            status, _reason = _record_process_status({"pid": 123})
        self.assertEqual(status, "refuse")

    def test_current_process_is_live_without_a_signal_probe(self):
        with mock.patch("flexgpu.runtime.os.kill") as kill:
            self.assertTrue(_pid_alive(os.getpid()))
        kill.assert_not_called()

    def test_exited_unreaped_child_is_not_live(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            # Do not call poll()/wait() until after the assertion: retain an
            # actual zombie, the state that blocked managed Linux shutdown.
            deadline = time.monotonic() + 5.0
            state = ""
            while time.monotonic() < deadline:
                text = Path("/proc/%d/stat" % child.pid).read_text()
                state = text.rpartition(")")[2].split()[0]
                if state == "Z":
                    break
                time.sleep(0.01)
            self.assertEqual(state, "Z", "child did not exit before the deadline")
            self.assertFalse(_pid_alive(child.pid))
        finally:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
