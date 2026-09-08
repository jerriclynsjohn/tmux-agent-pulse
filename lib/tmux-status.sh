#!/usr/bin/env bash
# Compatibility for existing Claude hooks and older copied Codex hooks.
exec python3 "$(dirname "$0")/agent_pulse.py" auto --action "${1:-}"
