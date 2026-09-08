"""Renderer integration tests: real metadata files, mocked tmux/process snapshots."""

import importlib.util
from itertools import permutations
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(BIN))
import agent_pulse as status
import tmux_agent_tree as popup
import tmux_status_ticker as rendering

spec = importlib.util.spec_from_file_location("tmux_sidebar", BIN / "tmux-agent-sidebar.py")
sidebar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sidebar)


def pane(pane_id="%1", window="@1", pid=100, title="Agent task", sidebar=False):
    return rendering.SEPARATOR.join((pane_id, window, str(pid), title, "1" if sidebar else ""))


class RenderingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        env = patch.dict(os.environ, {"AGENT_PULSE_DIR": temporary.name})
        env.start()
        self.addCleanup(env.stop)
        self.processes = {
            100: {"ppid": 1, "command": "/bin/zsh", "start": "shell-start"},
            101: {"ppid": 100, "command": "/bin/codex", "start": "codex-start"},
        }
        self.process_mock = patch.object(status, "process_snapshot", side_effect=lambda: self.processes)
        self.ps = self.process_mock.start()
        self.addCleanup(self.process_mock.stop)
        self.snapshot = pane()
        self.content = ""
        self.commands = []
        tmux_mock = patch.object(status, "tmux", side_effect=self.tmux)
        tmux_mock.start()
        self.addCleanup(tmux_mock.stop)
        self.ticker = rendering.Ticker(socket_path="/tmp/test-agent-renderer.sock")

    def tmux(self, args, **kwargs):
        self.commands.append(args)
        if args[0] == "list-panes":
            return self.snapshot
        if args[0] == "capture-pane":
            return self.content
        return ""

    def save(self, state="waiting", provider="codex", pane_id="%1", updated_at=10):
        record = {"state": state, "provider": provider, "owner_pid": 101,
                  "owner_start": f"{provider}-start", "updated_at": updated_at,
                  "session_id": "test-session", "pending_questions": {"q": {}},
                  "pending_permissions": {}}
        status.save_record(pane_id, record, directory=self.directory)
        return record

    def use_claude(self):
        self.processes[101] = {"ppid": 100, "command": "/bin/claude", "start": "claude-start"}

    def test_mixed_window_priority_is_independent_of_provider_or_order(self):
        records = [{"provider": "codex", "state": "working"},
                   {"provider": "claude", "state": "waiting"},
                   {"provider": "codex", "state": "cancelled"},
                   {"provider": "claude", "state": "idle"}]
        for ordered in permutations(records):
            self.assertEqual(rendering.aggregate_state(ordered), "waiting")
        self.assertEqual(rendering.aggregate_state([records[0], records[2]]), "working")
        self.assertEqual(rendering.aggregate_state([records[2], records[3]]), "cancelled")
        self.assertEqual(rendering.aggregate_state([{"state": "unknown"}, records[3]]), "idle")
        self.assertIn("○", rendering.icon_for_state("unknown"))
        self.assertEqual(rendering.aggregate_state([None]), "")
        self.assertIn("⏳", rendering.icon_for_state("waiting"))
        self.assertNotEqual(rendering.icon_for_state("working", 0), rendering.icon_for_state("working", 1))

    def test_no_record_and_previous_provider_record_show_live_provider_unknown(self):
        self.ticker.tick(now=20, monotonic=0)
        self.assertEqual(self.ticker.published[("pane", "%1", "@agent-pulse-provider")], "codex")
        self.assertEqual(self.ticker.published[("pane", "%1", "@agent-pulse-state")], "unknown")
        self.save("cancelled", "claude")
        self.ticker.tick(now=21, monotonic=1)
        self.assertEqual(self.ticker.published[("pane", "%1", "@agent-pulse-state")], "unknown")
        self.assertNotIn("✗", self.ticker.published[("window", "@1", "@agent-pulse-window-icon")])

    def test_cancelled_disappears_when_owner_exits_even_if_shell_and_file_remain(self):
        self.save("cancelled")
        self.ticker.tick(now=20, monotonic=0)
        self.assertIn("✗", self.ticker.published[("pane", "%1", "@agent-pulse-icon")])
        del self.processes[101]
        self.ticker.tick(now=21, monotonic=1)
        for option in ("@agent-pulse-state", "@agent-pulse-provider", "@agent-pulse-icon"):
            self.assertEqual(self.ticker.published[("pane", "%1", option)], "")
        self.assertEqual(self.ticker.published[("window", "@1", "@agent-pulse-window-icon")], "")

    def test_codex_waiting_never_uses_claude_title_or_screen_recovery(self):
        self.save()
        self.snapshot = pane(title="✳ task")
        self.ticker.tick(now=20, monotonic=0)
        self.assertFalse(any(c[0] == "capture-pane" for c in self.commands))
        self.assertEqual(status.load_record("%1")["state"], "waiting")

    def test_claude_unknown_title_preserves_waiting(self):
        self.use_claude()
        self.save(provider="claude")
        self.ticker.tick(now=20, monotonic=0)
        self.assertFalse(any(c[0] == "capture-pane" for c in self.commands))
        self.assertEqual(status.load_record("%1")["state"], "waiting")

    def test_claude_prompt_recovery_is_rate_limited_then_tracks_positive_title(self):
        self.use_claude()
        self.save(provider="claude")
        self.snapshot = pane(title="⠹ task")
        self.content = "Enter to select · Esc to cancel"
        self.ticker.tick(now=20, monotonic=0)
        self.ticker.tick(now=20.2, monotonic=.2)
        self.assertEqual(sum(c[0] == "capture-pane" for c in self.commands), 1)
        self.assertEqual(self.ps.call_count, 1)
        self.content = "The answer was accepted."
        self.ticker.tick(now=21, monotonic=1)
        self.assertEqual(status.load_record("%1")["state"], "working")
        self.assertEqual(self.ticker.published[("pane", "%1", "@agent-pulse-state")], "working")
        self.assertEqual(sum(c[0] == "list-panes" for c in self.commands), 3)

    def test_recovery_respects_grace_and_real_hook_wins_capture_race(self):
        self.use_claude()
        self.save(provider="claude", updated_at=19)
        self.snapshot = pane(title="✳ task")
        self.ticker.tick(now=20, monotonic=0)
        self.assertFalse(any(c[0] == "capture-pane" for c in self.commands))

        original_tmux = self.tmux

        def hook_during_capture(args, **kwargs):
            if args[0] == "capture-pane":
                self.save("working", "claude", updated_at=21.1)
            return original_tmux(args, **kwargs)

        with patch.object(status, "tmux", side_effect=hook_during_capture):
            self.ticker.tick(now=22, monotonic=2)
        self.assertEqual(status.load_record("%1")["state"], "working")
        self.assertEqual(status.load_record("%1")["updated_at"], 21.1)

    def test_idle_updates_only_once_and_orphan_cleanup_keeps_lock_inode(self):
        self.save("idle")
        self.save("idle", pane_id="%999")
        with status.record_lock("%999", directory=self.directory):
            pass
        self.ticker.tick(now=20, monotonic=0)
        self.assertFalse((self.directory / "%999.json").exists())
        self.assertTrue((self.directory / ".%999.lock").exists())
        self.commands.clear()
        self.ticker.tick(now=20.2, monotonic=.2)
        self.assertEqual([c[0] for c in self.commands], ["list-panes"])
        self.snapshot = ""
        self.ticker.tick(now=21, monotonic=1)
        self.assertFalse((self.directory / "%1.json").exists())


class ConsumerTests(unittest.TestCase):
    def test_sidebar_consumes_provider_and_state_options_and_preserves_name(self):
        fields = ["%1", "work", "@1", "1", "project", "2", "/work/repo", "A task | details",
                  "node", "my agent", "1", "1", "1", "", "waiting", "codex"]
        snapshot = sidebar.SEPARATOR.join(fields)
        # A plain node process with no published status is not guessed to be an agent.
        plain = fields.copy()
        plain[0], plain[14], plain[15] = "%2", "", ""
        with patch.object(sidebar, "tmux", return_value=snapshot + "\n" + sidebar.SEPARATOR.join(plain)):
            rows, _, _ = sidebar.collect_tree_and_active("work")
        panes = [r for r in rows if r["kind"] == "pane"]
        self.assertEqual(len(panes), 1)
        self.assertEqual(panes[0]["provider"], "codex")
        self.assertEqual(panes[0]["state"], "waiting")
        self.assertEqual(panes[0]["title"], "my agent · A task | details")
        self.assertEqual(sidebar.pane_detail(panes[0], 5), "codex")

    def test_popup_uses_same_published_state_and_excludes_empty_or_sidebar(self):
        rows = [
            ["%1", "work", "1", "project", "2", "/work/repo", "my agent", "waiting", "codex", ""],
            ["%2", "work", "1", "project", "3", "/work/repo", "", "idle", "claude", ""],
            ["%3", "work", "1", "project", "4", "/work/repo", "", "", "", ""],
            ["%4", "work", "1", "project", "5", "/work/repo", "", "idle", "claude", "1"],
        ]
        lines = popup.build_lines("\n".join(popup.SEPARATOR.join(row) for row in rows))
        self.assertEqual(len(lines), 3)  # One session heading plus two agents.
        self.assertIn("⏳", lines[1])
        self.assertIn("codex", lines[1])
        self.assertIn("my agent", lines[1])
        self.assertTrue(lines[1].endswith("\t%1"))
        self.assertIn("✓", lines[2])
        self.assertIn("claude", lines[2])


if __name__ == "__main__":
    unittest.main()
