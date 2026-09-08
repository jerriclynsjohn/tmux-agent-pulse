"""AgentPulse owns individual hook entries, not another plugin's hook array."""
import os
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(BIN))
import tmux_sidebar_hooks as hooks


class FakeTmux:
    def __init__(self):
        self.entries = {name: {0: 'set-option -g @other-plugin "active"',
                               1000: 'set-option -g @another-plugin "active"'}
                        for name in hooks.HOOKS}
        self.options = {}

    def __call__(self, args, **kwargs):
        if args[0] == "show-hooks":
            name = args[-1]
            return "\n".join(f"{name}[{index}] {command}" for index, command in sorted(self.entries[name].items()))
        if args[0] == "show-options":
            if args[-1] not in self.options:
                raise subprocess.CalledProcessError(1, args)
            return self.options[args[-1]]
        if args[0] == "set-option":
            if "-u" in args:
                self.options.pop(args[-1], None)
            else:
                self.options[args[-2]] = args[-1]
            return ""
        if args[0] == "set-hook":
            target = args[-1] if "-u" in args else args[-2]
            match = re.fullmatch(r"([^\[]+)\[(\d+)\]", target)
            if match is None:
                raise AssertionError("Whole-array hook mutation is forbidden: " + repr(args))
            name, index = match[1], int(match[2])
            if "-u" in args:
                self.entries[name].pop(index, None)
            else:
                self.entries[name][index] = args[-1]
            return ""
        raise AssertionError(args)


class SidebarHookOwnershipTests(unittest.TestCase):
    def test_repeated_enable_disable_preserves_all_foreign_entries(self):
        server = FakeTmux()
        before = {name: dict(entries) for name, entries in server.entries.items()}
        with patch.object(hooks, "tmux", side_effect=server):
            self.assertEqual(set(hooks.configure_sidebar_hooks(True).values()), {1001})
            hooks.configure_sidebar_hooks(True)
            self.assertTrue(all(len(entries) == 3 for entries in server.entries.values()))
            hooks.configure_sidebar_hooks(False)
        self.assertEqual(server.entries, before)
        self.assertEqual(server.options, {})

    def test_foreign_replacement_of_our_slot_is_never_removed(self):
        server = FakeTmux()
        with patch.object(hooks, "tmux", side_effect=server):
            installed = hooks.configure_sidebar_hooks(True)
            for name, index in installed.items():
                server.entries[name][index] = 'set-option -g @replacement "owned elsewhere"'
            hooks.configure_sidebar_hooks(True)
            self.assertTrue(all(entries[1001].startswith("set-option") for entries in server.entries.values()))
            self.assertTrue(all(1002 in entries for entries in server.entries.values()))
            hooks.configure_sidebar_hooks(False)
        self.assertTrue(all(len(entries) == 3 and 1001 in entries for entries in server.entries.values()))

    def test_migrates_standalone_legacy_callback_but_preserves_composed_foreign_hook(self):
        server = FakeTmux()
        with tempfile.TemporaryDirectory() as temporary:
            old = Path(temporary) / "old-agent-status"
            old.symlink_to(BIN, target_is_directory=True)
            for name, (script, arguments) in hooks.HOOKS.items():
                command = "run-shell " + hooks._tmux_quote(shlex.join([str(old / script), *arguments]))
                server.entries[name][7] = command
                server.entries[name][8] = command + ' ; set-option -g @foreign "keep"'
            with patch.object(hooks, "tmux", side_effect=server):
                hooks.configure_sidebar_hooks(False)
        self.assertTrue(all(7 not in entries and 8 in entries for entries in server.entries.values()))

    def test_same_script_name_from_another_directory_does_not_claim_ownership(self):
        server = FakeTmux()
        for name, (script, arguments) in hooks.HOOKS.items():
            server.entries[name][7] = "run-shell " + hooks._tmux_quote(shlex.join(["/other/plugin/" + script, *arguments]))
        before = {name: dict(entries) for name, entries in server.entries.items()}
        with patch.object(hooks, "tmux", side_effect=server):
            hooks.configure_sidebar_hooks(False)
        self.assertEqual(server.entries, before)

    def test_concurrent_replacement_is_not_recorded_as_our_callback(self):
        server = FakeTmux()
        replaced = False

        def replace_after_set(args, **kwargs):
            nonlocal replaced
            result = server(args, **kwargs)
            if args[:2] == ["set-hook", "-g"] and "-u" not in args and not replaced:
                server.entries["after-new-window"][1001] = 'set-option -g @concurrent-plugin "keep"'
                replaced = True
            return result

        with patch.object(hooks, "tmux", side_effect=replace_after_set):
            with self.assertRaisesRegex(RuntimeError, "changed during installation"):
                hooks.configure_sidebar_hooks(True)
            self.assertNotIn("@agent-pulse-hook-after-new-window", server.options)
            hooks.configure_sidebar_hooks(False)
        self.assertIn("@concurrent-plugin", server.entries["after-new-window"][1001])

    def test_another_checkout_is_never_disabled_or_replaced(self):
        server = FakeTmux()
        with patch.object(hooks, "tmux", side_effect=server):
            hooks.configure_sidebar_hooks(True)
            before = {name: dict(entries) for name, entries in server.entries.items()}
            options = dict(server.options)
            with patch.object(hooks, "ROOT", "/another/checkout"):
                hooks.configure_sidebar_hooks(False)
                self.assertEqual(server.entries, before)
                self.assertEqual(server.options, options)
                with self.assertRaisesRegex(RuntimeError, "Another AgentPulse checkout"):
                    hooks.configure_sidebar_hooks(True)

    def test_callback_paths_with_spaces_remain_one_shell_argument(self):
        with tempfile.TemporaryDirectory(prefix="agent pulse ") as temporary:
            script = Path(temporary) / "tmux-agent-sidebar-spawn.sh"
            command = "run-shell " + hooks._tmux_quote(shlex.join([str(script), "#{window_id}"]))
            self.assertTrue(hooks._owned_callback(command, script, ["#{window_id}"]))


@unittest.skipUnless(os.environ.get("AGENT_PULSE_REAL_TMUX_TEST") == "1",
                     "set AGENT_PULSE_REAL_TMUX_TEST=1 to run the private tmux integration test")
class RealSidebarHookTests(unittest.TestCase):
    def test_toggle_preserves_and_executes_other_plugin_callbacks(self):
        with tempfile.TemporaryDirectory(prefix="agentpulse-hooks-", dir="/tmp") as temporary:
            socket = str(Path(temporary) / "tmux.sock")

            def call(args):
                return subprocess.check_output(["tmux", "-S", socket, *args], text=True,
                                               stderr=subprocess.PIPE, timeout=10).rstrip("\n")

            try:
                call(["-f", "/dev/null", "new-session", "-d", "-s", "hook-test", "-x", "100", "-y", "30", "/bin/sleep", "60"])
                before = {}
                for name in hooks.HOOKS:
                    call(["set-hook", "-g", f"{name}[0]", 'set-option -g @other-plugin-fired yes'])
                    call(["set-hook", "-g", f"{name}[1000]", 'set-option -g @another-plugin-fired yes'])
                    before[name] = call(["show-hooks", "-g", name])
                env = dict(os.environ, TMUX=socket + ",1,0", PYTHONDONTWRITEBYTECODE="1")
                subprocess.run(["bash", str(BIN / "tmux-toggle-sidebar.sh")], env=env, check=True,
                               capture_output=True, text=True, timeout=15)
                for name in hooks.HOOKS:
                    installed = call(["show-hooks", "-g", name])
                    self.assertIn(f"{name}[1001]", installed)
                    self.assertIn(before[name], installed)
                call(["new-window", "-d", "-t", "hook-test", "/bin/sleep", "60"])
                self.assertEqual(call(["show-options", "-g", "-v", "@other-plugin-fired"]), "yes")
                self.assertEqual(call(["show-options", "-g", "-v", "@another-plugin-fired"]), "yes")
                call(["set-option", "-g", "-u", "@other-plugin-fired"])
                call(["set-option", "-g", "-u", "@another-plugin-fired"])
                # Execute the named window hook too: show-hooks without a name
                # can omit window-scoped hooks even with the global flag.
                call(["set-hook", "-g", "-R", "window-resized"])
                self.assertEqual(call(["show-options", "-g", "-v", "@other-plugin-fired"]), "yes")
                self.assertEqual(call(["show-options", "-g", "-v", "@another-plugin-fired"]), "yes")
                subprocess.run(["bash", str(BIN / "tmux-toggle-sidebar.sh")], env=env, check=True,
                               capture_output=True, text=True, timeout=15)
                for name in hooks.HOOKS:
                    self.assertEqual(call(["show-hooks", "-g", name]), before[name])
            finally:
                try:
                    call(["kill-server"])
                except subprocess.SubprocessError:
                    pass


if __name__ == "__main__":
    unittest.main()
