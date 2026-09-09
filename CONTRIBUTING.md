# Contributing

AgentPulse is an initial preview. A tagged release and license selection remain pending.
The plugin represents foreground agent conversations through tmux status and optional views.

## Issues and pull requests

Use the bug report or feature request form in GitHub Issues.
For a larger feature, describe the problem in an issue before you start implementation.
For bugs, include versions, a minimal tmux configuration, reproduction steps, and the expected and actual results.
Remove secrets, private paths, and conversation text from shared logs and screenshots.

Submit changes through a pull request to `main` from a branch or fork.
Keep each pull request focused on one problem.
Describe the resulting behavior.
If a related issue exists, link it in the pull request.
Complete local validation before you open a pull request for a code change.
All required CI checks must also pass before merge.

Every commit must carry a signature that GitHub verifies.
After you configure a signing key, create commits with `git commit -S`.
Make sure that GitHub shows **Verified** for each commit in your pull request.
See [GitHub's signing instructions](https://docs.github.com/en/authentication/managing-commit-signature-verification/signing-commits).

## Development environment

Use Python 3.10 or newer, Bash, tmux, Git, a C compiler, and a TPM checkout.
Install `fzf` to exercise the popup.
The core needs no Python packages.

The TPM discovery test uses `~/.tmux/plugins/tpm` by default.
For another checkout, set `AGENT_PULSE_TPM_PATH` before the test command.
CI supplies its own TPM checkout and runs this test on macOS and Linux.

## Local validation

For code changes, run these commands from the repository root before you open a pull request:

```sh
AGENT_PULSE_REAL_TMUX_TEST=1 PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
PYTHONDONTWRITEBYTECODE=1 python3 tests/tmux_status_smoke.py
bash -n agent-pulse.tmux
for script in lib/*.sh; do bash -n "$script"; done
git diff --check
```

Exercise the changed behavior locally, including the reproduction steps for a bug fix.
For display or key-binding changes, include the observed result from a private tmux session.
Record the commands, results, and tested versions in the pull request.
If a required check cannot run, resolve the blocker before you open the pull request.

Real tmux tests create private sockets and temporary processes.
The tests do not need the developer's existing agent conversations or provider credentials.
For native Codex hook changes, also run the native smoke test with a compatible installed Codex CLI:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 tests/codex_status_smoke.py
```

[Compatibility](docs/compatibility.md) describes its requirements and coverage limits.
For documentation-only changes, review the rendered text, links, and command examples.
Run `git diff --check`.
State which runtime checks do not apply and why.

## AI-assisted contributions

The same requirements apply to changes made with AI tools.
As the contributor, you own the correctness and maintenance of the submitted change.
Read and understand the diff, run the applicable tests locally, and explain the behavior to reviewers.
Report results from commands you actually ran.

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
