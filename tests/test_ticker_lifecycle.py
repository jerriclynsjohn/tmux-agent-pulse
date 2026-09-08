"""Ticker lifecycle regressions: overloaded tmux must not disable status."""

import fcntl
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

BIN = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(BIN))
import tmux_status_ticker as rendering


class TickerLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.ticker = SimpleNamespace(directory=self.directory, socket="/tmp/test-ticker.sock",
                                      config={}, tick=Mock(), tmux=Mock(), clear_options=Mock())
        self.handlers = {}
        self.clock = 100.0
        self.delays = []
        self.output = io.StringIO()

    def sleep(self, seconds):
        self.assertLess(len(self.delays), 500, "ticker did not stop")
        self.delays.append(seconds)
        self.clock += seconds

    def stop(self, signum=signal.SIGTERM):
        self.handlers[signum](signum, None)

    def run_main(self):
        with patch.object(rendering, "Ticker", return_value=self.ticker), \
             patch.object(rendering.signal, "signal", side_effect=self.handlers.__setitem__), \
             patch.object(rendering.time, "monotonic", side_effect=lambda: self.clock), \
             patch.object(rendering.time, "sleep", side_effect=self.sleep), \
             patch.object(rendering.status, "process_snapshot", return_value={os.getpid(): {"start": "test-start"}}), \
             patch.object(rendering.sys, "stderr", self.output):
            result = rendering.main()
        self.assertEqual(result, 0)
        self.ticker.clear_options.assert_called_once_with()
        # Main must release the actual singleton lock on every clean exit.
        with (self.directory / ".ticker.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)

    def test_repeated_tick_and_probe_timeouts_recover_before_server_exit(self):
        recovered = []
        tick_times = []
        record = self.directory / "%1.json"
        record.write_text('{"state":"working"}')

        def tick():
            tick_times.append(self.clock)
            if self.ticker.tick.call_count == 9:
                self.ticker.clear_options.assert_not_called()
                self.assertTrue(record.exists())
                recovered.append(True)
                return
            raise subprocess.TimeoutExpired(["tmux", "list-panes"], 2)

        self.ticker.tick.side_effect = tick
        self.ticker.tmux.side_effect = [
            *[subprocess.TimeoutExpired(["tmux", "list-sessions"], 2) for _ in range(8)],
            subprocess.CalledProcessError(1, ["tmux", "list-sessions"]),
        ]
        self.run_main()
        self.assertEqual(recovered, [True])
        self.assertEqual(self.ticker.tick.call_count, 10)
        self.assertEqual(self.ticker.tmux.call_count, 9)
        intervals = [later - earlier for earlier, later in zip(tick_times, tick_times[1:])]
        for actual, expected in zip(intervals[:8], [.5, 1, 2, 4, 5, 5, 5, 5]):
            self.assertAlmostEqual(actual, expected)
        self.assertAlmostEqual(intervals[8], .5 if hasattr(rendering, "read_config") else .2)
        output = self.output.getvalue()
        self.assertEqual(output.count("(retrying)"), 1)
        self.assertIn("recovered after 8 failed updates", output)
        self.assertIn("server unavailable; stopping", output)

    def test_probe_success_does_not_stop_after_tick_timeout(self):
        def tick():
            if self.ticker.tick.call_count == 1:
                raise subprocess.TimeoutExpired(["ps"], 3)
            self.ticker.clear_options.assert_not_called()
            self.stop()

        self.ticker.tick.side_effect = tick
        self.ticker.tmux.return_value = "$0"
        self.run_main()
        self.assertEqual(self.ticker.tick.call_count, 2)
        self.assertIn("recovered after 1 failed updates", self.output.getvalue())

    def test_other_probe_failure_does_not_prove_server_has_exited(self):
        def tick():
            if self.ticker.tick.call_count == 1:
                raise OSError("temporary process query failure")
            self.stop()

        self.ticker.tick.side_effect = tick
        self.ticker.tmux.side_effect = OSError("temporary tmux query failure")
        self.run_main()
        self.assertEqual(self.ticker.tick.call_count, 2)

    def test_missing_tmux_binary_releases_lock_without_retry(self):
        self.ticker.tick.side_effect = FileNotFoundError("tmux")
        self.ticker.tmux.side_effect = FileNotFoundError("tmux")
        self.run_main()
        self.assertEqual(self.ticker.tick.call_count, 1)
        self.assertEqual(self.delays, [])

    def test_sigterm_interrupts_failure_backoff(self):
        self.ticker.tick.side_effect = subprocess.TimeoutExpired(["tmux", "list-panes"], 2)
        self.ticker.tmux.side_effect = subprocess.TimeoutExpired(["tmux", "list-sessions"], 2)
        sleep = self.sleep

        def stop_during_backoff(seconds):
            sleep(seconds)
            if self.ticker.tick.call_count == 4:
                self.stop()

        self.sleep = stop_during_backoff
        self.run_main()
        self.assertEqual(self.ticker.tick.call_count, 4)
        self.assertLessEqual(self.delays[-1], .2)
        self.assertAlmostEqual(self.clock, 100 + .5 + 1 + 2 + .2)

    def test_sigterm_preserves_status_until_shutdown_and_cleans_once(self):
        self.assert_clean_signal_shutdown(signal.SIGTERM)

    def test_sigint_preserves_status_until_shutdown_and_cleans_once(self):
        self.assert_clean_signal_shutdown(signal.SIGINT)

    def assert_clean_signal_shutdown(self, signum):
        def tick():
            self.ticker.clear_options.assert_not_called()
            if self.ticker.tick.call_count == 3:
                self.stop(signum)

        self.ticker.tick.side_effect = tick
        self.run_main()
        self.assertEqual(self.ticker.tick.call_count, 3)
        interval = .5 if hasattr(rendering, "read_config") else .2
        self.assertAlmostEqual(self.clock, 100 + 2 * interval)
        self.ticker.tmux.assert_not_called()


if __name__ == "__main__":
    unittest.main()
