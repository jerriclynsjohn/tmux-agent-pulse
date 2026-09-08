"""Bounded lifecycle diagnostics without conversation content or live tmux."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "lib/agent_pulse.py"
SPEC = importlib.util.spec_from_file_location("agent_pulse_audit_under_test", SOURCE)
status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(status)

OWNER = {"provider": "codex", "pid": 300, "start": "owner-start"}
PROCESSES = {100: {"ppid": 1, "command": "/bin/zsh", "start": "shell-start"},
             300: {"ppid": 100, "command": "/bin/codex", "start": OWNER["start"]}}


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        patch = mock.patch.dict(os.environ, {"AGENT_PULSE_DIR": str(self.directory),
                                            "AGENT_PULSE_NOTIFY": "0"}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def entries(self):
        return [json.loads(line) for line in (self.directory / "events.jsonl").read_text().splitlines()]

    def hook(self, name, **fields):
        event = {"hook_event_name": name, "session_id": "session-a", "turn_id": "turn-a", **fields}
        with mock.patch.object(status, "process_snapshot", return_value=PROCESSES), \
             mock.patch.object(status, "tmux", return_value="100"):
            status.handle_hook("codex", event, pane="%1", socket="/private/test-socket")

    def test_save_ignore_and_delete_explain_record_lifecycle(self):
        self.hook("UserPromptSubmit")
        self.hook("Stop", session_id="foreign-session")
        self.assertEqual(status.load_record("%1", self.directory)["state"], "working")
        self.hook("SessionEnd")
        self.assertFalse((self.directory / "%1.json").exists())
        entries = self.entries()
        self.assertEqual([entry["outcome"] for entry in entries], ["saved", "ignored", "deleted"])
        self.assertEqual([entry["event"] for entry in entries], ["UserPromptSubmit", "Stop", "SessionEnd"])
        self.assertEqual(entries[1]["session_id"], "foreign-session")
        self.assertEqual(entries[2]["session_id"], "session-a")
        for entry in entries:
            self.assertEqual((entry["pane"], entry["owner_pid"], entry["state"]), ("%1", 300, "working"))

    def test_only_allowlisted_metadata_is_persisted(self):
        secret = "PRIVATE conversation, tool arguments, response and exception text"
        event = {"hook_event_name": "PostToolUse", "session_id": "session-a", "turn_id": "turn-a",
                 "prompt": secret, "message": secret, "tool_name": secret, "tool_input": {"command": secret},
                 "tool_response": secret, "last_assistant_message": secret, "transcript_path": secret}
        status.audit_event("codex", event, "error", directory=self.directory, pane="%1",
                           record={"state": "working", "pending_questions": {"q1": secret}},
                           owner=OWNER, error=OSError(secret), now=100)
        entry = self.entries()[0]
        self.assertEqual(entry, {"time": 100, "provider": "codex", "pane": "%1", "event": "PostToolUse",
                                 "outcome": "error", "state": "working", "session_id": "session-a",
                                 "turn_id": "turn-a", "owner_pid": 300, "error": "OSError"})
        self.assertNotIn(secret, (self.directory / "events.jsonl").read_text())
        self.assertEqual((self.directory / "events.jsonl").stat().st_mode & 0o777, 0o600)

    def test_oversized_or_structured_metadata_cannot_expand_log_entries(self):
        status.audit_event("codex", {"hook_event_name": {"prompt": "secret"},
                                     "session_id": "x" * 200000, "turn_id": "private\ntext"},
                           "ignored", directory=self.directory, now=100)
        self.assertEqual(self.entries(), [{"time": 100, "provider": "codex", "outcome": "ignored"}])

    def test_rotation_keeps_only_two_bounded_complete_jsonl_files(self):
        with mock.patch.object(status, "AUDIT_MAX_BYTES", 1024):
            for number in range(100):
                status.audit_event("codex", {"hook_event_name": "PreToolUse", "turn_id": f"turn-{number}"},
                                   "saved", directory=self.directory, pane="%1", now=number)
        files = sorted(self.directory.glob("events.jsonl*"))
        self.assertEqual([path.name for path in files], ["events.jsonl", "events.jsonl.1"])
        for path in files:
            self.assertLessEqual(path.stat().st_size, 1024)
            self.assertTrue(path.read_bytes().endswith(b"\n"))
            self.assertTrue(all(isinstance(json.loads(line), dict) for line in path.read_text().splitlines()))
        self.assertEqual(self.entries()[-1]["turn_id"], "turn-99")

    def test_concurrent_appends_remain_complete_and_do_not_lose_entries(self):
        def append(number):
            status.audit_event("codex", {"hook_event_name": "PostToolUse", "turn_id": f"turn-{number}"},
                               "saved", directory=self.directory, pane=f"%{number}", now=number)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(append, range(80)))
        entries = self.entries()
        self.assertEqual(len(entries), 80)
        self.assertEqual({entry["turn_id"] for entry in entries}, {f"turn-{number}" for number in range(80)})

    def test_swallowed_process_timeout_leaves_class_only_error_evidence(self):
        event = {"hook_event_name": "UserPromptSubmit", "session_id": "session-a",
                 "turn_id": "turn-a", "prompt": "PRIVATE prompt"}
        with mock.patch.object(status.sys, "argv", ["agent_pulse.py", "codex", "--socket", "/test/socket"]), \
             mock.patch.object(status.sys, "stdin", io.StringIO(json.dumps(event))), \
             mock.patch.object(status, "process_snapshot", side_effect=subprocess.TimeoutExpired("PRIVATE command", 3)), \
             mock.patch.dict(os.environ, {"TMUX_PANE": "%99"}):
            self.assertEqual(status.main(), 0)
        entry = self.entries()[0]
        self.assertEqual((entry["outcome"], entry["error"], entry["event"]),
                         ("error", "TimeoutExpired", "UserPromptSubmit"))
        self.assertEqual((entry["session_id"], entry["turn_id"]), ("session-a", "turn-a"))
        self.assertNotIn("pane", entry)
        self.assertNotIn("PRIVATE", (self.directory / "events.jsonl").read_text())
        self.assertFalse(list(self.directory.glob("%*.json")))

    def test_bad_json_leaves_error_evidence_without_storing_input(self):
        with mock.patch.object(status.sys, "argv", ["agent_pulse.py", "codex", "--socket", "/test/socket"]), \
             mock.patch.object(status.sys, "stdin", io.StringIO('{"prompt": "PRIVATE')):
            self.assertEqual(status.main(), 0)
        self.assertEqual(self.entries()[0]["error"], "JSONDecodeError")
        self.assertNotIn("PRIVATE", (self.directory / "events.jsonl").read_text())

    def test_audit_storage_failure_does_not_prevent_state_save(self):
        (self.directory / "events.jsonl").mkdir()  # Cannot append to a directory.
        self.hook("UserPromptSubmit")
        self.assertEqual(status.load_record("%1", self.directory)["state"], "working")


if __name__ == "__main__":
    unittest.main()
