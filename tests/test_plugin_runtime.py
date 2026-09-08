"""Configuration and process ownership checks for the TPM lifecycle."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import plugin_runtime as runtime
import tmux_status_ticker as ticker


class ConfigurationTests(unittest.TestCase):
    def test_defaults_and_ascii_indicators(self):
        with patch.object(runtime, "option", side_effect=lambda name, default="": default):
            config = runtime.config_from_tmux()
        self.assertEqual(config["interval"], .5)
        self.assertFalse(config["ascii"])
        for state in ticker.DEFAULT_COLORS:
            text = ticker.icon_for_state(state, config={"ascii": True})
            self.assertTrue(text.isascii())

    def test_invalid_polling_interval_or_tmux_format_injection_is_rejected(self):
        for values in ({"interval": "nan"}, {"interval": "0"}, {"interval": "100"},
                       {"ascii": "maybe"}, {"color-idle": "red]hello"},
                       {"symbol-idle": "#{pane_id}"}):
            with self.subTest(values=values), patch.object(runtime, "option", side_effect=lambda name, default="": values.get(name, default)):
                with self.assertRaises(ValueError):
                    runtime.config_from_tmux()

    def test_user_colors_and_symbols_are_rendered(self):
        config = {"colors": {"idle": "#123456"}, "symbols": {"idle": "done"}}
        self.assertEqual(ticker.icon_for_state("idle", config=config), " #[fg=#123456]done#[default]")


class OwnershipTests(unittest.TestCase):
    def test_binding_lookup_uses_native_alias_and_cleans_only_private_tables(self):
        probe = "agent-pulse-inspect-fixture"
        snapshot = (
            f'bind-key -T {probe} PPage display-message "AgentPulse key probe"\n'
            f'bind-key -T {probe}-sentinel PPage display-message "AgentPulse key probe"\n'
            'bind-key    -T prefix PPage display-message "foreign binding"\n'
        )
        with patch.object(runtime.uuid, "uuid4", return_value=SimpleNamespace(hex="fixture")), \
             patch.object(runtime.status, "tmux", side_effect=[snapshot, ""]) as tmux:
            found = runtime.binding("PgUp")
        self.assertEqual(found, "bind-key -T prefix PPage display-message 'foreign binding'")
        first, cleanup = [call.args[0] for call in tmux.call_args_list]
        self.assertNotIn("prefix", first)
        self.assertEqual(first[-1], "list-keys")
        self.assertNotIn("prefix", cleanup)
        self.assertEqual(cleanup, ["unbind-key", "-a", "-T", probe, ";",
                                   "unbind-key", "-a", "-T", probe + "-sentinel"])

    def test_incomplete_key_snapshot_fails_without_treating_key_as_unbound(self):
        with patch.object(runtime.status, "tmux", return_value="") as tmux:
            with self.assertRaisesRegex(RuntimeError, "complete key snapshot"):
                runtime.binding("F12")
        self.assertEqual(tmux.call_count, 2)
        self.assertEqual(tmux.call_args.args[0][0], "unbind-key")

    def test_reused_pid_is_not_a_ticker_even_if_lock_is_held(self):
        value = {"pid": 123, "start": "old", "root": str(runtime.ROOT)}
        with patch.object(runtime, "metadata", return_value=value), \
             patch.object(runtime, "lock_held", return_value=True), \
             patch.object(runtime.status, "process_snapshot", return_value={123: {"start": "new"}}), \
             patch.object(runtime.subprocess, "check_output") as ps:
            self.assertEqual(runtime.live_ticker(Path("/fixture")), {})
            ps.assert_not_called()

    def test_another_checkout_is_not_signalled(self):
        with self.assertRaisesRegex(RuntimeError, "Another AgentPulse checkout"):
            runtime.require_our_ticker({"root": "/another/checkout"})

    def test_user_replaced_binding_is_preserved(self):
        entry = {"P": {"root": str(runtime.ROOT), "definition": "old owned binding"}}
        with patch.object(runtime, "owned_bindings", return_value=entry), \
             patch.object(runtime, "binding", return_value="new foreign binding"), \
             patch.object(runtime.status, "tmux") as tmux:
            runtime.clear_bindings()
        self.assertFalse(any(call.args[0][0] == "unbind-key" for call in tmux.call_args_list))

    def test_foreign_prefix_binding_is_never_overwritten(self):
        values = {"popup-key": "P"}
        with patch.object(runtime, "clear_bindings"), \
             patch.object(runtime, "owned_bindings", return_value={}), \
             patch.object(runtime, "option", side_effect=lambda name, default="": values.get(name, default)), \
             patch.object(runtime, "binding", return_value="foreign binding"), \
             patch.object(runtime.status, "tmux") as tmux, contextlib.redirect_stderr(io.StringIO()) as output:
            runtime.configure_bindings()
        tmux.assert_not_called()
        self.assertIn("already bound", output.getvalue())

    def test_binding_replaced_during_install_is_not_claimed(self):
        with patch.object(runtime, "clear_bindings"), \
             patch.object(runtime, "owned_bindings", return_value={}), \
             patch.object(runtime, "option", side_effect=lambda name, default="": "P" if name == "popup-key" else default), \
             patch.object(runtime, "binding", side_effect=["", "bind-key -T prefix P display-message foreign"]), \
             patch.object(runtime.status, "tmux") as tmux:
            runtime.configure_bindings()
        self.assertEqual(tmux.call_count, 1)
        self.assertEqual(tmux.call_args.args[0][0], "bind-key")


if __name__ == "__main__":
    unittest.main()
