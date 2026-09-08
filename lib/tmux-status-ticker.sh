#!/usr/bin/env bash
# Shared Claude/Codex renderer; keep this entry point for tmux and existing jobs.
exec python3 "$(dirname "$0")/tmux_status_ticker.py" "$@"
