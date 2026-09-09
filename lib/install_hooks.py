#!/usr/bin/env python3
"""Preview, install, or remove the exact provider hooks owned by AgentPulse."""
import argparse
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
PROVIDERS = {"claude": "settings.json", "codex": "hooks.json"}
TOKEN = "@AGENT_PULSE_COMMAND@"


def encode(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def absolute(path):
    """Keep the selected symlink path for the manifest, resolving only at writes."""
    return Path(os.path.abspath(Path(path).expanduser()))


def hook_command(provider):
    return shlex.join([sys.executable, str(ROOT / "bin/tmux-agent-pulse"), "hook", provider])


def definitions(provider):
    template = json.loads((ROOT / "hooks" / (provider + ".json")).read_text())
    result = []
    for event, groups in template["hooks"].items():
        for group in groups:
            metadata = {key: value for key, value in group.items() if key != "hooks"}
            for hook in group["hooks"]:
                hook = deepcopy(hook)
                if hook.get("command") != TOKEN:
                    raise ValueError("Hook template has an unexpected command")
                hook["command"] = hook_command(provider)
                result.append({"event": event, "group": metadata, "hook": hook})
    return result


def hook_groups(config, event):
    hooks = config.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("The configuration's hooks field must be an object")
    groups = hooks.get(event, [])
    if not isinstance(groups, list):
        raise ValueError(f"The hooks for {event} must be a list")
    return groups


def matches(group, definition):
    return (isinstance(group, dict)
            and {key: value for key, value in group.items() if key != "hooks"} == definition["group"]
            and isinstance(group.get("hooks"), list))


def contains(config, definition):
    return any(matches(group, definition) and definition["hook"] in group["hooks"]
               for group in hook_groups(config, definition["event"]))


def command_present(config, definition):
    command = definition["hook"]["command"]
    for group in hook_groups(config, definition["event"]):
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            if any(isinstance(hook, dict) and hook.get("command") == command for hook in group["hooks"]):
                return True
    return False


def remove_definition(config, definition, created_events):
    """Remove one exact owned definition; keep changed definitions and siblings."""
    groups = hook_groups(config, definition["event"])
    for index, group in enumerate(groups):
        if matches(group, definition) and definition["hook"] in group["hooks"]:
            group["hooks"].remove(definition["hook"])
            if not group["hooks"]:
                groups.pop(index)
            if not groups and definition["event"] in created_events:
                config["hooks"].pop(definition["event"], None)
            return True
    return False


def merge_install(existing, provider, previous=None):
    updated = deepcopy(existing)
    desired = definitions(provider)
    previous = deepcopy(previous or {})
    created_events = previous.get("created_events", [])
    for definition in previous.get("definitions", []):
        if command_present(updated, definition) and not contains(updated, definition):
            raise ValueError(f"{provider} {definition['event']}: a managed hook was edited; preserve or remove it manually before installing")
        if definition not in desired:
            remove_definition(updated, definition, created_events)
    for definition in desired:
        if contains(updated, definition):
            continue
        if command_present(updated, definition):
            raise ValueError(f"{provider} {definition['event']}: this AgentPulse command already has a different hook definition")
        hook_groups(updated, definition["event"])  # Validate before changing anything.
        hooks = updated.setdefault("hooks", {})
        if definition["event"] not in hooks and definition["event"] not in created_events:
            created_events.append(definition["event"])
        hooks.setdefault(definition["event"], []).append({**deepcopy(definition["group"]), "hooks": [deepcopy(definition["hook"])]})
    metadata = {"definitions": desired, "created_events": created_events,
                "created_hooks": previous.get("created_hooks", "hooks" not in existing)}
    return updated, metadata


def merge_uninstall(existing, entry):
    updated, warnings = deepcopy(existing), []
    for definition in entry["definitions"]:
        if not remove_definition(updated, definition, entry.get("created_events", [])) and command_present(updated, definition):
            warnings.append(f"Kept edited {entry['provider']} {definition['event']} hook in {entry['path']}")
    if updated.get("hooks") == {} and entry.get("created_hooks"):
        updated.pop("hooks")
    return updated, warnings


@dataclass
class Snapshot:
    path: Path
    target: Path
    data: object
    mode: int

    @classmethod
    def read(cls, path):
        path = absolute(path)
        target = path.resolve(strict=False)
        try:
            data, mode = target.read_bytes(), stat.S_IMODE(target.stat().st_mode)
        except FileNotFoundError:
            data, mode = None, 0o600
        return cls(path, target, data, mode)

    def json(self, default):
        if self.data is None:
            return deepcopy(default)
        value = json.loads(self.data)
        if not isinstance(value, dict):
            raise ValueError(f"Expected a JSON object in {self.path}")
        return value

    def unchanged(self):
        current = Snapshot.read(self.path)
        return current.target == self.target and current.data == self.data


def atomic_write(snapshot, data):
    """Replace the resolved target, retaining a config-file symlink and its mode."""
    target = snapshot.target
    if data is None:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="." + target.name + ".", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), snapshot.mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def manifest_entries(manifest):
    if manifest.get("version") != 1 or not isinstance(manifest.get("installations"), list):
        raise ValueError("Unsupported AgentPulse install manifest; no configuration was changed")
    entries = manifest["installations"]
    seen = set()
    for entry in entries:
        if (not isinstance(entry, dict) or entry.get("provider") not in PROVIDERS
                or not isinstance(entry.get("path"), str) or not Path(entry["path"]).is_absolute()
                or not isinstance(entry.get("definitions"), list)):
            raise ValueError("Invalid AgentPulse install manifest")
        key = (entry["provider"], entry["path"])
        if key in seen:
            raise ValueError("Duplicate configuration in AgentPulse install manifest")
        seen.add(key)
        for definition in entry["definitions"]:
            if (not isinstance(definition, dict) or not isinstance(definition.get("event"), str)
                    or not isinstance(definition.get("group"), dict) or not isinstance(definition.get("hook"), dict)
                    or not isinstance(definition["hook"].get("command"), str)):
                raise ValueError("Invalid hook in AgentPulse install manifest")
    return deepcopy(entries)


def config_paths(args):
    result = {}
    for provider, filename in PROVIDERS.items():
        override = getattr(args, provider + "_home")
        variable = "CLAUDE_CONFIG_DIR" if provider == "claude" else "CODEX_HOME"
        home = override or os.environ.get(variable) or Path.home() / ("." + provider)
        result[provider] = absolute(home) / filename
    return result


def plan(args, manifest_path):
    manifest_snapshot = Snapshot.read(manifest_path)
    original = manifest_snapshot.json({"version": 1, "installations": []})
    entries = manifest_entries(original)
    selected = set(PROVIDERS) if args.provider == "both" else {args.provider}
    paths = config_paths(args)
    changes, warnings = [], []
    if args.uninstall:
        retained = []
        configurations = {}
        selected_targets = {
            (entry["provider"], Path(entry["path"]).resolve()) for entry in entries
            if entry["provider"] in selected
            and (not getattr(args, entry["provider"] + "_home")
                 or str(paths[entry["provider"]]) == entry["path"])
        }
        for entry in entries:
            provider = entry["provider"]
            if (provider, Path(entry["path"]).resolve()) not in selected_targets:
                retained.append(entry)
                continue
            snapshot = Snapshot.read(entry["path"])
            if snapshot.data is None:
                continue
            # Older manifests can name the same file through several symlinks.
            # Apply their removals together so the target is written once.
            _, aliases = configurations.setdefault(snapshot.target, (snapshot, []))
            aliases.append(entry)
        for snapshot, aliases in configurations.values():
            existing = snapshot.json({})
            updated = existing
            created_events = list(dict.fromkeys(event for entry in aliases
                                                for event in entry.get("created_events", [])))
            created_hooks = any(entry.get("created_hooks") for entry in aliases)
            for entry in aliases:
                updated, messages = merge_uninstall(updated, {
                    **entry, "created_events": created_events, "created_hooks": created_hooks})
                warnings.extend(messages)
            remove_file = not updated and any(entry.get("created_file") for entry in aliases)
            if updated != existing or remove_file:
                content = None if remove_file else encode(updated)
                changes.append((snapshot, content))
        entries = retained
    else:
        for provider in PROVIDERS:
            if provider not in selected:
                continue
            path = paths[provider]
            previous = next((entry for entry in entries if entry["provider"] == provider and entry["path"] == str(path)), None)
            snapshot = Snapshot.read(path)
            # An unchanged config still needs this check: otherwise a second
            # symlink alias is recorded without appearing in ``changes``.
            if snapshot.target == manifest_snapshot.target:
                raise ValueError("Configuration paths share a target with the install manifest")
            for recorded in entries:
                if recorded is not previous and Path(recorded["path"]).resolve() == snapshot.target:
                    raise ValueError("Configuration paths share a target; use the recorded path "
                                     f"{recorded['path']} or uninstall it first")
            existing = snapshot.json({})
            updated, metadata = merge_install(existing, provider, previous)
            entry = {"provider": provider, "path": str(path), **metadata,
                     "created_file": previous.get("created_file", snapshot.data is None) if previous else snapshot.data is None}
            if previous:
                entries[entries.index(previous)] = entry
            else:
                entries.append(entry)
            if updated != existing:
                changes.append((snapshot, encode(updated)))
    current = {"version": 1, "installations": entries}
    if current != original:
        changes.append((manifest_snapshot, encode(current)))
    # A selected config must not alias another selected config or the manifest.
    targets = [snapshot.target for snapshot, _ in changes]
    if len(targets) != len(set(targets)):
        raise ValueError("Configuration paths share a target; use distinct provider config files")
    return changes, warnings


@contextmanager
def install_lock(state_dir):
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_dir / "install.lock").open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield


def apply_changes(changes, state_dir):
    if not changes:
        return None
    if any(not snapshot.unchanged() for snapshot, _ in changes):
        raise RuntimeError("A configuration changed during preview; run the command again")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = state_dir / "backups" / stamp
    backup.mkdir(parents=True, mode=0o700)
    index = []
    for snapshot, _ in changes:
        name = hashlib.sha256(str(snapshot.path).encode()).hexdigest()[:16] + "-" + snapshot.path.name
        index.append({"path": str(snapshot.path), "target": str(snapshot.target), "backup": name if snapshot.data is not None else None})
        if snapshot.data is not None:
            saved = Snapshot.read(backup / name)
            atomic_write(saved, snapshot.data)
    atomic_write(Snapshot.read(backup / "index.json"), encode(index))
    written = []
    try:
        for snapshot, content in changes:
            if not snapshot.unchanged():
                raise RuntimeError(f"Configuration changed during installation: {snapshot.path}")
            atomic_write(snapshot, content)
            written.append((snapshot, content))
    except Exception:
        for snapshot, content in reversed(written):
            current = Snapshot.read(snapshot.path)
            if current.target == snapshot.target and current.data == content:
                atomic_write(snapshot, snapshot.data)
        raise
    return backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: preview only)")
    parser.add_argument("--provider", choices=["claude", "codex", "both"], default="both")
    parser.add_argument("--claude-home", type=Path, help="Claude config directory (default: CLAUDE_CONFIG_DIR or ~/.claude)")
    parser.add_argument("--codex-home", type=Path, help="Codex config directory (default: CODEX_HOME or ~/.codex)")
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "tmux-agent-pulse",
                        help="manifest and backups directory")
    parser.add_argument("--uninstall", action="store_true", help="remove recorded, unchanged hooks only")
    args = parser.parse_args(argv)
    args.state_dir = absolute(args.state_dir)
    manifest_path = args.state_dir / "install-manifest.json"

    def run():
        changes, warnings = plan(args, manifest_path)
        for message in warnings:
            print(message)
        for snapshot, content in changes:
            action = "Remove" if content is None else "Update"
            print(f"{action if args.apply else 'Would ' + action.lower()} {snapshot.path}")
        if not changes:
            print("No changes needed.")
        if args.apply:
            backup = apply_changes(changes, args.state_dir)
            if backup:
                print(f"Backups: {backup}")
        elif changes:
            print("Preview only. Repeat with --apply to save these changes.")
        if not args.uninstall:
            for provider in PROVIDERS:
                if args.provider in {provider, "both"}:
                    print(f"{provider} command: {hook_command(provider)}")
            if args.provider in {"codex", "both"}:
                print("Codex: restart or open /hooks, review the AgentPulse commands, and approve them. Changed commands may need approval again. This installer does not change hook trust.")
        return 0

    try:
        if args.apply:
            with install_lock(args.state_dir):
                return run()
        return run()
    except (OSError, ValueError, RuntimeError) as error:
        print(f"AgentPulse: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
