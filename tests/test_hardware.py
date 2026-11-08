"""CUDA probe lifetime checks using harmless Python sleepers instead of CUDA."""
from contextlib import ExitStack
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from studio import hardware


class HardwareProbeTests(unittest.TestCase):
    def setUp(self):
        self.original_popen = subprocess.Popen

    def wait_for(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = condition()
            if result:
                return result
            time.sleep(.01)
        self.fail("Hardware probe did not reach expected state")

    def sleeper(self, _arguments, **kwargs):
        return self.original_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    def monitored_sleeper(self, stack):
        stack.enter_context(mock.patch.object(hardware.HardwareMonitor, "_sample_loop", return_value=None))
        stack.enter_context(mock.patch("studio.hardware.importlib.util.find_spec", return_value=object()))
        stack.enter_context(mock.patch("studio.hardware.subprocess.Popen", side_effect=self.sleeper))
        monitor = hardware.HardwareMonitor()
        stack.callback(monitor.close)
        return monitor

    def test_close_reaps_only_owned_probe_without_waiting_35_seconds(self):
        unrelated = self.original_popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            with ExitStack() as stack:
                monitor = self.monitored_sleeper(stack)
                child = self.wait_for(lambda: monitor._probe_child)
                self.assertIsNone(child.poll())
                started = time.monotonic()
                monitor.close()
                self.assertLess(time.monotonic() - started, 3)
                self.assertIsNotNone(child.poll(), "Owned probe child survived monitor shutdown")
                self.assertIsNone(unrelated.poll(), "Shutdown touched an unrelated process")
                self.wait_for(lambda: monitor._probe_child is None)
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=3)

    def test_closed_monitor_cannot_spawn_probe(self):
        # Prevent constructor threads from starting, close, then deliver the
        # delayed probe callback as if its daemon thread had just been scheduled.
        with mock.patch("studio.hardware.threading.Thread"), \
             mock.patch("studio.hardware.subprocess.Popen") as popen:
            monitor = hardware.HardwareMonitor()
            monitor.close()
            monitor._probe_cuda()
            popen.assert_not_called()
            self.assertIsNone(monitor._probe_child)

    def test_probe_timeout_kills_and_reaps_child(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch("studio.hardware.CUDA_PROBE_TIMEOUT_SECONDS", .2))
            monitor = self.monitored_sleeper(stack)
            child = self.wait_for(lambda: monitor._probe_child)
            self.wait_for(lambda: monitor.snapshot()["cuda_error"])
            self.assertIn("超时", monitor.snapshot()["cuda_error"])
            self.assertFalse(monitor.snapshot()["cuda_available"])
            self.assertIsNotNone(child.poll(), "Timed-out probe was not reaped")
            self.assertIsNone(monitor._probe_child)

    def test_stop_during_dependency_lookup_prevents_later_spawn(self):
        with mock.patch("studio.hardware.threading.Thread"):
            monitor = hardware.HardwareMonitor()
        entered, resume = threading.Event(), threading.Event()

        def delayed_discovery(_name):
            entered.set()
            if not resume.wait(2):
                raise RuntimeError("Test did not release the dependency lookup")
            return object()

        with mock.patch("studio.hardware.importlib.util.find_spec", side_effect=delayed_discovery), \
             mock.patch("studio.hardware.subprocess.Popen") as popen:
            thread = threading.Thread(target=monitor._probe_cuda)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                monitor.close()
            finally:
                resume.set()
                thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
