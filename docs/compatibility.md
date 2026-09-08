# Compatibility and test coverage

AgentPulse targets macOS and Linux with Python 3.10 or newer.
The state engine uses the Python standard library, tmux, and process information from `ps`.
Bash launches the TPM entry point and command wrappers.
The popup also needs `fzf`.

The original integration ran on macOS with tmux 3.6a and Codex CLI 0.153.4.
The standalone package includes CI jobs for macOS and Ubuntu with Python 3.10 and 3.13.
The [GitHub Actions page](https://github.com/jerriclynsjohn/tmux-agent-pulse/actions) shows their results.
Local validation passed 148 tests on macOS, including ticker timeout and compatibility regressions.
Both integration smoke checks also passed during the initial extraction.
Tagged releases require a passing macOS and Linux CI matrix.
A lower supported tmux version remains unverified.

Compatibility checks also cover Ubuntu with tmux 3.4 and an isolated macOS tmux 3.7c build.
Printable field separators preserve text on older tmux versions.
Key lookup uses tmux's own alias handling and preserves existing bindings across those versions.

## Providers

Claude Code and Codex CLI must support the event names in `hooks/claude.json` and `hooks/codex.json`.
The templates define the exact events used by this version.
Codex also requires trust for the installed command definitions.
The installer leaves Codex trust unchanged for review through `/hooks`.
The native Codex smoke test targets the interface available in Codex CLI 0.153.4.

Hooks from an existing conversation can retain the previous configuration.
A new conversation loads the installed definitions.
Agents launched outside a tmux pane cannot update another pane through inherited environment variables.

## Automated coverage

Unit tests cover state transitions, pending questions and permissions, process ownership, file replacement, and diagnostic privacy.
They also cover hook installation, ownership-aware removal, sidebar focus, and shared display state.

Private tmux tests cover repeated plugin loading, checkout paths with spaces, foreign hooks and bindings, and theme preservation.
They also cover real process discovery, namespaced state publication, and ticker cleanup.
They exercise actual TPM discovery and separate default runtime directories for two tmux servers.
The status smoke test compiles temporary sleeping programs named `claude` and `codex`.
It sends synthetic lifecycle events through the production state engine and exercises all three displays.

The optional native Codex smoke test starts two real Codex CLI processes with temporary configuration.
It blocks each turn before a model request and tests native startup/submission hooks.
It also supplies an incorrect `TMUX_PANE` value to exercise process-based pane routing.
This test does not prove native interactive permissions, interruptions, or asynchronous questions from end to end.

To run that optional test:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 tests/codex_status_smoke.py
```

The test needs an installed compatible Codex CLI, tmux, and permission to create a local HTTP listener.
The listener rejects requests, and the test requires zero model requests.

## State limits

An unknown indicator means status evidence is missing, not that the agent crashed or finished.
Process identity includes its start time to prevent a reused process ID from inheriting stale status.
Internal child agents share the foreground conversation's pane and do not appear as separate rows.
Codex automatic permission handling can briefly produce a waiting state before the tool completes.

Asynchronous questions remain pending after the question tool returns.
A new user prompt acknowledges those questions.
Hooks cannot distinguish an answer from unrelated guidance in that new prompt.

Claude alone has limited recovery from its title and visible permission prompt.
That recovery requires positive evidence and cannot overwrite a newer hook event.
Codex does not infer completion from a quiet terminal.

## Release evidence still needed

- Remote macOS and Linux CI results
- A documented minimum tmux version
- Native interactive permission, interruption, and question exercises
- Multiple attached clients and multiple server stress exercises
- Measured costs at 10, 50, and 100 panes

Native Windows and remote agent dashboards are outside the current scope.
