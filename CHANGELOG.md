# Changelog

## Unreleased

- Extract AgentPulse into the standalone `tmux-agent-pulse` project.
- Add an executable TPM entry point and a command-line interface.
- Publish namespaced pane and window status without changing the user's theme.
- Make the popup, sidebar, and prefix bindings optional.
- Add hook installation previews, provider home overrides, backups, and ownership-aware removal.
- Add diagnostics and preserve foreign tmux hooks and key bindings.
- Add private tmux integration tests and a macOS/Linux CI matrix.
- Preserve status through transient tmux timeouts, with capped retries and responsive shutdown.
- Support tmux 3.4 field output and tmux 3.7c key lookup behavior without replacing existing bindings.
- Add the MIT license, community and security reporting policies, and release guidance.
- Require verified commits and passing CI through protected pull requests.
- Delete merged repository branches automatically while preserving unmerged work.
- Pin CI actions and the TPM test checkout, with Dependabot proposals for action updates.
- Clear stale indicators when unloading after a ticker crash, while preserving another checkout or runtime's state.
- Reject duplicate configuration aliases and recover existing alias records during hook removal.
- Validate key settings before changing bindings and retain ownership after a partial binding failure.
- Resume Claude's working status after question answers add response fields to the tool input.
- Preserve unanswered Claude approvals when the pane title shows a static star, and recognize overwrite prompts during recovery.
- Add a recorded Claude Code and Codex demo with a linked README preview.

This preview has no tagged release. Changes above remain unreleased.
