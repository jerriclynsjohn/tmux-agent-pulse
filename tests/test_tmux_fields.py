"""tmux field framing must survive older output escaping and literal text."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(BIN))
import agent_pulse as status
import tmux_agent_tree as popup
import tmux_status_ticker as ticker
import tmux_ui as ui

spec = importlib.util.spec_from_file_location("field_test_sidebar", BIN / "tmux-agent-sidebar.py")
sidebar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sidebar)


class FieldFramingTests(unittest.TestCase):
    def test_collectors_share_a_printable_separator(self):
        self.assertTrue(status.FIELD_SEPARATOR.isascii())
        self.assertTrue(status.FIELD_SEPARATOR.isprintable())
        self.assertNotIn("\\", status.FIELD_SEPARATOR)
        for consumer in (ticker, popup, sidebar, ui):
            self.assertEqual(consumer.SEPARATOR, status.FIELD_SEPARATOR)


@unittest.skipUnless(os.environ.get("AGENT_PULSE_REAL_TMUX_TEST") == "1" and shutil.which("tmux"),
                     "set AGENT_PULSE_REAL_TMUX_TEST=1 for the private tmux framing check")
class RealFieldFramingTests(unittest.TestCase):
    def test_literal_escape_text_tabs_and_pipes_survive_real_tmux_collectors(self):
        with tempfile.TemporaryDirectory(prefix="pulse-fields-", dir="/tmp") as temporary:
            base = Path(temporary).resolve()
            # tmux 3.4 prints U+001F as literal backslash037 but does not escape
            # an existing backslash037. Decoding that string cannot be lossless.
            value = "literal\\037|with\ttab"
            title = value.replace("\t", " ")
            label = "label\\037|with\ttab"
            cwd = base / value
            cwd.mkdir()
            socket = str(base / "server.sock")
            env = {key: val for key, val in os.environ.items()
                   if not key.startswith(("TMUX", "AGENT_PULSE_", "AGENT_STATUS_"))}
            env["TERM"] = "xterm-256color"
            try:
                subprocess.run(["tmux", "-S", socket, "-f", "/dev/null", "new-session",
                                "-d", "-s", "fields", "-c", str(cwd), "/bin/sleep", "60"],
                               env=env, check=True, capture_output=True, text=True, timeout=10)
                with patch.dict(os.environ, {"AGENT_PULSE_SOCKET": socket}):
                    pane = status.tmux(["display-message", "-p", "#{pane_id}"])
                    status.tmux(["select-pane", "-t", pane, "-T", title])
                    # tmux 3.7c escapes backslashes when it stores pane titles.
                    # Preserve that native title without decoding unrelated data.
                    native_title = status.tmux(["display-message", "-p", "-t", pane, "#{pane_title}"])
                    status.tmux(["set-option", "-p", "-t", pane, "@agent-pulse-state", "working",
                                 ";", "set-option", "-p", "-t", pane, "@agent-pulse-provider", "codex",
                                 ";", "set-option", "-p", "-t", pane, "@agent-pulse-name", label])
                    raw = status.tmux(["list-panes", "-a", "-F", ticker.PANE_FORMAT])
                    parsed = ticker.parse_panes(raw)
                    self.assertEqual(len(parsed), 1)
                    self.assertEqual(parsed[0]["title"], native_title)
                    self.assertEqual(ui._panes()[0][0], pane)
                    rows, active, *_ = sidebar.collect_sidebar_snapshot(my_pane=pane)
                    row = next(row for row in rows if row["kind"] == "pane")
                    self.assertEqual(row["title"], f"{label} · {native_title}")
                    self.assertEqual(row["dirname"], value)
                    self.assertEqual(active, pane)
                    raw = status.tmux(["list-panes", "-a", "-F", popup.PANE_FORMAT])
                    lines = popup.build_lines(raw, ascii_mode=True)
                    self.assertEqual(len(lines), 2)
                    self.assertIn(label.replace("\t", " "), lines[1])
                    self.assertIn(value.replace("\t", " "), lines[1])
                    self.assertTrue(lines[1].endswith("\t" + pane))
            finally:
                subprocess.run(["tmux", "-S", socket, "kill-server"], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


if __name__ == "__main__":
    unittest.main()
