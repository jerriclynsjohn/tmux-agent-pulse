"""Optional UI commands preserve unrelated panes and invoking-client scope."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import tmux_ui as ui
import tmux_agent_tree as tree


class UICommandTests(unittest.TestCase):
    def test_disable_kills_only_tagged_panes_from_this_checkout(self):
        panes = [["%1", "@1", "1", "", ""],
                 ["%2", "@1", "0", "1", ui.ROOT],
                 ["%3", "@2", "0", "1", "/other/checkout"]]
        with patch.object(ui, "option", side_effect=lambda name, default="": {
                ui.MODE: "on", ui.OWNER: ui.ROOT}.get(name, default)), \
                patch.object(ui, "_panes", return_value=panes), \
                patch.object(ui, "configure_sidebar_hooks") as hooks, \
                patch.object(ui, "tmux") as tmux:
            ui.sidebar("off")
        self.assertEqual([c.args[0] for c in tmux.call_args_list if c.args[0][0] == "kill-pane"],
                         [["kill-pane", "-t", "%2"]])
        hooks.assert_called_once_with(False)

    def test_other_checkout_blocks_sidebar_enable(self):
        with patch.object(ui, "option", side_effect=lambda name, default="": {
                ui.MODE: "on", ui.OWNER: "/other/checkout"}.get(name, default)), \
                patch.object(ui, "tmux") as tmux:
            with self.assertRaisesRegex(RuntimeError, "Another AgentPulse checkout"):
                ui.sidebar("on")
        tmux.assert_not_called()

    def test_width_has_bounds_and_invalid_value_falls_back(self):
        for value, expected in (("24", 24), ("40", 40), ("-1", 12), ("10000", 100), ("bad", 24)):
            with self.subTest(value=value), patch.object(ui, "option", return_value=value):
                self.assertEqual(ui.sidebar_width(), expected)

    def test_spawn_keeps_focus_and_preserves_path_arguments(self):
        panes = [["%1", "@1", "0", "", ""], ["%2", "@1", "1", "", ""]]
        with patch.object(ui, "option", side_effect=lambda name, default="": {
                ui.MODE: "on", ui.OWNER: ui.ROOT}.get(name, default)), \
                patch.object(ui, "_panes", return_value=panes), \
                patch.object(ui, "BIN", Path("/a checkout/lib")), \
                patch.object(ui, "tmux", return_value="%3") as tmux:
            ui.spawn("@1")
        split = tmux.call_args_list[0].args[0]
        self.assertIn("-d", split)
        self.assertEqual(split[-1], "/a checkout/lib/tmux-agent-sidebar.py")
        self.assertEqual(tmux.call_args_list[1].args[0][-1], "%2")

    def test_focus_uses_invoking_client_window_instead_of_inherited_pane(self):
        with patch.dict(os.environ, {"AGENT_PULSE_CLIENT": "/dev/client2", "TMUX_PANE": "%wrong"}), \
                patch.object(ui, "tmux", return_value="@actual") as tmux, \
                patch.object(ui, "_panes", return_value=[["%sidebar", "@actual", "0", "1", ui.ROOT]]) as panes:
            ui.focus()
        panes.assert_called_once_with("@actual")
        self.assertEqual(tmux.call_args_list[0].args[0],
                         ["display-message", "-p", "-c", "/dev/client2", "#{window_id}"])
        self.assertEqual(tmux.call_args_list[-1].args[0], ["select-pane", "-t", "%sidebar"])

    def test_popup_ascii_and_control_character_sanitization(self):
        snapshot = tree.SEPARATOR.join(("%1", "work", "1", "name\x1b[31m", "0", "/repo", "", "waiting", "codex", ""))
        lines = tree.build_lines(snapshot, ascii_mode=True)
        self.assertIn("?", lines[1])
        self.assertIn("codex -", lines[1])
        self.assertNotIn("name\x1b", lines[1])

    def test_popup_never_switches_an_arbitrary_client(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(tree.shutil, "which", return_value="/usr/bin/fzf"), \
                patch.object(tree, "tmux", return_value="client-one\nclient-two"):
            with self.assertRaisesRegex(RuntimeError, "invoking tmux client"):
                tree.pick(["agent"])


@unittest.skipUnless(os.environ.get("AGENT_PULSE_REAL_TMUX_TEST") == "1",
                     "set AGENT_PULSE_REAL_TMUX_TEST=1 for the private tmux UI check")
class RealUICommandTests(unittest.TestCase):
    def test_space_path_socket_override_width_and_focus(self):
        with tempfile.TemporaryDirectory(prefix="agent pulse ui-", dir="/tmp") as temporary:
            base = Path(temporary)
            checkout = base / "checkout with spaces"
            shutil.copytree(Path(__file__).resolve().parents[1] / "lib", checkout / "lib")
            socket = str(base / "server.sock")
            env = dict(os.environ, AGENT_PULSE_SOCKET=socket,
                       TMUX="/tmp/incorrect-agent-pulse-socket,0,0", PYTHONDONTWRITEBYTECODE="1")

            def tmux(*args):
                return subprocess.check_output(["tmux", "-S", socket, *args], text=True,
                                               stderr=subprocess.PIPE, timeout=10).strip()

            def command(*args):
                subprocess.run([sys.executable, str(checkout / "lib/tmux_ui.py"), *args],
                               env=env, check=True, capture_output=True, text=True, timeout=20)

            try:
                tmux("-f", "/dev/null", "new-session", "-d", "-s", "ui-test", "-x", "120", "-y", "35", "sleep", "120")
                original = tmux("list-panes", "-F", "#{pane_id}|#{pane_pid}")
                focus = tmux("display-message", "-p", "#{pane_id}")
                tmux("set-option", "-g", "@agent-pulse-sidebar-width", "31")
                tmux("set-option", "-g", "@agent-pulse-ascii", "on")
                command("sidebar", "on")
                before = tmux("list-panes", "-a", "-F", "#{pane_id}|#{pane_width}|#{@agent-pulse-sidebar}")
                self.assertIn("|31|1", before)
                self.assertEqual(tmux("display-message", "-p", "#{pane_id}"), focus)
                tmux("new-window", "-d", "-t", "ui-test", "sleep", "120")
                after = tmux("list-panes", "-a", "-F", "#{pane_id}|#{pane_width}|#{@agent-pulse-sidebar}")
                self.assertEqual(after.count("|31|1"), 2)
                tmux("set-option", "-g", "@agent-pulse-sidebar-width", "37")
                command("resize")
                resized = tmux("list-panes", "-a", "-F", "#{pane_id}|#{pane_width}|#{@agent-pulse-sidebar}")
                self.assertEqual(resized.count("|37|1"), 2)
                command("sidebar", "off")
                remaining = tmux("list-panes", "-a", "-F", "#{pane_id}|#{pane_pid}")
                self.assertIn(original, remaining)
                self.assertEqual(len(remaining.splitlines()), 2)
            finally:
                try:
                    tmux("kill-server")
                except subprocess.SubprocessError:
                    pass


if __name__ == "__main__":
    unittest.main()
