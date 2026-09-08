"""Regression checks for cross-server lifecycle and interactive popup behavior."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import plugin_runtime as runtime


class RuntimeReviewTests(unittest.TestCase):
    def test_shared_state_directory_cannot_reuse_or_stop_another_servers_ticker(self):
        with tempfile.TemporaryDirectory(prefix="pulse-cross-socket-") as temporary:
            directory = Path(temporary)
            other = {"pid": 12345, "start": "stable start", "root": str(runtime.ROOT),
                     "socket": str(directory / "server-a.sock")}
            selected = str(directory / "server-b.sock")
            for operation in (runtime.load, runtime.unload):
                with self.subTest(operation=operation.__name__), \
                     patch.dict(os.environ, {"AGENT_PULSE_DIR": str(directory)}), \
                     patch.object(runtime.status, "tmux", return_value=selected), \
                     patch.object(runtime.status, "state_directory", return_value=directory), \
                     patch.object(runtime, "config_from_tmux", return_value={}), \
                     patch.object(runtime, "live_ticker", return_value=other), \
                     patch.object(runtime, "lock_held", return_value=True), \
                     patch.object(runtime, "write_json") as write, \
                     patch.object(runtime.os, "kill") as kill, \
                     patch.object(runtime, "configure_bindings") as bindings:
                    with self.assertRaises(RuntimeError):
                        operation()
                    write.assert_not_called()
                    kill.assert_not_called()
                    bindings.assert_not_called()

    def test_interactive_popup_has_no_short_subprocess_timeout(self):
        with patch.dict(os.environ, {"AGENT_PULSE_SOCKET": "/tmp/pulse-popup-review.sock"}), \
             patch.object(runtime.shutil, "which", return_value="/usr/bin/fzf"), \
             patch.object(runtime.status, "tmux", return_value="") as tmux:
            result = runtime.main(["--client", "/dev/ttys-review", "popup"])
        self.assertEqual(result, 0)
        self.assertEqual(tmux.call_count, 1)
        command = tmux.call_args.args[0]
        self.assertEqual(command[0], "display-popup")
        self.assertIn("/dev/ttys-review", command)
        self.assertIn("timeout", tmux.call_args.kwargs)
        self.assertIsNone(tmux.call_args.kwargs["timeout"])


if __name__ == "__main__":
    unittest.main()
