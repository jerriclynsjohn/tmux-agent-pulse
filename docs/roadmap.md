# Release work

The standalone preview provides a TPM entry point, namespaced status, optional views, hook installation, removal, and diagnostics.
The [GitHub repository](https://github.com/jerriclynsjohn/tmux-agent-pulse) hosts this initial preview.
The next release gate is an installation trial on a clean macOS and Linux environment.

## Before a tagged preview release

1. Choose a license and review source ownership before a public release.
2. Complete an installation trial from the GitHub repository through TPM.
3. Run the remote CI matrix and publish the tested versions.
4. Exercise native interactive permissions, interruptions, and asynchronous questions.
5. Record a short demonstration of mixed Claude and Codex panes.
6. Tag a preview release with known limits.

## Performance

The ticker batches changed tmux fields and caches process discovery for one second.
Each tmux server has one ticker.
Hidden sidebars poll less often than visible sidebars.

Benchmarks will measure 10, 50, and 100 panes, tool-event bursts, and many hidden sidebars.
The results will guide polling defaults and any shared display cache.
Additional services remain unnecessary until measurements show a need.

## Later work

- A tested minimum-version policy for tmux and both providers
- More complete multi-client and server-restart exercises
- Cross-platform desktop notification backends
- A deterministic native fixture for permissions and asynchronous questions
- A migration command for the original personal dotfiles integration

Worktree management, agent launching, remote dashboards, and agent fleets remain outside this plugin's scope.
