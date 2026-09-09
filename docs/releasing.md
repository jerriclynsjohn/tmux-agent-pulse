# Releasing AgentPulse

No release has been tagged yet. Use this process when preparing the first preview.

## Version policy

Use [Semantic Versioning](https://semver.org/spec/v2.0.0.html) with `v`-prefixed Git tags.
The preview series uses `0.y.z`; a release candidate can use a suffix such as `0.1.0-rc.1`.
During the preview, increment the minor version for new features or incompatible changes and the patch version for compatible fixes.
Describe incompatible changes and migration steps in the release notes.

The public interface covers the documented CLI commands, tmux options and status fields, and provider installation behavior.
Internal Python modules and runtime files are not a stable integration interface.
Version `1.0.0` will mark the first stable public interface.
After that, incompatible changes increment the major version.

Keep published tags and release contents unchanged.
Publish a new version to correct code in a release.
The default TPM declaration follows the repository's default branch; installing it does not pin a release.

## Prepare the release

1. Prepare release changes on a topic branch and submit a pull request.
   Move the relevant entries from `Unreleased` in [CHANGELOG.md](../CHANGELOG.md) into a dated version entry.
   Keep an `Unreleased` section for later work.
2. Update [compatibility](compatibility.md) with the exact tested tmux, Python, operating system, and provider versions.
   Record remaining limits and update [SECURITY.md](../SECURITY.md) with the supported release policy.
3. Run the local checks in [CONTRIBUTING.md](../CONTRIBUTING.md).
   Test a clean TPM installation from GitHub in a private tmux server on macOS and Linux.
   Check loading, hook installation, status displays, updating, unloading, and removal.
4. Test native Claude Code and Codex conversations for startup, work, completion, permission requests, interruptions, and questions.
   Record each tested provider version and result.
   Fix failures or narrow the release's documented support before tagging.
5. Include upgrade and rollback instructions in the release notes.
   State whether users need to install hooks again or start new conversations.
   Test the documented rollback to the previous release, or explain why a reinstall is required.
   For the first release, verify removal and describe how to return to a setup without AgentPulse.
6. Merge through the normal pull request process, with GitHub-verified signatures on every proposed commit and the resulting squash commit.
   Do not bypass the pull request, signature, or required-check rules to prepare a release.
   Confirm that `Verified commits` and the required tests passed on the pull request head.
   Record the full commit SHA on `main` selected for release.
   Confirm that the macOS and Linux CI matrix passes for that exact SHA.
   The `Verified commits` check runs on pull requests, not the resulting squash commit.
   Compare the release tree with the locally tested tree.
   If their content differs, repeat the affected local checks and installation trials.

## Tag and publish

Use a clean checkout of the reviewed `main` commit.
Fetch from the repository and confirm the commit matches the one recorded with the test results.
Use `git rev-parse HEAD` to check the full SHA, and confirm that commit is on `origin/main`.
Create a signed annotated tag with `git tag -s`, then verify it with `git tag -v`.
See [GitHub's tag-signing instructions](https://docs.github.com/en/authentication/managing-commit-signature-verification/signing-tags).

For example, after selecting `0.1.0-rc.1` as the release version:

```sh
git tag -s v0.1.0-rc.1 -m "AgentPulse 0.1.0-rc.1"
git tag -v v0.1.0-rc.1
git push origin refs/tags/v0.1.0-rc.1
```

Check that GitHub shows the tag signature as verified.
Draft a GitHub release using that existing tag.
Include the changelog, tested versions, known limits, and upgrade and rollback instructions.
Mark preview releases as **pre-release**.
Review the draft before publishing it.

## Update and rollback notes

Follow the [checkout update procedure](installation.md#move-or-update-the-checkout).
Keep the checkout path and installation records consistent so provider hooks still reach the intended code.
Preview hook changes before applying them and restart the ticker after changing the checkout version.
If the release changes Codex hook commands, review their trust in `/hooks` and start new conversations.

For a rollback, unload the current plugin before switching the checkout to a previous version.
Follow that release's tested instructions for hooks and installation records before loading it again.
A release that changes those formats must document any required removal and reinstall steps.
Do not replace a user's entire provider configuration with an old backup.
