#!/usr/bin/env bash
# Optional AgentPulse UI. Keep the checkout path and arguments intact.
set -eu
agent_pulse_lib=$(cd -- "$(dirname -- "$0")" && pwd -P)
exec python3 "$agent_pulse_lib/tmux_ui.py" spawn "$@"
