#!/usr/bin/env python3
"""Exercise hooks -> metadata -> real ticker -> tmux UI on a private server.

Run: python3 -B tests/tmux_status_smoke.py
Requires tmux, a C compiler, and permission to create a local Unix socket. No
model, live user pane, network request, or desktop notification is used. Tiny
compiled programs named claude/codex sleep, with real process discovery.
"""

import contextlib
import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
BIN = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(BIN))
import agent_pulse as status
import tmux_agent_tree as popup
import tmux_status_ticker as rendering

spec = importlib.util.spec_from_file_location("tmux_sidebar", BIN / "tmux-agent-sidebar.py")
sidebar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sidebar)


@contextlib.contextmanager
def environment(values):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def wait_until(description, check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError(f"Timed out: {description}")


class SmokeTest:
    def __init__(self, directory, compiler):
        self.directory = directory
        self.compiler = compiler
        self.socket = str(directory / "tmux.sock")
        self.states = directory / "states"
        self.panes = {}
        self.owners = {}
        self.ticker = None
        self.log = None
        self.log_path = directory / "ticker.log"

    def tmux(self, args):
        return status.tmux(args, socket_path=self.socket)

    def option(self, pane, name):
        return self.tmux(["display-message", "-p", "-t", pane, "#{" + name + "}"])

    def pane_command(self, provider):
        binary = self.directory / provider
        source = self.directory / "sleep_fixture.c"
        source.write_text("#include <unistd.h>\nint main(void) { sleep(300); return 0; }\n")
        subprocess.run([self.compiler, str(source), "-o", str(binary)],
                       check=True, capture_output=True, timeout=30)
        # The parent shell stays in the pane when the synthetic agent exits.
        return ["/bin/sh", "-c", f"{shlex.quote(str(binary))}; exec /bin/sh -i"]

    def start(self):
        config = self.directory / "tmux.conf"
        config.write_text("set -g default-shell /bin/sh\nset -g status off\n")
        command = ["tmux", "-S", self.socket, "-f", str(config), "new-session", "-d",
                   "-s", "agent-smoke", "-x", "120", "-y", "30"] + self.pane_command("claude")
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        # tmux can return zero even when the server cannot create its socket.
        if result.returncode or not Path(self.socket).exists():
            raise RuntimeError("Private tmux server could not start: " + result.stderr.strip())
        self.panes["claude"] = self.tmux(["display-message", "-p", "-t", "agent-smoke", "#{pane_id}"])
        self.panes["codex"] = self.tmux(["split-window", "-h", "-d", "-t", self.panes["claude"],
                                         "-P", "-F", "#{pane_id}"] + self.pane_command("codex"))
        for provider, pane in self.panes.items():
            self.tmux(["set-option", "-p", "-t", pane, "@agent-pulse-name", f"smoke-{provider}"])
            pane_pid = int(self.option(pane, "pane_pid"))

            def detect():
                owner = status.find_owner(pane_pid, status.process_snapshot())
                return owner if owner and owner["provider"] == provider else None

            self.owners[provider] = wait_until(f"real {provider} process discovery", detect)
        self.log = self.log_path.open("w")
        self.ticker = subprocess.Popen([sys.executable, "-B", str(BIN / "tmux_status_ticker.py")],
                                       stdout=self.log, stderr=self.log)
        for provider in self.panes:
            self.assert_ui(provider, "unknown")
        assert int((self.states / ".ticker.lock").read_text()) == self.ticker.pid

    def emit(self, provider, name, **payload):
        event = {"hook_event_name": name, "session_id": f"smoke-{provider}",
                 "turn_id": f"smoke-{provider}-turn", **payload}
        # Events originate from this test runner, outside the synthetic agent.
        # The native CLI smoke test covers ancestry-based routing separately.
        status.handle_hook(provider, event, pane=self.panes[provider], socket=self.socket)

    def assert_ui(self, provider, expected):
        pane = self.panes[provider]

        def published():
            if self.ticker and self.ticker.poll() is not None:
                raise AssertionError("Ticker exited: " + self.log_path.read_text())
            return (self.option(pane, "@agent-pulse-state") == expected and
                    self.option(pane, "@agent-pulse-provider") == provider)

        wait_until(f"{provider} {expected} pane options", published)
        icon = self.option(pane, "@agent-pulse-icon")
        if expected == "working":
            assert any(glyph in icon for glyph in rendering.SPINNER), icon
        else:
            assert {"waiting": "⏳", "idle": "✓", "cancelled": "✗", "unknown": "○"}[expected] in icon, icon
        rows = [r for r in sidebar.collect_tree("agent-smoke") if r["kind"] == "pane"]
        row = next(r for r in rows if r["pane_id"] == pane)
        assert row["state"] == expected and row["provider"] == provider, row
        assert row["title"].startswith(f"smoke-{provider}"), row
        lines = popup.build_lines(self.tmux(["list-panes", "-a", "-F", popup.PANE_FORMAT]))
        line = next(line for line in lines if line.endswith("\t" + pane))
        assert provider in line and popup.STYLE[expected][0] in line, line

    def assert_window(self, glyph):
        wait_until(f"window icon {glyph}", lambda: glyph in self.tmux(
            ["display-message", "-p", "-t", "agent-smoke", "#{@agent-pulse-window-icon}"]))

    def lifecycle(self, provider):
        self.emit(provider, "SessionStart")
        self.assert_ui(provider, "idle")
        self.emit(provider, "UserPromptSubmit")
        self.assert_ui(provider, "working")
        question = "AskUserQuestion" if provider == "claude" else "functions.request_user_input"
        call = {"tool_name": question, "tool_use_id": f"{provider}-question",
                "tool_input": {"questions": [{"question": "Synthetic smoke question"}]}}
        self.emit(provider, "PreToolUse", **call)
        self.assert_ui(provider, "waiting")
        self.emit(provider, "PostToolUse", **call, tool_response={"answers": {"question": "yes"}})
        self.assert_ui(provider, "working")
        tool = "Bash" if provider == "claude" else "functions.exec_command"
        permission = {"tool_name": tool, "tool_use_id": f"{provider}-permission",
                      "tool_input": {"command": "synthetic command; never executed"}}
        self.emit(provider, "PermissionRequest", **permission)
        self.assert_ui(provider, "waiting")
        self.emit(provider, "PostToolUse", tool_name=tool, tool_use_id="unrelated-call",
                  tool_input={"command": "different synthetic command"}, tool_response={"ok": True})
        assert status.load_record(self.panes[provider])["state"] == "waiting"
        self.emit(provider, "PostToolUse", **permission, tool_response={"ok": True})
        self.assert_ui(provider, "working")
        self.emit(provider, "Stop")
        self.assert_ui(provider, "idle")
        self.emit(provider, "Interrupt")
        self.assert_ui(provider, "cancelled")
        self.emit(provider, "UserPromptSubmit")
        self.emit(provider, "Stop")
        self.assert_ui(provider, "idle")
        print(f"PASS: {provider} lifecycle, question/approval response, interruption, and all three renderers", flush=True)

    def priority_and_cleanup(self):
        self.emit("claude", "UserPromptSubmit")
        self.emit("codex", "UserPromptSubmit")
        self.emit("codex", "PreToolUse", tool_name="functions.request_user_input", tool_use_id="priority-question")
        self.assert_ui("codex", "waiting")
        self.emit("claude", "Stop")
        self.assert_ui("claude", "idle")
        self.tmux(["select-pane", "-t", self.panes["claude"]])
        self.assert_window("⏳")
        # Pane options shadow window options with the same name in formats.
        # Separate names must retain waiting even while the idle pane is active.
        pane_icon, window_icon = self.tmux([
            "display-message", "-p", "-t", self.panes["claude"],
            "#{@agent-pulse-icon}|#{@agent-pulse-window-icon}"]).split("|", 1)
        assert "✓" in pane_icon and "⏳" in window_icon, (pane_icon, window_icon)
        self.emit("codex", "PostToolUse", tool_name="functions.request_user_input",
                  tool_use_id="priority-question", tool_response={"answers": {"q": "yes"}})
        self.emit("claude", "Interrupt")
        self.assert_ui("claude", "cancelled")
        self.assert_ui("codex", "working")
        wait_until("working beats cancelled", lambda: any(glyph in self.tmux(
            ["display-message", "-p", "-t", "agent-smoke", "#{@agent-pulse-window-icon}"])
            for glyph in rendering.SPINNER))
        self.emit("codex", "Stop")
        self.assert_ui("codex", "idle")
        self.assert_window("✗")

        claude_pane = self.panes["claude"]
        # This PID was discovered only beneath our newly created private pane.
        os.kill(self.owners["claude"]["pid"], signal.SIGTERM)
        wait_until("cancelled clears when synthetic owner exits", lambda:
                   self.option(claude_pane, "@agent-pulse-state") == "" and
                   self.option(claude_pane, "@agent-pulse-provider") == "" and
                   self.option(claude_pane, "@agent-pulse-icon") == "")
        assert self.option(claude_pane, "pane_dead") == "0", "The synthetic pane should keep its shell"
        assert status.find_owner(int(self.option(claude_pane, "pane_pid")), status.process_snapshot()) is None
        self.assert_window("✓")
        assert all(r.get("pane_id") != claude_pane for r in sidebar.collect_tree("agent-smoke"))
        lines = popup.build_lines(self.tmux(["list-panes", "-a", "-F", popup.PANE_FORMAT]))
        assert all(not line.endswith("\t" + claude_pane) for line in lines)
        self.tmux(["kill-pane", "-t", claude_pane])
        wait_until("removed pane metadata deleted", lambda: not status.load_record(claude_pane))
        self.emit("codex", "SessionEnd")
        assert not status.load_record(self.panes["codex"])
        print("PASS: mixed-provider priority, cancelled owner exit with surviving shell, orphan removal, and session cleanup", flush=True)

    def close(self):
        if self.ticker is not None and self.ticker.poll() is None:
            self.ticker.terminate()
            try:
                self.ticker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.ticker.kill()
                self.ticker.wait(timeout=5)
        if self.log is not None:
            self.log.close()
        with contextlib.suppress(Exception):
            self.tmux(["kill-server"])


def main():
    compiler = shutil.which("cc")
    if compiler is None:
        print("SKIP: this smoke test needs a C compiler to build its temporary agent fixtures")
        return
    with tempfile.TemporaryDirectory(prefix="agent-pulse-smoke-", dir="/tmp") as temporary:
        directory = Path(temporary)
        test = SmokeTest(directory, compiler)
        with environment({"TMUX": test.socket + ",1,0", "TMUX_PANE": "%0",
                          "AGENT_PULSE_DIR": str(test.states), "AGENT_PULSE_NOTIFY": "0",
                          "AGENT_PULSE_PANE": "", "AGENT_PULSE_SOCKET": ""}):
            try:
                test.start()
                test.lifecycle("claude")
                test.lifecycle("codex")
                test.priority_and_cleanup()
            finally:
                test.close()
    print("PASS: private server and temporary files removed; no real model or live user pane involved")


if __name__ == "__main__":
    main()
