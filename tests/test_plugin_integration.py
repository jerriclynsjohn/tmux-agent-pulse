"""Exercise the public plugin entry point on disposable tmux servers."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
TPM = Path(os.environ.get("AGENT_PULSE_TPM_PATH", str(Path.home() / ".tmux/plugins/tpm")))


def wait_until(description, check, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError("Timed out: " + description)


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class PluginIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pulse-plugin-", dir="/tmp")
        self.directory = Path(self.temporary.name).resolve()
        self.socket = str(self.directory / "server.sock")
        self.checkout = self.directory / "custom plugins" / "tmux agent pulse"
        shutil.copytree(ROOT, self.checkout,
                        ignore=shutil.ignore_patterns(".git", ".test-deps", "__pycache__", "*.pyc"))
        self.states = self.directory / "state files"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("TMUX", "AGENT_PULSE_", "AGENT_STATUS_"))}
        self.env.update(TMUX=self.socket + ",1,0", AGENT_PULSE_DIR=str(self.states),
                        AGENT_PULSE_NOTIFY="0", PYTHONDONTWRITEBYTECODE="1", TERM="xterm-256color")
        # Pass an empty environment for tmux startup rather than nesting inside
        # the user's live server. Every later command addresses this exact socket.
        startup = dict(self.env)
        startup.pop("TMUX")
        try:
            subprocess.run(["tmux", "-S", self.socket, "-f", "/dev/null", "new-session",
                            "-d", "-s", "plugin-test", "-x", "120", "-y", "30",
                            "/bin/sleep", "120"], env=startup, check=True,
                           capture_output=True, text=True, timeout=10)
            if not Path(self.socket).exists():
                raise RuntimeError("tmux did not create the private socket")
        except Exception:
            self.temporary.cleanup()
            raise

    def tearDown(self):
        with contextlib.suppress(Exception):
            self.cli("unload")
        with contextlib.suppress(Exception):
            self.tmux("kill-server")
        self.temporary.cleanup()

    def tmux(self, *args):
        return subprocess.check_output(["tmux", "-S", self.socket, *args], env=self.env,
                                       text=True, stderr=subprocess.PIPE, timeout=10).rstrip("\n")

    def prefix_binding(self, key):
        # Read all rows: tmux 3.7c sends a one-row query to a status message.
        for line in self.tmux("list-keys").splitlines():
            parts = shlex.split(line)
            if "-T" in parts:
                index = parts.index("-T")
                if parts[index + 1:index + 3] == ["prefix", key]:
                    return shlex.join(parts)
        return ""

    def cli(self, *args):
        return subprocess.run([str(self.checkout / "bin/tmux-agent-pulse"), *args],
                              env=self.env, cwd=self.directory, check=True,
                              capture_output=True, text=True, timeout=15)

    def entry(self):
        return subprocess.run([str(self.checkout / "agent-pulse.tmux")], env=self.env,
                              cwd=self.directory, check=True, capture_output=True,
                              text=True, timeout=15)

    def lock_is_held(self, directory=None):
        try:
            with ((directory or self.states) / ".ticker.lock").open("a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(lock, fcntl.LOCK_UN)
        except FileNotFoundError:
            pass
        return False

    def test_repeated_tpm_load_in_custom_path_preserves_other_plugins(self):
        self.tmux("set-option", "-g", "status-left", "USER THEME")
        self.tmux("set-window-option", "-g", "window-status-format", "#I:#W user")
        self.tmux("bind-key", "-T", "prefix", "F12", "display-message", "foreign binding")
        self.tmux("set-option", "-g", "@agent-pulse-popup-key", "F12")
        for hook in ("after-new-window", "window-resized"):
            self.tmux("set-hook", "-g", hook + "[1000]", "set-option -g @foreign-fired yes")
        self.tmux("set-option", "-p", "@foreign-pane", "keep")
        before = {
            "panes": self.tmux("list-panes", "-a", "-F", "#{pane_id}|#{pane_pid}"),
            "bindings": self.tmux("list-keys"),
            "theme": self.tmux("show-options", "-g", "-v", "status-left"),
            "window_theme": self.tmux("show-window-options", "-g", "-v", "window-status-format"),
            "hooks": {name: self.tmux("show-hooks", "-g", name)
                      for name in ("after-new-window", "window-resized")},
        }
        self.entry()
        wait_until("ticker lock", self.lock_is_held)
        first_pid = (self.states / ".ticker.lock").read_text().strip()
        self.entry()
        self.assertEqual((self.states / ".ticker.lock").read_text().strip(), first_pid)
        self.assertTrue(self.lock_is_held())
        metadata = json.loads((self.states / "ticker.json").read_text())
        self.assertEqual(metadata["pid"], int(first_pid))
        self.assertEqual(Path(metadata["root"]).resolve(), self.checkout)
        self.assertEqual(Path(metadata["socket"]).resolve(), Path(self.socket))
        self.assertEqual(self.tmux("list-panes", "-a", "-F", "#{pane_id}|#{pane_pid}"), before["panes"])
        self.assertEqual(self.tmux("list-keys"), before["bindings"])
        self.assertEqual(self.tmux("show-options", "-g", "-v", "status-left"), before["theme"])
        self.assertEqual(self.tmux("show-window-options", "-g", "-v", "window-status-format"), before["window_theme"])
        for name, previous in before["hooks"].items():
            self.assertEqual(self.tmux("show-hooks", "-g", name), previous)
        self.cli("unload")
        wait_until("ticker stopped", lambda: not self.lock_is_held())
        self.assertEqual(self.tmux("list-panes", "-a", "-F", "#{pane_id}|#{pane_pid}"), before["panes"])
        self.assertEqual(self.tmux("list-keys"), before["bindings"])
        self.assertEqual(self.tmux("show-options", "-p", "-v", "@foreign-pane"), "keep")
        for name, previous in before["hooks"].items():
            self.assertEqual(self.tmux("show-hooks", "-g", name), previous)

    def test_unload_preserves_replaced_binding_and_removes_owned_binding(self):
        self.tmux("set-option", "-g", "@agent-pulse-popup-key", "F11")
        self.tmux("set-option", "-g", "@agent-pulse-sidebar-key", "F10")
        self.entry()
        self.assertIn("tmux-agent-pulse", self.prefix_binding("F11"))
        self.assertIn("tmux-agent-pulse", self.prefix_binding("F10"))
        self.tmux("bind-key", "-T", "prefix", "F11", "display-message", "replacement binding")
        replaced = self.prefix_binding("F11")
        self.cli("unload")
        wait_until("ticker stopped", lambda: not self.lock_is_held())
        self.assertEqual(self.prefix_binding("F11"), replaced)
        remaining = self.tmux("list-keys", "-T", "prefix")
        self.assertFalse(any(" F10 " in line for line in remaining.splitlines()))

    def test_native_key_aliases_preserve_foreign_bindings_without_leaving_probe_tables(self):
        for existing, requested in (("PageUp", "PgUp"), ("C-i", "C-i")):
            with self.subTest(existing=existing):
                self.tmux("bind-key", "-T", "prefix", existing, "display-message", "keep alias")
                self.tmux("set-option", "-g", "@agent-pulse-popup-key", requested)
                before = self.tmux("list-keys")
                result = self.entry()
                self.assertIn("already bound", result.stderr)
                self.assertEqual(self.tmux("list-keys"), before)
                self.assertNotIn("agent-pulse-inspect-", self.tmux("list-keys"))

    @unittest.skipUnless((TPM / "tpm").is_file(), "set AGENT_PULSE_TPM_PATH to a TPM checkout")
    def test_real_tpm_discovers_entry_in_custom_plugin_directory(self):
        home = self.directory / "home"
        home.mkdir()
        xdg = home / ".config"
        (xdg / "tmux").mkdir(parents=True)
        (xdg / "tmux/tmux.conf").write_text("set -g @plugin 'local/tmux-agent-pulse'\n")
        plugins = self.directory / "tpm-plugins"
        plugins.mkdir()
        # TPM itself splits paths with spaces in its discovery loop. A symlink
        # gives TPM a normal install path while the actual checkout has spaces.
        (plugins / "tmux-agent-pulse").symlink_to(self.checkout, target_is_directory=True)
        self.tmux("set-environment", "-g", "TMUX_PLUGIN_MANAGER_PATH", str(plugins) + "/")
        env = dict(self.env, HOME=str(home), XDG_CONFIG_HOME=str(xdg))
        subprocess.run([str(TPM / "tpm")], env=env, check=True, capture_output=True,
                       text=True, timeout=20)
        wait_until("ticker started through TPM discovery", self.lock_is_held)
        first = (self.states / ".ticker.lock").read_text()
        metadata = json.loads((self.states / "ticker.json").read_text())
        self.assertEqual(Path(metadata["root"]).resolve(), self.checkout)
        self.assertEqual(self.tmux("list-panes", "-a", "-F", "#{pane_id}").splitlines(), ["%0"])
        subprocess.run([str(TPM / "tpm")], env=env, check=True, capture_output=True,
                       text=True, timeout=20)
        self.assertEqual((self.states / ".ticker.lock").read_text(), first)

    def test_default_runtime_directories_isolate_two_servers(self):
        second_socket = str(self.directory / "second.sock")
        startup = dict(self.env)
        startup.pop("TMUX")
        subprocess.run(["tmux", "-S", second_socket, "-f", "/dev/null", "new-session",
                        "-d", "-s", "second", "/bin/sleep", "120"], env=startup,
                       check=True, capture_output=True, text=True, timeout=10)
        sockets = (self.socket, second_socket)
        directories = {socket: Path("/tmp") / f"tmux-agent-pulse-{os.getuid()}" /
                       hashlib.sha256(str(Path(socket).resolve()).encode()).hexdigest()[:16]
                       for socket in sockets}

        def run(socket, *args):
            env = dict(self.env, TMUX=socket + ",1,0")
            env.pop("AGENT_PULSE_DIR")
            return subprocess.run([str(self.checkout / "bin/tmux-agent-pulse"), *args],
                                  env=env, check=True, capture_output=True, text=True, timeout=15)

        try:
            for socket in sockets:
                self.assertFalse(directories[socket].exists(), "Each test starts with a unique server path")
                run(socket, "load")
                result = json.loads(run(socket, "doctor", "--json").stdout)
                self.assertEqual(Path(result["state_directory"]), directories[socket])
                self.assertEqual(Path(result["ticker"]["socket"]).resolve(), Path(socket))
            identities = [json.loads((directories[socket] / "ticker.json").read_text()) for socket in sockets]
            self.assertNotEqual(identities[0]["pid"], identities[1]["pid"])
            run(self.socket, "unload")
            self.assertFalse(self.lock_is_held(directories[self.socket]))
            self.assertTrue(self.lock_is_held(directories[second_socket]))
            run(second_socket, "unload")
            self.assertFalse(self.lock_is_held(directories[second_socket]))
        finally:
            for socket in sockets:
                with contextlib.suppress(Exception):
                    run(socket, "unload")
            subprocess.run(["tmux", "-S", second_socket, "kill-server"], env=startup,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            for directory in directories.values():
                if not self.lock_is_held(directory):
                    shutil.rmtree(directory, ignore_errors=True)

    @unittest.skipUnless(shutil.which("cc"), "a C compiler is needed for the process fixture")
    def test_real_owner_records_publish_namespaced_state_and_clear_on_unload(self):
        source = self.directory / "fixture.c"
        source.write_text(r'''
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc != 4) return 2;
    while (access(argv[1], F_OK) != 0) usleep(10000);
    int input[2];
    if (pipe(input) != 0) return 3;
    pid_t child = fork();
    if (child < 0) return 4;
    if (child == 0) {
        close(input[1]);
        if (dup2(input[0], STDIN_FILENO) < 0) _exit(5);
        close(input[0]);
        setenv("TMUX_PANE", "%999", 1);
        execl(argv[2], argv[2], argv[3], "codex", (char *)NULL);
        _exit(6);
    }
    close(input[0]);
    const char event[] = "{\"hook_event_name\":\"UserPromptSubmit\","
                         "\"session_id\":\"plugin-smoke\",\"turn_id\":\"first\"}";
    if (write(input[1], event, sizeof(event) - 1) != sizeof(event) - 1) return 7;
    close(input[1]);
    int result;
    if (waitpid(child, &result, 0) != child || !WIFEXITED(result) || WEXITSTATUS(result)) return 8;
    sleep(120);
    return 0;
}
''')
        binary = self.directory / "codex"
        trigger = self.directory / "submit-hook"
        subprocess.run([shutil.which("cc"), str(source), "-o", str(binary)], check=True,
                       capture_output=True, text=True, timeout=30)
        pane = self.tmux("split-window", "-d", "-P", "-F", "#{pane_id}", str(binary),
                         str(trigger), sys.executable, str(self.checkout / "lib/agent_pulse.py"))
        self.tmux("set-option", "-p", "-t", pane, "@agent-state", "foreign-state")
        self.tmux("set-option", "-g", "@agent-pulse-ascii", "on")
        self.entry()
        state = lambda: self.tmux("display-message", "-p", "-t", pane, "#{@agent-pulse-state}")
        wait_until("unknown live owner", lambda: state() == "unknown")
        # The event is synthetic. Its hook process is a real descendant of the
        # detected owner, with a deliberately incorrect inherited TMUX_PANE.
        # It must route by ancestry without an explicit pane override.
        trigger.touch()
        wait_until("working state", lambda: state() == "working")
        self.assertEqual(self.tmux("display-message", "-p", "-t", pane, "#{@agent-pulse-provider}"), "codex")
        self.assertTrue(self.tmux("display-message", "-p", "-t", pane, "#{@agent-pulse-icon}"))
        self.assertTrue(self.tmux("display-message", "-p", "-t", pane, "#{@agent-pulse-icon}").isascii())
        self.assertEqual(self.tmux("show-window-options", "-t", "plugin-test", "-v", "@agent-pulse-window-state"), "working")
        self.assertEqual(self.tmux("display-message", "-p", "-t", pane, "#{@agent-state}"), "foreign-state")
        self.cli("unload")
        wait_until("ticker stopped", lambda: not self.lock_is_held())
        self.assertEqual(state(), "")
        self.assertEqual(self.tmux("display-message", "-p", "-t", pane, "#{@agent-state}"), "foreign-state")
        self.assertEqual(self.tmux("display-message", "-p", "-t", pane, "#{pane_dead}"), "0")
        self.assertNotIn("@agent-pulse-window-state", self.tmux("show-window-options", "-t", "plugin-test"))


if __name__ == "__main__":
    unittest.main()
