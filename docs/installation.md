# Installation and removal

TPM loads the tmux display; the separate `install` command adds provider hooks.
Those hooks call the code in this checkout, so both providers use the same runtime.

## Install with TPM

Add this declaration before your existing TPM initialization:

```tmux
set -g @plugin 'jerriclynsjohn/tmux-agent-pulse'
```

Reload your tmux configuration.
Press `prefix + I` to install and load the plugin.

If you use TPM's default directory, open the checkout:

```sh
cd ~/.tmux/plugins/tmux-agent-pulse
```

For a custom TPM directory, use its `tmux-agent-pulse` checkout instead.
Then install the provider hooks with the commands below.

## Install hooks

From the checkout, choose the provider to preview:

```sh
./bin/tmux-agent-pulse install --provider claude
./bin/tmux-agent-pulse install --provider codex
```

To install both providers, use `--provider both` or omit that argument.
To apply the displayed changes, add `--apply`:

```sh
./bin/tmux-agent-pulse install --provider both --apply
```

For Codex, review the exact installed commands in `/hooks` and enable their trust.
The installer does not edit Codex trust.
Start new agent conversations after installation so they load the new hook definitions.

The installer preserves unrelated hook definitions and JSON fields.
Before a write, it saves the original bytes and replaces the target file atomically.
An existing configuration symlink stays in place.
The manifest records the installed definitions and their configuration paths.

## Custom directories

Explicit arguments take precedence over environment variables, followed by the default provider directories:

```sh
./bin/tmux-agent-pulse install --provider both \
  --claude-home '/path/to/Claude configuration' \
  --codex-home '/path/to/Codex configuration' \
  --state-dir '/path/to/AgentPulse installation records'
```

`CLAUDE_CONFIG_DIR` and `CODEX_HOME` select provider directories when the corresponding arguments are absent.
Defaults are `~/.claude` and `~/.codex`.
The default installation-record directory is `${XDG_STATE_HOME:-~/.local/state}/tmux-agent-pulse`.
Use the same `--state-dir` for later installation or removal commands.

The record directory contains `install-manifest.json` and `backups/<timestamp>/`.
Each backup contains an `index.json` that maps original paths to saved files.
The installer preview prints exact hook commands, including the resolved checkout and Python interpreter paths.

## Move or update the checkout

After a code update, run `bin/tmux-agent-pulse unload`, then `bin/tmux-agent-pulse load`.
This restarts the ticker without restarting agent conversations.
If you use the sidebar, enable it again after loading.
Provider hooks call the same checkout path and use its updated code.
If a hook template changed, rerun installation to preview the new definitions.

Before moving an active checkout, run its `unload` command.
After moving it, run installation from the new path with the same installation-record directory.
The installer uses the manifest to replace its unchanged previous definitions.
Review changed Codex commands again in `/hooks`, then start new agent conversations.

The plugin will not replace another checkout's ticker or signal one whose process identity it cannot establish.
The original personal dotfiles integration has a separate runtime directory and remains independent.
Its hook commands are not migrated by this installer.

## Remove the plugin

1. Preview removal before deleting the checkout:

   ```sh
   ./bin/tmux-agent-pulse uninstall --provider both
   ```

2. Apply the displayed removal:

   ```sh
   ./bin/tmux-agent-pulse uninstall --provider both --apply
   ```

3. Unload the tmux integration:

   ```sh
   ./bin/tmux-agent-pulse unload
   ```

4. Remove the plugin declaration or direct entry point from your tmux configuration.
5. For a TPM installation, use TPM's uninstall command after removing the declaration.

Uninstall examines the manifest's recorded configuration paths for the selected providers.
Explicit provider-home arguments restrict removal to those paths.
It removes only definitions that still match the installation record, reports any edited definitions, and leaves them in place.
Unrelated hooks, configuration fields, backups, and status records stay in place too.

Unload removes owned sidebar panes and callbacks, along with unchanged owned bindings and published status fields.
It leaves agent conversations running.
[TPM removal](https://github.com/tmux-plugins/tpm#uninstalling-plugins) deletes the checkout but leaves provider hooks in place, so run `uninstall` first.
