#!/usr/bin/env bash
# Optional interactive popup; fzf supplies the picker and terminal theme.
set -eu
agent_pulse_lib=$(cd -- "$(dirname -- "$0")" && pwd -P)
exec python3 "$agent_pulse_lib/tmux_agent_tree.py" --pick "$@"
