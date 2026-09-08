#!/usr/bin/env bash
# TPM executes root-level executable .tmux files.
set -eu
AGENT_PULSE_ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec python3 "$AGENT_PULSE_ROOT/bin/tmux-agent-pulse" load
