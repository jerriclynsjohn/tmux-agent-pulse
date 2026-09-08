#!/usr/bin/env python3
"""Build AgentPulse's fzf tree from the ticker's validated tmux pane options."""

import argparse
import os
import re
import shutil
import subprocess
import sys

from agent_pulse import FIELD_SEPARATOR, tmux

SEPARATOR = FIELD_SEPARATOR
PANE_FORMAT = SEPARATOR.join((
    "#{pane_id}", "#{session_name}", "#{window_index}", "#{window_name}",
    "#{pane_index}", "#{pane_current_path}", "#{@agent-pulse-name}",
    "#{@agent-pulse-state}", "#{@agent-pulse-provider}", "#{@agent-pulse-sidebar}",
))
RESET = "\033[0m"
DIM = "\033[2m"
STYLE = {
    "waiting": ("⏳", "\033[33m"),
    "working": ("●", "\033[36m"),
    "cancelled": ("✗", "\033[31m"),
    "idle": ("✓", "\033[32m"),
    "unknown": ("○", "\033[38;5;245m"),
}
ASCII_ICONS = {"waiting": "?", "working": "*", "cancelled": "x", "idle": "+", "unknown": "-"}


def clean_text(value):
    return "".join(c if c >= " " and c != "\x7f" else " " for c in value)


def build_lines(snapshot, ascii_mode=False):
    panes = []
    for line in snapshot.splitlines():
        fields = line.split(SEPARATOR)
        if len(fields) != 10:
            continue
        pane_id, session, winidx, winname, paneidx, cwd, user_name, state, provider, sidebar = fields
        if state not in STYLE or provider not in ("claude", "codex") or sidebar == "1":
            continue
        try:
            winidx, paneidx = int(winidx), int(paneidx)
        except ValueError:
            continue
        panes.append((session, winidx, paneidx, pane_id, winname, cwd, user_name, state, provider))
    lines = []
    last_session = None
    for session, winidx, paneidx, pane_id, winname, cwd, user_name, state, provider in sorted(panes):
        session = clean_text(session)
        if session != last_session:
            lines.append(f"{session}\t\033[1m{session}{RESET}\t")
            last_session = session
        icon, color = STYLE[state]
        if ascii_mode:
            icon = ASCII_ICONS[state]
        short_cwd = os.sep.join(cwd.rstrip(os.sep).split(os.sep)[-2:])
        label = clean_text(user_name or winname)
        display = (f"    {color}{icon}{RESET}  {DIM}#{winidx}.{paneidx}{RESET}  "
                   f"{label}  {DIM}{provider} {'-' if ascii_mode else '—'}{RESET}  {color}{clean_text(short_cwd)}{RESET}")
        lines.append(f"{session}/{winidx:03}/{paneidx:03}\t{display}\t{pane_id}")
    return lines


def pick(lines, ascii_mode=False):
    binary = shutil.which(os.environ.get("FZF_BIN", "fzf"))
    if not binary:
        raise RuntimeError("The optional popup needs fzf in PATH.")
    client = os.environ.get("AGENT_PULSE_CLIENT")
    if not client:
        clients = tmux(["list-clients", "-F", "#{client_name}"]).splitlines()
        if len(clients) != 1:
            raise RuntimeError("The picker needs an invoking tmux client; pass --client to tmux-agent-pulse.")
        client = clients[0]
    if not lines:
        print("No Claude or Codex agents are active in this tmux server.")
        return
    result = subprocess.run([
        binary, "--ansi", "--delimiter=\t", "--with-nth=2", "--layout=reverse",
        "--height=100%", "--info=inline", "--prompt=> " if ascii_mode else "--prompt=› ",
        "--pointer=>" if ascii_mode else "--pointer=▸",
        "--header=AgentPulse | Enter to jump | Esc to cancel", "--no-multi",
    ], input="\n".join(lines) + "\n", text=True, stdout=subprocess.PIPE)
    if result.returncode in (1, 130):
        return
    if result.returncode != 0:
        raise RuntimeError("fzf exited with status " + str(result.returncode))
    fields = result.stdout.rstrip("\n").split("\t")
    pane_id = fields[2] if len(fields) == 3 else ""
    if re.fullmatch(r"%[0-9]+", pane_id):
        tmux(["switch-client", "-c", client, "-t", pane_id])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pick", action="store_true")
    args = parser.parse_args(argv)
    try:
        snapshot = tmux(["list-panes", "-a", "-F", PANE_FORMAT])
        try:
            ascii_mode = tmux(["show-options", "-g", "-v", "@agent-pulse-ascii"]) in ("on", "1", "yes", "true")
        except subprocess.SubprocessError:
            ascii_mode = False
        lines = build_lines(snapshot, ascii_mode)
        if args.pick:
            pick(lines, ascii_mode)
        else:
            for line in lines:
                print(line)
    except (subprocess.SubprocessError, RuntimeError) as error:
        print("AgentPulse: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
