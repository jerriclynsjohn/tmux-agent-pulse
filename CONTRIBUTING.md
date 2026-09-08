# Contributing

AgentPulse is an initial preview. A tagged release and license selection remain pending.
The plugin represents foreground agent conversations through tmux status and optional views.

## Development environment

Use Python 3.10 or newer, Bash, tmux, and a C compiler.
Install `fzf` to exercise the popup.
The core needs no Python packages.

Run the suite from the repository root:

```sh
AGENT_PULSE_REAL_TMUX_TEST=1 PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
PYTHONDONTWRITEBYTECODE=1 python3 tests/tmux_status_smoke.py
```

Real tmux tests create private sockets and temporary processes.
The tests do not need the developer's existing agent conversations or provider credentials.
The optional native Codex test is described in [compatibility](docs/compatibility.md).

The TPM discovery test uses `~/.tmux/plugins/tpm` by default.
For another checkout, set `AGENT_PULSE_TPM_PATH` before the test command.
CI supplies its own TPM checkout and runs this test on both platforms.

## Change expectations

- Preserve unrelated provider hooks, tmux hooks, key bindings, and themes.
- Keep plugin loading safe to repeat and provider installation separate from loading.
- Use exact process identity before accepting status or stopping a process.
- Keep prompts, tool arguments, responses, and credentials out of diagnostic logs.
- Keep all three displays consistent with the same stored state.
- Add focused tests for behavior changes and regressions.
- Document public configuration changes and compatibility limits.

Use private tmux servers for integration experiments.
Do not test cleanup against existing user panes or unrelated ticker processes.

## Source history

The initial source comes from the owner's AgentPulse integration in the personal dotfiles repository.
The standalone extraction changes packaging, installation, defaults, and public configuration names.
It does not depend on that repository at runtime.
