"""Provider config lifecycle in isolated homes; never touch the user's settings."""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("install_hooks", ROOT / "lib/install_hooks.py")
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


class InstallHooksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent pulse install ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.claude = self.home / "claude config"
        self.codex = self.home / "codex config"
        self.state = self.home / "state"
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.claude),
                                         "CODEX_HOME": str(self.codex), "XDG_STATE_HOME": str(self.state)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    @property
    def manifest_path(self):
        return self.state / "tmux-agent-pulse/install-manifest.json"

    def run_cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            status = installer.main(list(args))
        return status, output.getvalue()

    def write(self, provider, value):
        path = (self.claude if provider == "claude" else self.codex) / installer.PROVIDERS[provider]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=4) + "\n")
        return path

    def read(self, provider):
        return json.loads(((self.claude if provider == "claude" else self.codex) / installer.PROVIDERS[provider]).read_text())

    def assert_ok(self, *args):
        status, output = self.run_cli(*args)
        self.assertEqual(status, 0, output)
        return output

    def test_preview_creates_no_files_or_directories(self):
        output = self.assert_ok()
        self.assertIn("Preview only", output)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_preview_existing_configs_leaves_original_bytes(self):
        path = self.write("claude", {"permissions": {"allow": ["Bash(git status:*)"]}})
        original = path.read_bytes()
        self.assert_ok("--provider", "claude")
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(self.state.exists())

    def test_provider_selection_installs_only_chosen_provider(self):
        self.assert_ok("--apply", "--provider", "claude")
        self.assertFalse(self.codex.exists())
        config = self.read("claude")
        self.assertEqual(len(config["hooks"]), 9)
        manifest = json.loads(self.manifest_path.read_text())
        self.assertEqual([entry["provider"] for entry in manifest["installations"]], ["claude"])

    def test_command_quotes_interpreter_and_plugin_paths(self):
        plugin = self.home / "plugin's directory"
        interpreter = self.home / "python's binary"
        with patch.object(installer, "ROOT", plugin), patch.object(installer.sys, "executable", str(interpreter)):
            command = installer.hook_command("codex")
        self.assertEqual(shlex.split(command), [str(interpreter), str(plugin / "bin/tmux-agent-pulse"), "hook", "codex"])

    def test_home_flags_take_precedence_over_environment(self):
        explicit = self.home / "explicit"
        self.assert_ok("--apply", "--provider", "codex", "--codex-home", str(explicit))
        self.assertTrue((explicit / "hooks.json").exists())
        self.assertFalse(self.codex.exists())
        self.assertEqual(json.loads(self.manifest_path.read_text())["installations"][0]["path"], str(explicit / "hooks.json"))

    def test_unrelated_hooks_and_settings_survive_install_and_uninstall(self):
        foreign = {"type": "command", "command": "echo agent-pulse; cat ~/.claude/bin/tmux-status.sh", "timeout": 8}
        original = {"permissions": {"allow": ["WebFetch"]}, "hooks": {
            "Stop": [{"matcher": "*", "hooks": [foreign]}], "AnotherEvent": [{"hooks": [foreign]}],
            "SessionStart": [], "UserPromptSubmit": [{"hooks": []}],
        }}
        self.write("claude", original)
        self.assert_ok("--apply", "--provider", "claude")
        installed = self.read("claude")
        self.assertEqual(installed["hooks"]["Stop"][0], original["hooks"]["Stop"][0])
        self.assertEqual(installed["permissions"], original["permissions"])
        self.assert_ok("--uninstall", "--apply")
        self.assertEqual(self.read("claude"), original)

    def test_install_is_idempotent_without_new_backup(self):
        self.assert_ok("--apply")
        before = {path: path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        output = self.assert_ok("--apply")
        after = {path: path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        self.assertIn("No changes needed", output)
        self.assertEqual(before, after)

    def test_atomic_write_preserves_config_symlink_and_mode(self):
        target = self.home / "dotfiles/claude.json"
        target.parent.mkdir()
        target.write_text('{"unrelated": true}\n')
        target.chmod(0o640)
        self.claude.mkdir()
        path = self.claude / "settings.json"
        path.symlink_to(target)
        self.assert_ok("--apply", "--provider", "claude")
        self.assertTrue(path.is_symlink())
        self.assertEqual(path.resolve(), target.resolve())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.assertTrue(json.loads(target.read_text())["unrelated"])
        self.assert_ok("--uninstall", "--apply")
        self.assertTrue(path.is_symlink())
        self.assertEqual(json.loads(target.read_text()), {"unrelated": True})

    def test_backups_contain_exact_prior_bytes_and_mapping(self):
        path = self.write("codex", {"hooks": {}, "unknown": [1, 2]})
        original = path.read_bytes()
        self.assert_ok("--apply", "--provider", "codex")
        backups = list((self.state / "tmux-agent-pulse/backups").iterdir())
        self.assertEqual(len(backups), 1)
        index = json.loads((backups[0] / "index.json").read_text())
        saved = next(entry for entry in index if entry["path"] == str(path))
        self.assertEqual((backups[0] / saved["backup"]).read_bytes(), original)
        self.assertEqual(stat.S_IMODE((backups[0] / saved["backup"]).stat().st_mode), 0o600)

    def test_uninstall_keeps_user_edited_hook_and_other_hooks_in_same_group(self):
        self.assert_ok("--apply", "--provider", "codex")
        config = self.read("codex")
        changed = config["hooks"]["Stop"][0]["hooks"][0]
        changed["timeout"] = 10
        extra = {"type": "command", "command": "echo preserve me"}
        config["hooks"]["PreToolUse"][0]["hooks"].append(extra)
        self.write("codex", config)
        output = self.assert_ok("--uninstall", "--apply")
        self.assertIn("Kept edited codex Stop", output)
        self.assertEqual(self.read("codex"), {"hooks": {
            "PreToolUse": [{"hooks": [extra]}], "Stop": [{"hooks": [changed]}],
        }})
        self.assertEqual(json.loads(self.manifest_path.read_text())["installations"], [])

    def test_edited_group_matcher_survives_uninstall(self):
        self.assert_ok("--apply", "--provider", "claude")
        config = self.read("claude")
        config["hooks"]["SessionStart"][0]["matcher"] = "resume"
        changed_group = config["hooks"]["SessionStart"][0]
        self.write("claude", config)
        self.assert_ok("--uninstall", "--apply")
        self.assertEqual(self.read("claude"), {"hooks": {"SessionStart": [changed_group]}})

    def test_reinstall_refuses_to_overwrite_edits_before_any_provider_write(self):
        self.assert_ok("--apply")
        config = self.read("codex")
        config["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 9
        codex_path = self.write("codex", config)
        claude_path = self.write("claude", {"unrelated": True})
        original = [path.read_bytes() for path in (codex_path, claude_path, self.manifest_path)]
        status, output = self.run_cli("--apply")
        self.assertEqual(status, 1)
        self.assertIn("a managed hook was edited", output)
        self.assertEqual([path.read_bytes() for path in (codex_path, claude_path, self.manifest_path)], original)

    def test_uninstall_removes_new_empty_files_but_preserves_added_fields(self):
        self.assert_ok("--apply")
        config = self.read("claude")
        config["theme"] = "dark"
        self.write("claude", config)
        self.assert_ok("--uninstall", "--apply")
        self.assertEqual(self.read("claude"), {"theme": "dark"})
        self.assertFalse((self.codex / "hooks.json").exists())

    def test_uninstall_without_manifest_does_not_claim_matching_commands(self):
        config, _ = installer.merge_install({}, "claude")
        path = self.write("claude", config)
        before = path.read_bytes()
        self.assert_ok("--uninstall", "--apply")
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.manifest_path.exists())

    def test_custom_profiles_are_recorded_and_can_be_selectively_removed(self):
        custom = self.home / "second codex"
        self.assert_ok("--apply", "--provider", "codex")
        self.assert_ok("--apply", "--provider", "codex", "--codex-home", str(custom))
        self.assertEqual(len(json.loads(self.manifest_path.read_text())["installations"]), 2)
        self.assert_ok("--uninstall", "--apply", "--codex-home", str(custom))
        self.assertFalse((custom / "hooks.json").exists())
        self.assertTrue((self.codex / "hooks.json").exists())
        # Default uninstall uses recorded homes even if environment values changed.
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home / "unrecorded")}):
            self.assert_ok("--uninstall", "--apply")
        self.assertFalse((self.codex / "hooks.json").exists())

    def test_install_refuses_a_second_alias_of_a_recorded_configuration(self):
        original = {"unrelated": True}
        target = self.write("claude", original)
        self.assert_ok("--apply", "--provider", "claude")
        alias_home = self.home / "alias profile"
        alias_home.mkdir()
        alias = alias_home / "settings.json"
        alias.symlink_to(target)
        before = target.read_bytes(), self.manifest_path.read_bytes()

        status, output = self.run_cli("--apply", "--provider", "claude",
                                      "--claude-home", str(alias_home))

        self.assertEqual(status, 1, output)
        self.assertIn("share a target", output)
        self.assertEqual((target.read_bytes(), self.manifest_path.read_bytes()), before)
        self.assertTrue(alias.is_symlink())
        self.assert_ok("--uninstall", "--apply")
        self.assertEqual(json.loads(target.read_text()), original)

    def test_uninstall_recovers_previously_recorded_aliases(self):
        self.assert_ok("--apply", "--provider", "claude")
        target = self.claude / "settings.json"
        alias_home = self.home / "alias profile"
        alias_home.mkdir()
        alias = alias_home / "settings.json"
        alias.symlink_to(target)
        manifest = json.loads(self.manifest_path.read_text())
        # Older installers recorded this second path after finding all hooks
        # already present. Put it first to verify ownership is order-independent.
        duplicate = json.loads(json.dumps(manifest["installations"][0]))
        duplicate.update(path=str(alias), created_file=False, created_hooks=False,
                         created_events=[])
        manifest["installations"].insert(0, duplicate)
        self.manifest_path.write_text(json.dumps(manifest))

        self.assert_ok("--uninstall", "--apply")

        self.assertFalse(target.exists())
        self.assertTrue(alias.is_symlink())
        self.assertEqual(json.loads(self.manifest_path.read_text())["installations"], [])
        self.assert_ok("--apply", "--provider", "claude")
        self.assertTrue(target.exists())

    def test_uninstall_by_recorded_alias_preserves_edits_and_other_profiles(self):
        foreign = {"hooks": [{"type": "command", "command": "echo keep"}]}
        target = self.write("claude", {"unrelated": True, "hooks": {"Stop": [foreign]}})
        self.assert_ok("--apply", "--provider", "claude")
        config = self.read("claude")
        config["hooks"]["SessionStart"][0]["matcher"] = "resume"
        edited = config["hooks"]["SessionStart"][0]
        self.write("claude", config)
        other_home = self.home / "independent profile"
        self.assert_ok("--apply", "--provider", "claude", "--claude-home", str(other_home))
        other_path = other_home / "settings.json"
        other_before = other_path.read_bytes()
        alias_home = self.home / "alias profile"
        alias_home.mkdir()
        alias = alias_home / "settings.json"
        alias.symlink_to(target)
        manifest = json.loads(self.manifest_path.read_text())
        duplicate = json.loads(json.dumps(manifest["installations"][0]))
        duplicate.update(path=str(alias), created_file=False, created_hooks=False,
                         created_events=[])
        manifest["installations"].append(duplicate)
        self.manifest_path.write_text(json.dumps(manifest))

        output = self.assert_ok("--uninstall", "--apply", "--provider", "claude",
                                "--claude-home", str(alias_home))

        self.assertIn("Kept edited claude SessionStart", output)
        self.assertEqual(self.read("claude"), {"unrelated": True, "hooks": {
            "Stop": [foreign], "SessionStart": [edited]}})
        self.assertTrue(alias.is_symlink())
        self.assertEqual(other_path.read_bytes(), other_before)
        remaining = json.loads(self.manifest_path.read_text())["installations"]
        self.assertEqual([entry["path"] for entry in remaining], [str(other_path)])

    def test_checkout_migration_updates_only_recorded_unchanged_commands(self):
        self.assert_ok("--apply", "--provider", "claude")
        old = installer.hook_command("claude")
        new = shlex.join([sys.executable, str(self.home / "new path/bin/tmux-agent-pulse"), "hook", "claude"])
        with patch.object(installer, "hook_command", return_value=new):
            self.assert_ok("--apply", "--provider", "claude")
        serialized = json.dumps(self.read("claude"))
        self.assertNotIn(old, serialized)
        for groups in self.read("claude")["hooks"].values():
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["hooks"][0]["command"], new)

    def test_invalid_second_provider_config_prevents_first_provider_write(self):
        self.codex.mkdir()
        (self.codex / "hooks.json").write_text("{broken")
        status, _ = self.run_cli("--apply")
        self.assertEqual(status, 1)
        self.assertFalse(self.claude.exists())
        self.assertFalse(self.manifest_path.exists())

    def test_failed_atomic_write_rolls_back_earlier_provider(self):
        path = self.write("claude", {"unrelated": "original"})
        before = path.read_bytes()
        actual = installer.atomic_write

        def fail_codex(snapshot, data):
            if snapshot.path == self.codex / "hooks.json":
                raise OSError("simulated disk failure")
            return actual(snapshot, data)

        with patch.object(installer, "atomic_write", side_effect=fail_codex):
            status, output = self.run_cli("--apply")
        self.assertEqual(status, 1)
        self.assertIn("simulated disk failure", output)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.manifest_path.exists())

    def test_changed_snapshot_does_not_overwrite_an_external_edit(self):
        path = self.write("claude", {"old": 1})
        snapshot = installer.Snapshot.read(path)
        path.write_text('{"new":2}\n')
        with self.assertRaisesRegex(RuntimeError, "configuration changed"):
            installer.apply_changes([(snapshot, b"{}\n")], self.state)
        self.assertEqual(path.read_text(), '{"new":2}\n')
        self.assertFalse(self.state.exists())

    def test_codex_install_does_not_change_trust_or_other_files(self):
        self.codex.mkdir()
        config = self.codex / "config.toml"
        config.write_text('[hooks.state.example]\ntrusted_hash = "existing"\n')
        before = config.read_bytes()
        output = self.assert_ok("--apply", "--provider", "codex")
        self.assertEqual(config.read_bytes(), before)
        self.assertIn("does not change hook trust", output)
        self.assertIn("/hooks", output)


if __name__ == "__main__":
    unittest.main()
