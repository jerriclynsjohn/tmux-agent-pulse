#!/usr/bin/env python3
"""Optional AgentPulse panes; all mutations target this tmux server explicitly."""
import argparse
from pathlib import Path
import os
import subprocess
import sys

from agent_pulse import FIELD_SEPARATOR, tmux
from tmux_sidebar_hooks import configure_sidebar_hooks

BIN = Path(__file__).resolve().parent
ROOT = str(BIN.parent)
TAG = "@agent-pulse-sidebar"
OWNER = "@agent-pulse-sidebar-owner"
PANE_OWNER = "@agent-pulse-sidebar-root"
MODE = "@agent-pulse-sidebar-mode"
SEPARATOR = FIELD_SEPARATOR


def option(name, default=""):
    try:
        return tmux(["show-options", "-g", "-v", name]) or default
    except subprocess.SubprocessError:
        return default


def sidebar_width():
    try:
        return max(12, min(100, int(option("@agent-pulse-sidebar-width", "24"))))
    except ValueError:
        return 24


def _panes(target=None):
    args = ["list-panes", "-t", target] if target else ["list-panes", "-a"]
    output = tmux([*args, "-F", SEPARATOR.join((
        "#{pane_id}", "#{window_id}", "#{pane_active}", "#{" + TAG + "}",
        "#{" + PANE_OWNER + "}"))])
    return [parts for line in output.splitlines()
            if len(parts := line.split(SEPARATOR)) == 5]


def spawn(target):
    """Add a detached full-height pane, preserving the content pane's focus."""
    if option(MODE) != "on" or option(OWNER) != ROOT:
        return
    panes = _panes(target)
    if not panes or any(p[3] == "1" for p in panes):
        return
    prior = next((p[0] for p in panes if p[2] == "1"), panes[0][0])
    pane = tmux(["split-window", "-f", "-h", "-b", "-d", "-l", str(sidebar_width()),
                 "-P", "-F", "#{pane_id}", "-t", panes[0][0],
                 sys.executable, str(BIN / "tmux-agent-sidebar.py")])
    if pane:
        tmux(["set-option", "-p", "-t", pane, TAG, "1", ";",
              "set-option", "-p", "-t", pane, PANE_OWNER, ROOT, ";",
              "set-option", "-p", "-t", pane, "@agent-pulse-sidebar-prior", prior])


def resize():
    width = str(sidebar_width())
    for pane, _, _, tag, owner in _panes():
        if tag == "1" and owner == ROOT:
            try:
                tmux(["resize-pane", "-t", pane, "-x", width])
            except subprocess.SubprocessError:
                pass  # A pane can disappear while a window-resized hook runs.


def sidebar(action="toggle"):
    mode = option(MODE, "off")
    owner = option(OWNER)
    if action == "toggle":
        action = "off" if mode == "on" and owner == ROOT else "on"
    if action == "off":
        configure_sidebar_hooks(False)
        for pane, _, _, tag, pane_owner in _panes():
            if tag == "1" and pane_owner == ROOT:
                try:
                    tmux(["kill-pane", "-t", pane])
                except subprocess.SubprocessError:
                    pass
        if owner == ROOT:
            tmux(["set-option", "-g", "-u", MODE, ";", "set-option", "-g", "-u", OWNER])
        return
    if owner and owner != ROOT:
        raise RuntimeError("Another AgentPulse checkout owns this server's sidebar; disable it there first.")
    configure_sidebar_hooks(True)
    tmux(["set-option", "-g", OWNER, ROOT, ";", "set-option", "-g", MODE, "on"])
    windows = tmux(["list-windows", "-a", "-F", "#{window_id}"]).splitlines()
    failures = []
    for window in dict.fromkeys(windows):
        try:
            spawn(window)
        except subprocess.SubprocessError:
            failures.append(window)
    if failures:
        print("AgentPulse: sidebar could not fit in windows " + ", ".join(failures) +
              "; enlarge them and run sidebar on again.", file=sys.stderr)


def focus(target=None):
    """Focus the sidebar in the explicitly invoking client's current window."""
    if not target:
        client = os.environ.get("AGENT_PULSE_CLIENT")
        if client:
            target = tmux(["display-message", "-p", "-c", client, "#{window_id}"])
        else:
            target = os.environ.get("TMUX_PANE")
    if not target:
        raise RuntimeError("Focus needs a pane target or an invoking tmux client.")
    pane = next((p[0] for p in _panes(target) if p[3] == "1" and p[4] == ROOT), None)
    if pane:
        tmux(["select-pane", "-t", pane])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    side = sub.add_parser("sidebar")
    side.add_argument("action", choices=("toggle", "on", "off"), nargs="?", default="toggle")
    spawner = sub.add_parser("spawn")
    spawner.add_argument("target")
    sub.add_parser("resize")
    focuser = sub.add_parser("focus")
    focuser.add_argument("target", nargs="?")
    args = parser.parse_args(argv)
    try:
        if args.command == "sidebar":
            sidebar(args.action)
        elif args.command == "spawn":
            spawn(args.target)
        elif args.command == "resize":
            resize()
        elif args.command == "focus":
            focus(args.target)
    except (RuntimeError, subprocess.SubprocessError) as error:
        print("AgentPulse: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
