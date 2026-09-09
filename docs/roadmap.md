# Release work

The preview is available in the [GitHub repository](https://github.com/jerriclynsjohn/tmux-agent-pulse).
The next step is to test installation through TPM on clean macOS and Linux environments.

## Before a tagged preview release

1. Test installation from the GitHub repository through TPM on clean macOS and Linux environments.
2. Test native interactive permissions, interruptions, and asynchronous questions.
3. Record a short demonstration of mixed Claude and Codex panes.
4. Run the CI matrix for the release candidate and publish the tested versions.
5. Follow the [release policy](releasing.md) to tag a preview with known limits.

The project uses the [MIT license](../LICENSE).
The macOS and Linux [CI matrix passed](https://github.com/jerriclynsjohn/tmux-agent-pulse/actions/runs/34301781799) on commit `8ead514`.
Each release candidate must pass its own checks.

## Performance

The ticker batches changed tmux fields and caches process discovery for one second.
Each tmux server has one ticker.
Hidden sidebars poll less often than visible sidebars.

Benchmarks will measure 10, 50, and 100 panes, tool-event bursts, and many hidden sidebars.
Use the results to choose polling defaults and decide whether a shared display cache is needed.
Add a service only if the measurements show a need for one.

## Later work

- A tested minimum-version policy for tmux and both providers
- More tests with multiple attached clients and server restarts
- Cross-platform desktop notification backends
- A deterministic native fixture for permissions and asynchronous questions
- A migration command for the original personal dotfiles integration

Worktree management, agent launching, remote dashboards, and agent fleets remain outside this plugin's scope.
