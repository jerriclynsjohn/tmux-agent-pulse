# Configuration

AgentPulse publishes status fields for your tmux theme to display.
Set plugin configuration before the entry point or TPM loads.
After changes, run `bin/tmux-agent-pulse load` again.

## Status fields

| Field | Scope | Value |
| --- | --- | --- |
| `@agent-pulse-state` | Pane | `waiting`, `working`, `cancelled`, `idle`, `unknown`, or empty |
| `@agent-pulse-provider` | Pane | `claude`, `codex`, or empty |
| `@agent-pulse-icon` | Pane | The formatted pane indicator |
| `@agent-pulse-window-state` | Window | The highest-priority pane state |
| `@agent-pulse-window-icon` | Window | The formatted window indicator |
| `@agent-pulse-name` | Pane | An optional label for the sidebar and popup |

A window shows the highest-priority state among its panes: waiting, working, cancelled, idle, then unknown.
Separate pane and window fields prevent the active pane from hiding a waiting agent elsewhere in that window.
Empty state means no recognized agent owns the pane or window.
The plugin uses `@agent-pulse-` names for its public configuration and internal metadata.

For a pane border, add the icon to your existing format:

```tmux
set -g pane-border-status top
set -g pane-border-format '#P #{pane_title}#{@agent-pulse-icon}'
```

To give one pane a custom label:

```sh
tmux set-option -p @agent-pulse-name api-agent
```

Custom labels stay in tmux memory.
The plugin does not rename windows or persist labels across server restarts.

## Plugin configuration

| Name | Default | Meaning |
| --- | --- | --- |
| `@agent-pulse-interval` | `0.5` | Ticker interval in seconds, from `0.1` to `10` |
| `@agent-pulse-ascii` | `off` | Use plain ASCII symbols when `on` |
| `@agent-pulse-popup-key` | Empty | Optional key in the prefix table |
| `@agent-pulse-sidebar-key` | Empty | Optional sidebar toggle key in the prefix table |
| `@agent-pulse-sidebar-width` | `24` | Sidebar width, limited to 12–100 columns |
| `@agent-pulse-color-waiting` | `red` | Waiting indicator color |
| `@agent-pulse-color-working` | `yellow` | Working indicator color |
| `@agent-pulse-color-cancelled` | `yellow` | Cancelled indicator color |
| `@agent-pulse-color-idle` | `green` | Idle indicator color |
| `@agent-pulse-color-unknown` | `default` | Unknown indicator color |

Each state also accepts `@agent-pulse-symbol-STATE`, such as `@agent-pulse-symbol-idle`.
A custom symbol replaces the normal symbol, including the working animation.
Symbols can contain up to 12 characters, without control characters or `#`, `[` and `]`.
Colors accept tmux color names or hexadecimal RGB values, such as `#a6e3a1`.

Color and custom-symbol configuration applies to status indicators.
The popup and sidebar use their own terminal palettes and support the ASCII mode.
The sidebar reads ASCII configuration at startup.
Turn it off, then on to pick up a change.

AgentPulse keeps bindings set by you or other plugins and reports a conflict if you request the same key.
Reloading and unloading remove only bindings whose current definition still matches this checkout's definition.

## Commands

| Command | Effect |
| --- | --- |
| `load` | Start or reuse this server's ticker and apply configuration |
| `unload` | Stop this checkout's ticker and remove its owned tmux integration |
| `doctor [--json]` | Report dependencies, paths, hooks, processes, and ticker status |
| `install` | Preview or apply provider hooks |
| `uninstall` | Preview or remove unchanged recorded provider hooks |
| `popup` | Open the `fzf` picker |
| `list` | Print the popup's rows without opening it |
| `sidebar [on\|off\|toggle]` | Control the persistent sidebar |
| `focus-sidebar` | Move focus to a sidebar |
| `hook claude\|codex` | Receive a lifecycle event from standard input |

Use global arguments before the command:

```sh
./bin/tmux-agent-pulse --socket /path/to/tmux.sock doctor --json
```

`--client` selects the client for popup and focus actions.
The plugin's key bindings pass the active client automatically.

## Data and environment

The default runtime directory is `/tmp/tmux-agent-pulse-<uid>/<socket-hash>`.
Each server gets a separate directory to prevent pane-number collisions.
The installation manifest and backups use `${XDG_STATE_HOME:-~/.local/state}/tmux-agent-pulse`.

| Variable | Effect |
| --- | --- |
| `AGENT_PULSE_DIR` | Override the runtime directory for one server |
| `AGENT_PULSE_SOCKET` | Select a tmux socket explicitly |
| `AGENT_PULSE_NOTIFY` | Set `1` to enable macOS notifications |
| `CLAUDE_CONFIG_DIR` | Select the Claude configuration directory |
| `CODEX_HOME` | Select the Codex configuration directory |
| `XDG_STATE_HOME` | Select the parent directory for installation records |
| `FZF_BIN` | Select the popup's `fzf` executable |

Notifications are off by default.
The optional backend uses macOS `osascript` and `afplay`; Linux desktop notifications remain future work.
For consistent behavior, supply runtime variables to both agent processes and plugin loading.
Do not share an explicit `AGENT_PULSE_DIR` between different tmux servers.
