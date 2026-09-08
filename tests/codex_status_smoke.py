#!/usr/bin/env python3
"""Exercise installed Codex hooks in two private tmux panes without model calls.

Run manually: python3 tests/codex_status_smoke.py
Only temporary config, hook trust, status records, and tmux sockets are used.
Both synthetic turns are blocked by UserPromptSubmit. The only configured model
endpoint is a local rejecting HTTP server; the test requires zero requests.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import queue
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

BIN = Path(__file__).resolve().parents[1] / "lib"


class ConfigClient:
    """Use only initialize, hooks/list and exact isolated trust writes."""
    def __init__(self, binary, env, cwd):
        self.process = subprocess.Popen([binary, "app-server", "--stdio"], env=env, cwd=cwd,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.messages, self.sequence = queue.Queue(), 0
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        for line in self.process.stdout:
            self.messages.put(json.loads(line))

    def call(self, method, params):
        self.sequence += 1
        self.process.stdin.write(json.dumps({"id": self.sequence, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            message = self.messages.get(timeout=max(.1, deadline - time.monotonic()))
            if message.get("id") == self.sequence:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message["result"]
        raise TimeoutError(method)

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


def main():
    codex, tmux = shutil.which("codex"), shutil.which("tmux")
    if not codex or not tmux:
        raise RuntimeError("This smoke test requires installed codex and tmux executables")
    root = Path(tempfile.mkdtemp(prefix="codex-status-smoke-", dir="/tmp")).resolve()
    work = root / "work"
    work.mkdir()
    requests = []

    class RejectModel(BaseHTTPRequestHandler):
        def reject(self):
            requests.append((self.command, self.path))
            self.send_error(503)
        do_GET = do_POST = reject
        def log_message(self, *args):
            pass

    http = HTTPServer(("127.0.0.1", 0), RejectModel)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    (root / "config.toml").write_text(
        'model="probe"\nmodel_provider="probe"\n[model_providers.probe]\nname="Offline probe"\n'
        f'base_url="http://127.0.0.1:{http.server_port}"\n'
        'wire_api="responses"\nrequest_max_retries=0\nstream_max_retries=0\n[analytics]\nenabled=false\n')
    hook = root / "hook.py"
    hook.write_text(f'''import json, os, sys
from pathlib import Path
sys.path.insert(0, {str(BIN)!r})
import agent_pulse
event = json.load(sys.stdin)
expected = os.environ.get("TMUX_PANE")
os.environ["TMUX_PANE"] = "%987654"  # Deliberately wrong: ancestry must win.
socket = agent_pulse.socket_path()
processes = agent_pulse.process_snapshot()
resolved = agent_pulse.resolve_hook_pane("codex", socket, processes)
agent_pulse.handle_hook("codex", event)
record = agent_pulse.load_record(expected, agent_pulse.state_directory(socket))
result = {{"event": event.get("hook_event_name"), "session_id": event.get("session_id"),
          "expected_pane": expected, "resolved_pane": resolved, "state": record.get("state"),
          "record_session": record.get("session_id"), "owner_pid": record.get("owner_pid")}}
with Path({str(root / "events.jsonl")!r}).open("a") as stream:
    stream.write(json.dumps(result) + "\\n")
print(json.dumps({{"decision":"block", "reason":"Offline metadata probe"}})
      if event.get("hook_event_name") == "UserPromptSubmit" else "{{}}")
''')
    definition = {"type": "command", "command": shlex.join([sys.executable, str(hook)])}
    (root / "hooks.json").write_text(json.dumps({"hooks": {
        name: [{"hooks": [definition]}] for name in ("SessionStart", "UserPromptSubmit")}}))
    # No API keys, login files, inherited agent IDs, or unrelated configuration.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "USER", "LOGNAME", "LANG") if key in os.environ}
    env.update(CODEX_HOME=str(root), SHELL="/bin/sh", TERM="xterm-256color",
               AGENT_PULSE_DIR=str(root / "states"), AGENT_PULSE_NOTIFY="0")
    control, socket_path = None, root / "tmux.sock"
    try:
        control = ConfigClient(codex, env, work)
        control.call("initialize", {"clientInfo": {"name": "status-smoke", "version": "1"},
                                    "capabilities": {"experimentalApi": True}})
        control.process.stdin.write('{"method":"initialized","params":{}}\n')
        control.process.stdin.flush()
        for entry in control.call("hooks/list", {"cwds": [str(work)]})["data"]:
            for item in entry["hooks"]:
                if Path(item["sourcePath"]) != root / "hooks.json":
                    raise RuntimeError("Unexpected hook source in isolated test")
                control.call("config/value/write", {
                    "filePath": str(root / "config.toml"), "mergeStrategy": "upsert",
                    "keyPath": "hooks.state." + json.dumps(item["key"]) + ".trusted_hash",
                    "value": item["currentHash"]})
        control.close()
        control = None
        subprocess.run([tmux, "-S", str(socket_path), "-f", "/dev/null", "new-session", "-d",
                        "-s", "probe", "sleep 30"], env=env, check=True, capture_output=True)
        for label in ("one", "two"):
            command = shlex.join([codex, "exec", "--json", "--skip-git-repo-check", "-C", str(work),
                                  "Metadata-only probe; the hook must block this turn."])
            command += " > " + shlex.quote(str(root / (label + ".out")))
            command += " 2> " + shlex.quote(str(root / (label + ".err")))
            command += "; touch " + shlex.quote(str(root / (label + ".done")))
            subprocess.run([tmux, "-S", str(socket_path), "new-window", "-d", "-t", "probe", "-n", label,
                            command], env=env, check=True, capture_output=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not all((root / (name + ".done")).exists() for name in ("one", "two")):
            time.sleep(.1)
        events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
        assert not requests, f"Unexpected model requests: {requests}"
        assert len(events) == 4, events
        assert len({item["expected_pane"] for item in events}) == 2, events
        assert len({item["owner_pid"] for item in events}) == 2, events
        for event in events:
            assert event["expected_pane"] == event["resolved_pane"] != "%987654", event
            assert event["record_session"] == event["session_id"], event
            assert event["state"] == ("idle" if event["event"] == "SessionStart" else "working"), event
        print(f"PASS: two native Codex panes, ancestry routing despite wrong TMUX_PANE, zero model requests. Evidence: {root}")
    finally:
        subprocess.run([tmux, "-S", str(socket_path), "kill-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if control:
            control.close()
        http.shutdown()
        http.server_close()


if __name__ == "__main__":
    main()
