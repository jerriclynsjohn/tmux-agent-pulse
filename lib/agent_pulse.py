#!/usr/bin/env python3
"""AgentPulse: shared Claude/Codex state without prompts or tool arguments."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time

STATES = {"idle", "working", "waiting", "cancelled"}
QUESTIONS = {"AskUserQuestion", "ExitPlanMode", "request_user_input", "request_user_input_async"}
AUDIT_MAX_BYTES = 128 * 1024
# tmux 3.4 prints control separators such as U+001F as literal "\\037",
# indistinguishable from a user's backslash text. A printable random marker
# preserves those values without depending on tmux's output escaping version.
FIELD_SEPARATOR = "__agent_pulse_" + secrets.token_hex(16) + "__"


def setting(name, default=None):
    """Prefer AgentPulse settings while accepting the previous environment names."""
    return (os.environ.get(f"AGENT_PULSE_{name}") or
            os.environ.get(f"AGENT_STATUS_{name}") or default)


def socket_path():
    return setting("SOCKET") or os.environ.get("TMUX", "").rsplit(",", 2)[0]


def state_directory(socket_path=None):
    if setting("DIR"):
        return Path(setting("DIR"))
    socket = socket_path if socket_path is not None else globals()["socket_path"]()
    key = hashlib.sha256(str(Path(socket).resolve()).encode()).hexdigest()[:16]
    return Path("/tmp") / f"tmux-agent-pulse-{os.getuid()}" / key


def tmux(args, socket_path=None, timeout=2):
    socket = socket_path if socket_path is not None else globals()["socket_path"]()
    command = ["tmux"] + (["-S", socket] if socket else []) + list(args)
    return subprocess.check_output(command, stderr=subprocess.DEVNULL,
                                   timeout=timeout, text=True).rstrip("\n")


def process_snapshot():
    """Start times distinguish a reused PID from the previous agent."""
    output = subprocess.check_output(["ps", "-axo", "pid=,ppid=,lstart=,comm="], text=True, timeout=3)
    result = {}
    for line in output.splitlines():
        parts = line.split(None, 7)
        if len(parts) == 8:
            try:
                result[int(parts[0])] = {"ppid": int(parts[1]), "start": " ".join(parts[2:7]), "command": parts[7]}
            except ValueError:
                pass
    return result


def provider_for_command(command):
    name = Path(command).name
    if name in {"claude", "claude-code"} or re.fullmatch(r"\d+\.\d+\.\d+(?:-[\w.]+)?", name):
        return "claude"
    if name in {"codex", "codex-cli"}:
        return "codex"
    return None


def find_owner(pane_pid, processes):
    """Find the outermost agent under the pane, including npm's Node wrapper."""
    children = {}
    for pid, process in processes.items():
        children.setdefault(process["ppid"], []).append(pid)
    queue, visited = [int(pane_pid)], set()
    while queue:
        pid = queue.pop(0)
        if pid in visited:
            continue
        visited.add(pid)
        process = processes.get(pid, {})
        provider = provider_for_command(process.get("command", ""))
        if provider:
            return {"provider": provider, "pid": pid, "start": process.get("start", "")}
        queue.extend(sorted(children.get(pid, [])))
    return None


def resolve_hook_pane(provider, socket, processes, hook_pid=None):
    """Only a hook descended from a pane's agent can update that pane.

    A desktop/shared-daemon process can inherit stale TMUX_PANE values. Its
    ancestry does not reach a pane, so it cannot overwrite a terminal agent.
    """
    pid, ancestors = hook_pid or os.getpid(), set()
    while pid and pid not in ancestors:
        ancestors.add(pid)
        pid = processes.get(pid, {}).get("ppid", 0)
    candidates = set()
    snapshot = tmux(["list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}"], socket)
    for line in snapshot.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or not fields[1].isdigit():
            continue
        pane, pane_pid = fields[0], int(fields[1])
        if pane_pid not in ancestors:
            continue
        owner = find_owner(pane_pid, processes)
        if owner and (provider == "auto" or owner["provider"] == provider) and owner["pid"] in ancestors:
            candidates.add(pane)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _file(pane_id, directory=None):
    if not re.fullmatch(r"%\d+", pane_id):
        raise ValueError("Invalid pane id")
    return Path(directory or state_directory()) / f"{pane_id}.json"


@contextlib.contextmanager
def record_lock(pane_id, directory=None):
    path = _file(pane_id, directory)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path.parent / f".{pane_id}.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def load_record(pane_id, directory=None):
    try:
        record = json.loads(_file(pane_id, directory).read_text())
        return record if isinstance(record, dict) else {}
    except (OSError, ValueError):
        return {}


def save_record(pane_id, record, directory=None):
    path = _file(pane_id, directory)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{pane_id}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def delete_record(pane_id, directory=None):
    with contextlib.suppress(FileNotFoundError):
        _file(pane_id, directory).unlink()


def audit_event(provider, event, outcome, *, directory, pane=None, record=None,
                owner=None, error=None, now=None):
    """Best-effort bounded metadata; never copy payloads or exception messages."""
    event = event if isinstance(event, dict) else {}
    record, owner = record or {}, owner or {}
    entry = {"time": time.time() if now is None else now}
    fields = {"provider": provider, "pane": pane, "event": event.get("hook_event_name"),
              "outcome": outcome, "state": record.get("state"),
              "session_id": event.get("session_id"), "turn_id": event.get("turn_id"),
              "error": type(error).__name__ if error is not None else None}
    for key, value in fields.items():
        if isinstance(value, str) and re.fullmatch(r"[\w.%:/-]{1,160}", value, re.ASCII):
            entry[key] = value
    pid = owner.get("pid", record.get("owner_pid"))
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        entry["owner_pid"] = pid
    line = (json.dumps(entry, separators=(",", ":")) + "\n").encode()
    path = Path(directory) / "events.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path.parent / ".events.lock", os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.exists() and path.stat().st_size + len(line) > AUDIT_MAX_BYTES:
                os.replace(path, path.with_name("events.jsonl.1"))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "ab") as stream:
                stream.write(line)
    except OSError:
        # Audit storage failures must not change the agent's lifecycle handling.
        pass


def visible_record(record, owner):
    if not owner:
        return None
    if (record.get("provider") == owner["provider"] and record.get("owner_pid") == owner["pid"]
            and record.get("owner_start") == owner["start"] and record.get("state") in STATES):
        return record
    return {"state": "unknown", "provider": owner["provider"],
            "owner_pid": owner["pid"], "owner_start": owner["start"]}


def update_recovery(pane_id, expected_updated_at, state, directory=None):
    with record_lock(pane_id, directory):
        record = load_record(pane_id, directory)
        if (record.get("updated_at") != expected_updated_at or record.get("provider") != "claude"
                or record.get("state") != "waiting"):
            return False
        record.update(state=state, activity=state, updated_at=time.time(),
                      pending_questions={}, pending_permissions={})
        save_record(pane_id, record, directory)
        return True


def _tool(event):
    return str(event.get("tool_name", "")).rsplit(".", 1)[-1]


def permission_key(event, with_id=True):
    if with_id and event.get("tool_use_id"):
        return f"call:{event['tool_use_id']}"
    value = event.get("tool_input")
    if not isinstance(value, dict) or not value:
        return _tool(event) or "permission"
    # Approval prompts can add a description absent from the completed call.
    value = {key: item for key, item in value.items() if key != "description"}
    fingerprint = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:20]
    return f"{_tool(event)}:{fingerprint}"


def _cancelled(response):
    """Require explicit cancellation: an answer containing 'cancel' is valid."""
    if isinstance(response, dict):
        return any(response.get(key) is True for key in ("cancelled", "canceled", "dismissed", "interrupted"))
    if isinstance(response, str):
        return response.strip().lower() in {"cancelled", "canceled", "dismissed", "interrupted",
            "user cancelled", "user canceled", "user dismissed", "user declined to answer questions"}
    return False


def reduce_event(previous, provider, event, owner, now=None, action=None):
    """Pure lifecycle reducer. None clears; the original dict means ignore."""
    now = time.time() if now is None else now
    name, session, tool = str(event.get("hook_event_name", "")), str(event.get("session_id", "")), _tool(event)
    if name in {"SubagentStart", "SubagentStop"} or event.get("agent_id"):
        return previous
    old = previous if visible_record(previous, owner) is previous else {}
    if old.get("session_id") and session and old["session_id"] != session and name != "SessionStart":
        return previous
    incoming_turn = str(event.get("turn_id", ""))
    if (old.get("turn_id") and incoming_turn and old["turn_id"] != incoming_turn
            and name not in {"UserPromptSubmit", "SessionStart", "SessionEnd"}):
        return previous
    if name == "SessionStart" and event.get("source") == "compact":
        return previous
    if name == "SessionEnd" or action == "clear":
        return None
    if name == "Notification" and event.get("notification_type") != "permission_prompt":
        return previous
    if name == "SessionStart":
        old = {}
    record = dict(old)
    record.update(provider=provider, owner_pid=owner["pid"], owner_start=owner["start"],
                  session_id=session or old.get("session_id", ""))
    questions, permissions = dict(old.get("pending_questions", {})), dict(old.get("pending_permissions", {}))
    activity = old.get("activity", old.get("state", "idle"))
    if activity == "waiting":
        activity = "working"
    turn, key = str(event.get("turn_id", "")), str(event.get("tool_use_id") or tool)
    if name == "SessionStart":
        activity, questions, permissions = "idle", {}, {}
    elif name == "UserPromptSubmit":
        # A new user turn acknowledges outstanding questions, including async
        # replies. Tool completion alone never acknowledges an async question.
        activity, questions, permissions = "working", {}, {}
        if not turn:
            record.pop("turn_id", None)
        if not turn or not old.get("turn_started_at") or old.get("activity") != "working" or turn != old.get("turn_id"):
            record["turn_started_at"] = now
    elif name == "PermissionRequest" or name == "Notification":
        permissions[permission_key(event)] = True
    elif name == "PreToolUse":
        activity = "working"
        if tool in QUESTIONS:
            questions[key] = {"tool": tool, "async": tool == "request_user_input_async"}
    elif name in {"PostToolUse", "PostToolUseFailure"}:
        if tool in QUESTIONS:
            if tool != "request_user_input_async":
                questions.pop(key, None)
                activity = "cancelled" if _cancelled(event.get("tool_response")) else "working"
        elif not questions:
            activity = "working"
        permissions.pop(permission_key(event), None)
        permissions.pop(permission_key(event, with_id=False), None)
        if provider == "claude":
            permissions.pop("permission", None)
    elif name == "Stop":
        activity = "idle"
        questions = {key: value for key, value in questions.items() if value.get("async")}
        permissions = {}
    elif name == "Interrupt":
        activity, questions, permissions = "cancelled", {}, {}
    elif action in STATES:
        activity = action
    elif action == "post-tool":
        activity = "cancelled" if _cancelled(event.get("tool_response")) else "working"
    else:
        return previous
    if activity == "working" and not record.get("turn_started_at"):
        record["turn_started_at"] = now
    record.update(activity=activity, state="waiting" if questions or permissions else activity,
                  pending_questions=questions, pending_permissions=permissions, updated_at=now)
    if turn:
        record["turn_id"] = turn
    return record


def notification_detail(event):
    inp = event.get("tool_input") or {}
    if not isinstance(inp, dict):
        inp = {}
    questions = inp.get("questions") or []
    if questions and isinstance(questions[0], dict):
        return str(questions[0].get("question") or questions[0].get("title") or "")[:140]
    if _tool(event) == "ExitPlanMode":
        return "Plan ready for your review"
    return str(inp.get("description") or event.get("message") or "")[:140]


def notify_transition(pane, previous, record, event, socket, now):
    if setting("NOTIFY", "0") == "0":
        return
    state = record["state"]
    waiting = state == "waiting" and previous.get("state") != "waiting"
    reminder = (event.get("hook_event_name") == "Notification"
                and event.get("notification_type") != "permission_prompt" and bool(event.get("message")))
    finished = (event.get("hook_event_name") == "Stop" and record.get("activity") == "idle"
                and previous.get("activity") == "working" and state != "waiting")
    if not (waiting or finished or reminder) or now - previous.get("notified_at", 0) < 5:
        return
    try:
        if pane in tmux(["list-clients", "-F", "#{pane_id}"], socket).splitlines():
            return
        fields = tmux(["display-message", "-p", "-t", pane,
                       "#{session_name}\t#{window_name}\t#{pane_index}\t#{@agent-pulse-name}\t#{pane_title}"], socket).split("\t", 4)
        session, window, index, user_name, title = fields
    except (OSError, subprocess.SubprocessError, ValueError):
        return
    context = f"{session} · {window} · {user_name or index}"
    task = re.sub(r"^[\u2800-\u28ff✳\s]+", "", title)[:80]
    body = (notification_detail(event) or "Needs your input") if waiting or reminder else "Finished"
    if finished:
        duration = int(now - previous.get("turn_started_at", now))
        if duration >= 5:
            body += f" in {duration // 60}m {duration % 60}s" if duration >= 60 else f" in {duration}s"
    if task and (finished or body == "Needs your input"):
        body += f" — {task}"
    # Pass text as argv, never interpolate hook text as AppleScript code.
    script = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run'
    try:
        subprocess.Popen(["osascript", "-e", script, f"{record['provider']} · {context}", body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        sound = "Glass" if finished else "Submarine"
        subprocess.Popen(["/usr/bin/afplay", f"/System/Library/Sounds/{sound}.aiff"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        record["notified_at"] = now
    except OSError:
        pass


def handle_hook(provider, event, pane=None, socket=None, action=None):
    socket = socket or setting("SOCKET") or socket_path()
    if not socket:
        return
    directory = state_directory(socket)
    processes = process_snapshot()
    if pane is None:
        pane = resolve_hook_pane(provider, socket, processes)
    # Explicit --pane/--socket arguments support isolated tests and integrations.
    if not pane or not re.fullmatch(r"%\d+", pane):
        audit_event(provider, event, "unroutable", directory=directory)
        return
    pane_pid = int(tmux(["display-message", "-p", "-t", pane, "#{pane_pid}"], socket))
    owner = find_owner(pane_pid, processes)
    if not owner or (provider != "auto" and owner["provider"] != provider):
        audit_event(provider, event, "owner_mismatch", directory=directory, pane=pane)
        return
    provider = owner["provider"]
    with record_lock(pane, directory):
        previous = load_record(pane, directory)
        now = time.time()
        record = reduce_event(previous, provider, event, owner, now=now, action=action)
        if record is previous:
            if (event.get("hook_event_name") == "Notification"
                    and not event.get("agent_id")
                    and (not previous.get("session_id") or not event.get("session_id")
                         or previous["session_id"] == event["session_id"])
                    and (not previous.get("turn_id") or not event.get("turn_id")
                         or previous["turn_id"] == event["turn_id"])
                    and visible_record(previous, owner) is previous):
                record = dict(previous)
            else:
                audit_event(provider, event, "ignored", directory=directory, pane=pane,
                            record=previous, owner=owner, now=now)
                return
        if record is None:
            delete_record(pane, directory)
            audit_event(provider, event, "deleted", directory=directory, pane=pane,
                        record=previous, owner=owner, now=now)
            return
        notification_previous = previous if visible_record(previous, owner) is previous else {}
        notify_transition(pane, notification_previous, record, event, socket, now)
        save_record(pane, record, directory)
        audit_event(provider, event, "saved", directory=directory, pane=pane,
                    record=record, owner=owner, now=now)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=("claude", "codex", "auto"))
    parser.add_argument("--action")
    parser.add_argument("--pane")
    parser.add_argument("--socket")
    args = parser.parse_args(argv)
    event = {}
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        event = json.loads(raw) if raw.strip() else {}
        if isinstance(event, dict):
            handle_hook(args.provider, event, args.pane, args.socket, args.action)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Reporting must not block execution or change tool approval decisions.
        socket = args.socket or setting("SOCKET") or socket_path()
        if socket:
            audit_event(args.provider, event, "error", directory=state_directory(socket),
                        pane=args.pane, error=error)
    return 0


if __name__ == "__main__":
    sys.exit(main())
