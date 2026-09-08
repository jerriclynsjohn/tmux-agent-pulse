#!/usr/bin/env python3
"""Install/remove only AgentPulse's own entries in shared tmux hook arrays."""

import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess

from agent_pulse import tmux

BIN = Path(__file__).resolve().parent
ROOT = str(BIN.parent)
HOOKS = {
    "after-new-window": ("tmux-agent-sidebar-spawn.sh", ["#{window_id}"]),
    "window-resized": ("tmux-resize-sidebars.sh", []),
}


def _entries(hook, call):
    output = call(["show-hooks", "-g", hook])
    pattern = re.compile(r"^" + re.escape(hook) + r"\[(\d+)\]\s+(.*)$")
    return {int(match[1]): match[2] for line in output.splitlines()
            if (match := pattern.match(line))}


def _owned_callback(definition, script, arguments):
    """Recognize an old standalone callback without claiming a composed hook."""
    try:
        command = shlex.split(definition)
        if len(command) != 2 or command[0] != "run-shell":
            return False
        shell = shlex.split(command[1])
        return (len(shell) == len(arguments) + 1 and shell[1:] == arguments and
                Path(shell[0]).expanduser().resolve() == script.resolve())
    except (ValueError, OSError):
        return False


def _tmux_quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def configure_sidebar_hooks(enabled, socket_path=None):
    """Preserve other plugins' indices and callbacks, including replaced slots."""
    def call(args):
        return tmux(args, socket_path=socket_path)

    installed = {}
    for hook, (filename, arguments) in HOOKS.items():
        script = BIN / filename
        option = "@agent-pulse-hook-" + hook
        try:
            owned = json.loads(call(["show-options", "-g", "-v", option]))
            if not isinstance(owned, dict):
                owned = {}
        except (ValueError, OSError, subprocess.SubprocessError):
            # An absent user option normally raises CalledProcessError.
            owned = {}
        entries = _entries(hook, call)
        # A second checkout must not unload or replace the first checkout's
        # callbacks merely because both use AgentPulse's option namespace.
        other_owner = owned.get("root") not in (None, ROOT)
        if other_owner:
            if enabled:
                raise RuntimeError("Another AgentPulse checkout owns sidebar hooks; disable it there first.")
            continue
        for index, definition in list(entries.items()):
            exact_record = (owned.get("root") == ROOT and owned.get("index") == index and
                            owned.get("definition") == definition)
            if exact_record or _owned_callback(definition, script, arguments):
                call(["set-hook", "-g", "-u", f"{hook}[{index}]"])
                del entries[index]
        call(["set-option", "-g", "-u", option])
        if not enabled:
            continue
        index = 1000
        while index in entries:
            index += 1
        command = "run-shell " + _tmux_quote(shlex.join([str(script), *arguments]))
        call(["set-hook", "-g", f"{hook}[{index}]", command])
        definition = _entries(hook, call).get(index, "")
        if not _owned_callback(definition, script, arguments):
            raise RuntimeError(f"{hook}[{index}] changed during installation; leaving it untouched")
        call(["set-option", "-g", option,
              json.dumps({"index": index, "definition": definition, "root": ROOT}, separators=(",", ":"))])
        installed[hook] = index
    return installed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("enable", "disable"))
    args = parser.parse_args()
    configure_sidebar_hooks(args.action == "enable")


if __name__ == "__main__":
    main()
