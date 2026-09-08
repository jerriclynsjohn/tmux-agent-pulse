"""TPM lifecycle, diagnostics, and commands for one tmux server."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid

import agent_pulse as status
from tmux_status_ticker import DEFAULT_COLORS, write_json

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0-dev"
ENTRY = ROOT / "bin/tmux-agent-pulse"
TICKER = ROOT / "lib/tmux_status_ticker.py"


def option(name, default=""):
    value = status.tmux(["show-options", "-gqv", "@agent-pulse-" + name])
    return value if value else default


def config_from_tmux():
    try:
        interval = float(option("interval", "0.5"))
        if not .1 <= interval <= 10:
            raise ValueError
    except ValueError:
        raise ValueError("@agent-pulse-interval must be between 0.1 and 10 seconds")
    ascii_value = option("ascii", "off")
    if ascii_value not in {"on", "off"}:
        raise ValueError("@agent-pulse-ascii must be on or off")
    colors, symbols = {}, {}
    for state, default in DEFAULT_COLORS.items():
        color = option("color-" + state, default)
        if not re.fullmatch(r"(?:#[0-9a-fA-F]{6}|[a-zA-Z][a-zA-Z0-9]*)", color):
            raise ValueError(f"Invalid AgentPulse color for {state}")
        colors[state] = color
        symbol = option("symbol-" + state)
        if symbol:
            if len(symbol) > 12 or any(ord(c) < 32 or c in "#[]" for c in symbol):
                raise ValueError(f"Invalid AgentPulse symbol for {state}")
            symbols[state] = symbol
    return {"interval": interval, "ascii": ascii_value == "on", "colors": colors,
            "symbols": symbols}


def metadata(directory):
    try:
        value = json.loads((directory / "ticker.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def lock_held(directory):
    try:
        with open(directory / ".ticker.lock", "r+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return False
            except BlockingIOError:
                return True
    except FileNotFoundError:
        return False


def live_ticker(directory):
    value = metadata(directory)
    pid = value.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 or not lock_held(directory):
        return {}
    process = status.process_snapshot().get(pid, {})
    if not value.get("start") or process.get("start") != value["start"]:
        return {}
    try:
        args = subprocess.check_output(["ps", "-p", str(pid), "-o", "args="],
                                       text=True, timeout=3)
        # ps renders paths with spaces without shell quoting on some platforms.
        expected = str(Path(value["root"]) / "lib/tmux_status_ticker.py")
        if expected not in args:
            return {}
    except (OSError, KeyError, subprocess.SubprocessError):
        return {}
    return value


@contextlib.contextmanager
def control_lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(directory / ".control.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def require_our_ticker(value, socket=None):
    if value and socket and Path(value.get("socket", "")).resolve() != Path(socket).resolve():
        raise RuntimeError("The state directory belongs to a different tmux server. "
                           "Use a separate AGENT_PULSE_DIR for each server or leave it unset.")
    if value and value.get("root") != str(ROOT):
        raise RuntimeError(f"Another AgentPulse checkout is active: {value.get('root')}. "
                           "Run unload from that checkout first.")


def binding(key):
    """Let tmux normalize key aliases without changing the prefix table.

    tmux 3.7c sends one-result list-keys queries to a status message instead
    of stdout. Two private probe tables guarantee a multi-result snapshot,
    even on a server with no other bindings. They also avoid duplicating
    tmux's version-dependent control-key normalization.
    """
    probe = "agent-pulse-inspect-" + uuid.uuid4().hex
    sentinel = probe + "-sentinel"
    try:
        snapshot = status.tmux([
            "bind-key", "-T", probe, key, "display-message", "AgentPulse key probe", ";",
            "bind-key", "-T", sentinel, key, "display-message", "AgentPulse key probe", ";",
            "list-keys"])
    finally:
        status.tmux(["unbind-key", "-a", "-T", probe, ";",
                     "unbind-key", "-a", "-T", sentinel])
    rows, canonical_key = [], None
    for line in snapshot.splitlines():
        parts = shlex.split(line)
        if not parts or parts[0] != "bind-key" or "-T" not in parts:
            continue
        index = parts.index("-T")
        table, parsed_key = parts[index + 1:index + 3]
        if table == probe:
            canonical_key = parsed_key
        elif table == "prefix":
            rows.append((parsed_key, shlex.join(parts)))
    if canonical_key is None:
        raise RuntimeError("tmux did not return a complete key snapshot; bindings were not changed")
    return next((definition for parsed_key, definition in rows if parsed_key == canonical_key), "")


def normalized_binding(value):
    try:
        return shlex.join(shlex.split(value))
    except (ValueError, TypeError):
        return ""


def owned_bindings():
    try:
        result = json.loads(option("owned-bindings", "{}"))
        return result if isinstance(result, dict) else {}
    except ValueError:
        return {}


def clear_bindings():
    owned = owned_bindings()
    remaining = dict(owned)
    for key, record in owned.items():
        if not isinstance(record, dict) or record.get("root") != str(ROOT):
            continue
        if binding(key) == normalized_binding(record.get("definition")):
            status.tmux(["unbind-key", "-T", "prefix", key])
        remaining.pop(key, None)
    if remaining:
        status.tmux(["set-option", "-g", "@agent-pulse-owned-bindings", json.dumps(remaining)])
    else:
        status.tmux(["set-option", "-gu", "@agent-pulse-owned-bindings"])


def configure_bindings():
    clear_bindings()
    owned = owned_bindings()
    for name, command in (("popup-key", "popup"), ("sidebar-key", "sidebar")):
        key = option(name)
        if not key:
            continue
        if len(key) > 80 or re.search(r"[\s;]", key) or key.startswith("-"):
            raise ValueError(f"Invalid @agent-pulse-{name}")
        if binding(key):
            print(f"AgentPulse: prefix {key} is already bound; leaving it unchanged.", file=sys.stderr)
            continue
        shell_command = shlex.join([sys.executable, str(ENTRY), "--client", "#{client_name}", command])
        status.tmux(["bind-key", "-T", "prefix", key, "run-shell", shell_command])
        definition = binding(key)
        try:
            exact_command = shlex.split(definition)[-2:] == ["run-shell", shell_command]
        except ValueError:
            exact_command = False
        if exact_command:
            owned[key] = {"root": str(ROOT), "definition": definition}
    if owned:
        status.tmux(["set-option", "-g", "@agent-pulse-owned-bindings", json.dumps(owned)])


def load():
    # Resolve even when called outside an attached tmux client.
    socket = status.tmux(["display-message", "-p", "#{socket_path}"])
    os.environ["AGENT_PULSE_SOCKET"] = socket
    directory = status.state_directory(socket)
    config = config_from_tmux()
    with control_lock(directory):
        current = live_ticker(directory)
        require_our_ticker(current, socket)
        if lock_held(directory) and not current:
            raise RuntimeError("Ticker lock is held but its owner cannot be verified. Run doctor.")
        write_json(directory / "ticker-config.json", config)
        if not current:
            env = dict(os.environ, AGENT_PULSE_SOCKET=socket, PYTHONDONTWRITEBYTECODE="1")
            log_path = directory / "ticker.log"
            if log_path.exists() and log_path.stat().st_size > 128 * 1024:
                os.replace(log_path, directory / "ticker.log.1")
            with open(log_path, "a") as log:
                child = subprocess.Popen([sys.executable, str(TICKER)], env=env,
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                         start_new_session=True, close_fds=True)
            until = time.monotonic() + 6
            while time.monotonic() < until:
                current = live_ticker(directory)
                if current:
                    break
                if child.poll() is not None:
                    raise RuntimeError(f"Ticker exited. Read {log_path}")
                time.sleep(.05)
            if not current:
                raise RuntimeError(f"Ticker startup timed out. Read {log_path}")
        status.tmux(["set-option", "-g", "@agent-pulse-plugin-path", str(ROOT)])
        configure_bindings()
        print(f"AgentPulse active: PID {current['pid']} on {socket}")
    return 0


def unload():
    socket = status.tmux(["display-message", "-p", "#{socket_path}"])
    os.environ["AGENT_PULSE_SOCKET"] = socket
    directory = status.state_directory(socket)
    with control_lock(directory):
        current = live_ticker(directory)
        require_our_ticker(current, socket)
        if lock_held(directory) and not current:
            raise RuntimeError("Ticker owner cannot be verified; refusing to signal an unknown process.")
        if current:
            os.kill(current["pid"], signal.SIGTERM)
            until = time.monotonic() + 12
            while lock_held(directory) and time.monotonic() < until:
                time.sleep(.05)
            if lock_held(directory):
                raise RuntimeError("Ticker is still stopping. Run doctor before retrying.")
        import tmux_ui
        tmux_ui.sidebar("off")
        clear_bindings()
        if option("plugin-path") == str(ROOT):
            status.tmux(["set-option", "-gu", "@agent-pulse-plugin-path"])
        print("AgentPulse unloaded. Agent hooks and status records are preserved.")
    return 0


def doctor(as_json=False):
    result = {"version": VERSION, "root": str(ROOT), "python": sys.executable,
              "python_version": sys.version.split()[0], "tmux": shutil.which("tmux"),
              "fzf": shutil.which("fzf"), "providers": {}, "warnings": []}
    for provider, env, filename in (("claude", "CLAUDE_CONFIG_DIR", "settings.json"),
                                    ("codex", "CODEX_HOME", "hooks.json")):
        home = Path(os.environ.get(env) or Path.home() / f".{provider}").expanduser()
        path = home / filename
        commands = []
        try:
            data = json.loads(path.read_text())
            for groups in data.get("hooks", {}).values():
                for group in groups:
                    for hook in group.get("hooks", []):
                        command = hook.get("command", "")
                        try:
                            parts = shlex.split(command)
                        except ValueError:
                            continue
                        if str(ENTRY) in parts and parts[-2:] == ["hook", provider]:
                            commands.append(command)
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        result["providers"][provider] = {"executable": shutil.which(provider),
            "configuration": str(path), "hook_count": len(commands),
            "trust": "Review enabled hooks in /hooks" if provider == "codex" else "Not required"}
        if not commands:
            result["warnings"].append(f"{provider}: no hooks for this checkout; run install --provider {provider}")
    try:
        socket = status.tmux(["display-message", "-p", "#{socket_path}"])
        directory = status.state_directory(socket)
        result.update(socket=socket, state_directory=str(directory), ticker=live_ticker(directory),
                      ticker_lock_held=lock_held(directory))
        result["panes"] = []
        processes = status.process_snapshot()
        snapshot = status.tmux(["list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}"])
        for line in snapshot.splitlines():
            pane, pid = line.split("\t")
            owner = status.find_owner(int(pid), processes)
            if owner:
                record = status.visible_record(status.load_record(pane, directory), owner)
                result["panes"].append({"pane": pane, **owner, "state": record["state"]})
        try:
            last = (directory / "events.jsonl").read_text().splitlines()[-1]
            result["last_hook"] = json.loads(last)
        except (OSError, IndexError, ValueError):
            result["last_hook"] = None
        if not result["ticker"]:
            result["warnings"].append("No verified ticker is running; load the plugin on this server")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result["warnings"].append(f"tmux server unavailable ({type(error).__name__})")
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"AgentPulse {VERSION}\nCheckout: {ROOT}\nPython: {result['python_version']}")
        print(f"Socket: {result.get('socket', 'unavailable')}\nTicker: {result.get('ticker') or 'not running'}")
        for provider, value in result["providers"].items():
            print(f"{provider}: {value['hook_count']} hooks in {value['configuration']}")
        for warning in result["warnings"]:
            print(f"- {warning}")
        if result.get("last_hook"):
            print(f"Latest hook: {json.dumps(result['last_hook'])}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="AgentPulse for tmux: Claude and Codex activity")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--socket", help="tmux socket path (defaults to the current server)")
    parser.add_argument("--client", help="client to focus for a popup or sidebar action")
    parser.add_argument("command", choices=("load", "unload", "doctor", "install", "uninstall",
                                            "hook", "popup", "sidebar", "focus-sidebar", "list"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    extra = args.arguments
    if args.socket:
        os.environ["AGENT_PULSE_SOCKET"] = args.socket
    if args.client:
        os.environ["AGENT_PULSE_CLIENT"] = args.client
    try:
        if args.command == "hook":
            return status.main(extra)
        if args.command in {"install", "uninstall"}:
            import install_hooks
            return install_hooks.main((["--uninstall"] if args.command == "uninstall" else []) + extra)
        if args.command == "doctor":
            doctor_parser = argparse.ArgumentParser(prog="tmux-agent-pulse doctor")
            doctor_parser.add_argument("--json", action="store_true")
            return doctor(doctor_parser.parse_args(extra).json)
        if args.command == "sidebar":
            import tmux_ui
            if len(extra) > 1 or (extra and extra[0] not in {"on", "off", "toggle"}):
                parser.error("sidebar accepts on, off, or toggle")
            return tmux_ui.sidebar(extra[0] if extra else "toggle") or 0
        if args.command == "focus-sidebar":
            return subprocess.call([sys.executable, str(ROOT / "lib/tmux_ui.py"), "focus", *extra])
        if extra:
            parser.error("unrecognized arguments: " + " ".join(extra))
        if args.command == "load":
            return load()
        if args.command == "unload":
            return unload()
        if args.command == "list":
            import tmux_agent_tree
            return tmux_agent_tree.main() or 0
        if args.command == "popup":
            if not shutil.which(os.environ.get("FZF_BIN", "fzf")):
                raise RuntimeError("Popup requires fzf. Install fzf or use the sidebar.")
            client = os.environ.get("AGENT_PULSE_CLIENT")
            env = [f"AGENT_PULSE_SOCKET={status.socket_path()}"]
            if client:
                env.append(f"AGENT_PULSE_CLIENT={client}")
            if os.environ.get("FZF_BIN"):
                env.append("FZF_BIN=" + os.environ["FZF_BIN"])
            command = shlex.join(["env", *env, str(ROOT / "lib/tmux-agent-tree.sh")])
            status.tmux(["display-popup", "-E", "-w", "80%", "-h", "70%"] +
                        (["-c", client] if client else []) + [command], timeout=None)
            return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"AgentPulse: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
