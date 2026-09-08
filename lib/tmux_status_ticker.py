#!/usr/bin/env python3
"""AgentPulse: publish one consistent view of live agent panes for tmux's three renderers."""

import fcntl
import os
import json
from pathlib import Path
import tempfile
import re
import signal
import subprocess
import sys
import time

import agent_pulse as status


SEPARATOR = status.FIELD_SEPARATOR
PANE_FORMAT = SEPARATOR.join((
    "#{pane_id}", "#{window_id}", "#{pane_pid}", "#{pane_title}",
    "#{@agent-pulse-sidebar}",
))
PRIORITY = {"": 0, "unknown": 1, "idle": 2, "cancelled": 3, "working": 4, "waiting": 5}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
PROMPT = re.compile(
    r"Enter to (?:select|confirm).*Esc to cancel|"
    r"Do you want (?:to|me to) (?:proceed|allow|run|continue)", re.IGNORECASE
)
# Detached servers still need fresh metadata, but have no client to redraw.
REFRESH = ["if-shell", "-F", "#{client_name}", "refresh-client -S"]


DEFAULT_COLORS = {"waiting": "red", "working": "yellow", "cancelled": "yellow",
                  "idle": "green", "unknown": "default"}
SYMBOLS = {"waiting": "⏳", "cancelled": "✗", "idle": "✓", "unknown": "○"}
ASCII_SYMBOLS = {"waiting": "?", "working": "*", "cancelled": "x", "idle": "+", "unknown": "-"}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_config(directory):
    try:
        value = json.loads((directory / "ticker-config.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def icon_for_state(state, frame=0, config=None):
    if state not in DEFAULT_COLORS:
        return ""
    config = config or {}
    symbol = (ASCII_SYMBOLS[state] if config.get("ascii") else
              SPINNER[frame % len(SPINNER)] if state == "working" else SYMBOLS[state])
    symbol = config.get("symbols", {}).get(state, symbol)
    color = config.get("colors", {}).get(state, DEFAULT_COLORS[state])
    return f" #[fg={color}]{symbol}#[default]"


def aggregate_state(records):
    return max((r.get("state", "") for r in records if r),
               key=lambda state: PRIORITY.get(state, 0), default="")


def parse_panes(snapshot):
    panes = []
    for line in snapshot.splitlines():
        fields = line.split(SEPARATOR)
        if len(fields) != 5:
            continue
        pane_id, window_id, pane_pid, title, sidebar = fields
        try:
            pane_pid = int(pane_pid)
        except ValueError:
            continue
        panes.append({"pane_id": pane_id, "window_id": window_id,
                      "pane_pid": pane_pid, "title": title,
                      "sidebar": sidebar == "1"})
    return panes


def title_state(title):
    """Only Claude's explicit status glyphs provide recovery evidence."""
    if title and "\u2800" <= title[0] <= "\u28ff":
        return "working"
    if title.startswith("✳"):
        return "idle"
    return None


def claude_recovery_state(record, title, content, now):
    if (record.get("provider") != "claude" or
            record.get("state") != "waiting"):
        return None
    try:
        if now - float(record.get("updated_at", now)) < 2:
            return None
    except (TypeError, ValueError):
        return None
    if PROMPT.search(content):
        return None
    return title_state(title)


class Ticker:
    def __init__(self, socket_path=None):
        self.socket = socket_path if socket_path is not None else status.socket_path()
        self.directory = status.state_directory(self.socket)
        self.processes = {}
        self.process_checked_at = None
        self.recovery_checked_at = {}
        self.published = {}
        self.known_panes = set()
        self.gc_checked_at = None
        self.frame = 0
        self.config = {}
        self.config_checked_at = None

    def tmux(self, args):
        return status.tmux(args, socket_path=self.socket)

    def recover(self, pane, record, owner, now, monotonic):
        pane_id = pane["pane_id"]
        if not record or record.get("provider") != "claude":
            return record
        if record.get("state") != "waiting" or title_state(pane["title"]) is None:
            return record
        last = self.recovery_checked_at.get(pane_id)
        if last is not None and monotonic - last < 1:
            return record
        # Check the grace period before asking tmux for pane content.
        if not claude_recovery_state(record, pane["title"], "", now):
            return record
        self.recovery_checked_at[pane_id] = monotonic
        try:
            content = self.tmux(["capture-pane", "-p", "-t", pane_id, "-S", "-40"])
        except Exception:
            return record
        recovered = claude_recovery_state(record, pane["title"], content, now)
        if recovered:
            # A real hook arriving during capture wins over this inferred state.
            status.update_recovery(pane_id, record.get("updated_at"), recovered,
                                   directory=self.directory)
            return status.visible_record(
                status.load_record(pane_id, directory=self.directory), owner)
        return record

    def tick(self, now=None, monotonic=None):
        now = time.time() if now is None else now
        monotonic = time.monotonic() if monotonic is None else monotonic
        if self.config_checked_at is None or monotonic - self.config_checked_at >= 1:
            self.config = read_config(self.directory)
            self.config_checked_at = monotonic
        panes = parse_panes(self.tmux(["list-panes", "-a", "-F", PANE_FORMAT]))
        if (self.process_checked_at is None or
                monotonic - self.process_checked_at >= 1):
            self.processes = status.process_snapshot()
            self.process_checked_at = monotonic

        live_panes = {p["pane_id"] for p in panes}
        orphans = self.known_panes - live_panes
        if self.gc_checked_at is None or monotonic - self.gc_checked_at >= 10:
            orphans |= {p.stem for p in self.directory.glob("%*.json")} - live_panes
            self.gc_checked_at = monotonic
        for pane_id in orphans:
            status.delete_record(pane_id, directory=self.directory)
            self.recovery_checked_at.pop(pane_id, None)
        self.known_panes = live_panes

        windows = {}
        desired = {}
        for pane in panes:
            pane_id = pane["pane_id"]
            owner = None if pane["sidebar"] else status.find_owner(
                pane["pane_pid"], self.processes)
            record = status.visible_record(
                status.load_record(pane_id, directory=self.directory), owner)
            record = self.recover(pane, record, owner, now, monotonic)
            state = record.get("state", "") if record else ""
            provider = record.get("provider", "") if record else ""
            windows.setdefault(pane["window_id"], []).append(record)
            desired[("pane", pane_id, "@agent-pulse-state")] = state
            desired[("pane", pane_id, "@agent-pulse-provider")] = provider
            desired[("pane", pane_id, "@agent-pulse-icon")] = icon_for_state(state, self.frame, self.config)
        for window_id, records in windows.items():
            desired[("window", window_id, "@agent-pulse-window-icon")] = icon_for_state(
                aggregate_state(records), self.frame, self.config)
            desired[("window", window_id, "@agent-pulse-window-state")] = aggregate_state(records)

        commands = []
        for key, value in desired.items():
            if self.published.get(key) == value:
                continue
            scope, target, option = key
            command = (["set-option", "-p"] if scope == "pane" else
                       ["set-window-option"])
            commands.extend(command + ["-t", target, option, value, ";"])
        if commands:
            commands += REFRESH
            self.tmux(commands)
        # Update only after the whole tmux command succeeds, so failures retry.
        self.published = desired
        self.frame = (self.frame + 1) % len(SPINNER)

    def clear_options(self):
        try:
            panes = parse_panes(self.tmux(["list-panes", "-a", "-F", PANE_FORMAT]))
            commands = []
            for pane in panes:
                for option in ("@agent-pulse-state", "@agent-pulse-provider", "@agent-pulse-icon"):
                    commands += ["set-option", "-p", "-u", "-t", pane["pane_id"], option, ";"]
            for window_id in {p["window_id"] for p in panes}:
                commands += ["set-window-option", "-u", "-t", window_id, "@agent-pulse-window-icon", ";",
                             "set-window-option", "-u", "-t", window_id, "@agent-pulse-window-state", ";"]
            if commands:
                self.tmux(commands + REFRESH)
        except Exception:
            pass  # Server shutdown is a normal reason for cleanup to fail.


def main():
    ticker = Ticker()
    ticker.directory.mkdir(parents=True, exist_ok=True)
    # Keep the lock file in place: removing a flock inode can admit two writers.
    with open(ticker.directory / ".ticker.lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        lock.seek(0)
        lock.truncate()
        lock.write(f"{os.getpid()}\n")
        lock.flush()
        identity = status.process_snapshot().get(os.getpid(), {})
        metadata = {"pid": os.getpid(), "start": identity.get("start", ""),
                    "root": str(Path(__file__).resolve().parents[1]), "socket": ticker.socket}
        write_json(ticker.directory / "ticker.json", metadata)
        stopped = False

        def stop(signum, frame):
            nonlocal stopped
            stopped = True

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        failures = 0
        last_error_at = None
        try:
            while not stopped:
                started = time.monotonic()
                delay = ticker.config.get("interval", .5)
                try:
                    ticker.tick()
                except Exception as exc:
                    failures += 1
                    server_gone = False
                    # A timeout means the server may be busy, not gone. Keep the
                    # last published status until a successful tick replaces it.
                    try:
                        ticker.tmux(["list-sessions", "-F", "#{session_id}"])
                    except (subprocess.CalledProcessError, FileNotFoundError):
                        server_gone = True
                    except Exception:
                        pass
                    if (last_error_at is None or started - last_error_at >= 30
                            or server_gone):
                        action = "server unavailable; stopping" if server_gone else "retrying"
                        print(f"AgentPulse ticker ({action}): {exc}", file=sys.stderr, flush=True)
                        last_error_at = started
                    if server_gone:
                        break
                    delay = max(delay, min(5, .5 * 2 ** min(failures - 1, 4)))
                else:
                    if failures:
                        print(f"AgentPulse ticker recovered after {failures} failed updates",
                              file=sys.stderr, flush=True)
                    failures = 0
                    last_error_at = None
                    delay = ticker.config.get("interval", .5)
                # Keep TERM/INT responsive even during a long retry backoff.
                deadline = started + delay
                while not stopped:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(.2, remaining))
        finally:
            ticker.clear_options()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
