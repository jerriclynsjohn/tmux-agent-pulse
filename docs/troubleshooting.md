# Troubleshooting

`doctor` reports the current checkout, dependencies, provider hook paths, recognized pane owners, latest hook outcome, and ticker identity.
It does not change configuration or restart agents.

From the checkout, run:

```sh
./bin/tmux-agent-pulse doctor --json
```

For an explicit server, put the socket argument before the command:

```sh
./bin/tmux-agent-pulse --socket /path/to/tmux.sock doctor --json
```

## Unknown status

Unknown means the plugin found a live agent process without a matching status record.
It does not mean the agent crashed or finished.

1. Read the provider's `hook_count` and configuration path in the doctor output.
2. If hooks are absent, preview installation for that provider.
3. For Codex, review and enable the installed hook commands through `/hooks`.
4. Start a new conversation after a hook configuration change.
5. Read `events.jsonl` and `ticker.log` in the reported runtime directory.

The event log records saved, ignored, deleted, unroutable, and failed hook outcomes.
An unroutable event comes from a process chain that cannot prove ownership of a tmux agent pane.
The plugin does not use `TMUX_PANE` alone as ownership evidence.
It does not mark a quiet agent as finished to hide a missing record.

## No indicator

Loading the plugin publishes fields without changing the theme.
Window indicators appear where your configuration references `#{@agent-pulse-window-icon}`.
Pane indicators use `#{@agent-pulse-icon}`.
The field remains empty when no recognized live agent owns the pane or window.

To inspect one pane directly:

```sh
tmux display-message -p '#{@agent-pulse-provider} #{@agent-pulse-state} #{@agent-pulse-icon}'
```

If doctor reports no ticker, run the plugin's `load` command on that server.
If doctor reports another checkout's ticker, unload it from that checkout before loading this one.

## Keys or popup do not work

No keys are assigned by default.
Configured popup and sidebar keys use the tmux prefix table.
An existing foreign binding takes precedence, and plugin loading reports the conflict.

The popup requires `fzf` and a tmux client that supports popups.
The sidebar does not require `fzf`.
For a client selected outside its shell, use the command's global `--client` argument.

## Data retention

Runtime data uses `/tmp/tmux-agent-pulse-<uid>/<socket-hash>` unless `AGENT_PULSE_DIR` overrides it.
Status records contain lifecycle identifiers, state, and process identity.
`events.jsonl` retains up to 128 KiB plus one rotated file.
The diagnostic logger excludes prompts, tool arguments, responses, credentials, and exception messages.

Installation backups preserve the original provider configuration bytes.
Those backups can contain the user's existing private configuration.
They remain under the installation-record directory on the local machine.
