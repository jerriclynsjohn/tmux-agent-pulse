"""Lifecycle and storage regressions; never connect to the user's tmux server."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "lib/agent_pulse.py"
SPEC = importlib.util.spec_from_file_location("agent_pulse_under_test", SOURCE)
status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(status)

OWNER = {"provider": "codex", "pid": 300, "start": "Tue Sep 8 10:00:00 2026"}
CLAUDE_OWNER = {**OWNER, "provider": "claude"}


def event(name, **fields):
    return {"hook_event_name": name, "session_id": "session-a", **fields}


def transition(previous, name, *, now=100, provider="codex", **fields):
    owner = OWNER if provider == "codex" else CLAUDE_OWNER
    return status.reduce_event(previous, provider, event(name, **fields), owner, now=now)


class LifecycleTests(unittest.TestCase):
    def working(self, **fields):
        return transition({}, "UserPromptSubmit", now=100, **fields)

    def question(self, tool="request_user_input", identifier="q1"):
        return transition(self.working(), "PreToolUse", now=110,
                          tool_name=tool, tool_use_id=identifier)

    def test_start_prompt_tools_stop_and_exit(self):
        record = transition({}, "SessionStart", source="startup")
        self.assertEqual(record["state"], "idle")
        record = transition(record, "UserPromptSubmit", now=110)
        self.assertEqual(record["state"], "working")
        for name in ("PreToolUse", "PostToolUse"):
            record = transition(record, name, now=120, tool_name="exec_command", tool_use_id="t1")
            self.assertEqual(record["state"], "working")
        record = transition(record, "Stop", now=130)
        self.assertEqual(record["state"], "idle")
        self.assertIsNone(transition(record, "SessionEnd", now=140))

    def test_answer_containing_cancel_is_an_answer(self):
        for response in ("cancel operation", {"answers": {"next": "Cancel operation"}},
                         {"cancelled": False, "answer": "cancel operation"}):
            with self.subTest(response=response):
                result = transition(self.question(), "PostToolUse", now=120,
                                    tool_name="request_user_input", tool_use_id="q1",
                                    tool_response=response)
                self.assertEqual(result["state"], "working")

    def test_explicit_question_cancellation(self):
        for response in ({"cancelled": True}, {"dismissed": True}, {"interrupted": True},
                         "user declined to answer questions", "cancelled"):
            with self.subTest(response=response):
                result = transition(self.question(), "PostToolUse", now=120,
                                    tool_name="request_user_input", tool_use_id="q1",
                                    tool_response=response)
                self.assertEqual(result["state"], "cancelled")

    def test_no_response_does_not_invent_cancellation(self):
        result = transition(self.question(), "PostToolUse", tool_name="request_user_input",
                            tool_use_id="q1", tool_response=None)
        self.assertEqual(result["state"], "working")

    def test_qualified_tool_names_are_supported(self):
        record = self.question("functions.request_user_input")
        self.assertEqual(record["state"], "waiting")
        record = transition(record, "PostToolUse", tool_name="functions.request_user_input",
                            tool_use_id="q1", tool_response={"answers": {"q": "yes"}})
        self.assertEqual(record["state"], "working")

    def test_unrelated_tool_does_not_acknowledge_question(self):
        record = self.question()
        result = transition(record, "PostToolUse", tool_name="exec_command", tool_use_id="t1")
        self.assertEqual(result["state"], "waiting")

    def test_each_question_id_requires_its_own_answer(self):
        record = transition(self.question(), "PreToolUse", tool_name="request_user_input",
                            tool_use_id="q2")
        record = transition(record, "PostToolUse", tool_name="request_user_input",
                            tool_use_id="unknown", tool_response={"answer": "yes"})
        self.assertEqual(record["state"], "waiting")
        record = transition(record, "PostToolUse", tool_name="request_user_input",
                            tool_use_id="q1", tool_response={"answer": "yes"})
        self.assertEqual(record["state"], "waiting")
        record = transition(record, "PostToolUse", tool_name="request_user_input",
                            tool_use_id="q2", tool_response={"answer": "yes"})
        self.assertEqual(record["state"], "working")

    def test_async_post_and_stop_leave_waiting_until_new_user_message(self):
        record = self.question("request_user_input_async")
        for name, fields in (("PostToolUse", {"tool_name": "request_user_input_async", "tool_use_id": "q1"}),
                             ("PostToolUse", {"tool_name": "exec_command", "tool_use_id": "t1"}),
                             ("Stop", {})):
            record = transition(record, name, now=120, **fields)
            self.assertEqual(record["state"], "waiting", name)
        record = transition(record, "UserPromptSubmit", now=200)
        self.assertEqual(record["state"], "working")
        self.assertEqual(record["turn_started_at"], 200)

    def test_permission_wait_survives_unrelated_tool_completion(self):
        record = transition(self.working(), "PermissionRequest", now=110,
                            tool_name="exec_command", tool_use_id="p1")
        record = transition(record, "PostToolUse", now=115, tool_name="read_file", tool_use_id="other")
        self.assertEqual(record["state"], "waiting")
        record = transition(record, "PostToolUse", now=120, tool_name="exec_command", tool_use_id="p1")
        self.assertEqual(record["state"], "working")

    def test_other_call_of_same_tool_does_not_clear_permission(self):
        record = transition(self.working(), "PermissionRequest", now=110,
                            tool_name="exec_command", tool_use_id="p1")
        record = transition(record, "PostToolUse", now=115,
                            tool_name="exec_command", tool_use_id="other")
        self.assertEqual(record["state"], "waiting")
        record = transition(record, "PostToolUse", now=120,
                            tool_name="exec_command", tool_use_id="p1")
        self.assertEqual(record["state"], "working")

    def test_native_permission_without_call_id_matches_input_not_other_call(self):
        record = transition(self.working(), "PermissionRequest", now=110, tool_name="Bash",
                            tool_input={"command": "npm test", "description": "Run tests"})
        record = transition(record, "PostToolUse", now=115, tool_name="Bash", tool_use_id="other",
                            tool_input={"command": "git status"})
        self.assertEqual(record["state"], "waiting")
        record = transition(record, "PostToolUse", now=120, tool_name="Bash", tool_use_id="approved",
                            tool_input={"command": "npm test"})
        self.assertEqual(record["state"], "working")

    def test_permission_and_question_are_independent(self):
        record = transition(self.question(), "PermissionRequest", tool_name="exec_command", tool_use_id="p1")
        record = transition(record, "PostToolUse", tool_name="exec_command", tool_use_id="p1")
        self.assertEqual(record["state"], "waiting")
        record = transition(record, "PostToolUse", tool_name="request_user_input", tool_use_id="q1",
                            tool_response={"answer": "yes"})
        self.assertEqual(record["state"], "working")

    def test_claude_answer_clears_permission_after_input_gains_answer_fields(self):
        original_input = {"questions": [{"question": "Which order?"}]}
        for name, response, expected in (
                ("PostToolUse", {"answers": {"Which order?": "Priority"}}, "working"),
                ("PostToolUseFailure", {"cancelled": True}, "cancelled")):
            with self.subTest(event=name):
                call = {"tool_name": "AskUserQuestion", "tool_use_id": "question-1",
                        "tool_input": original_input}
                record = transition({}, "UserPromptSubmit", provider="claude")
                record = transition(record, "PreToolUse", provider="claude", **call)
                record = transition(record, "PermissionRequest", provider="claude",
                                    tool_name="AskUserQuestion", tool_input=original_input)
                record = transition(record, "Notification", provider="claude",
                                    notification_type="permission_prompt")
                completed = {**call, "tool_input": {**original_input,
                             "answers": {"Which order?": "Priority"}, "annotations": {}}}
                record = transition(record, name, provider="claude", now=120,
                                    **completed, tool_response=response)
                self.assertEqual(record["state"], expected)
                self.assertFalse(record["pending_questions"])
                self.assertFalse(record["pending_permissions"])
                self.assertEqual(record["turn_started_at"], 100)
                self.assertEqual(original_input, {"questions": [{"question": "Which order?"}]})

    def test_claude_answer_preserves_other_pending_questions(self):
        for second_question in ("Which order?", "Which format?"):
            with self.subTest(second_question=second_question):
                record = transition({}, "UserPromptSubmit", provider="claude")
                calls = [{"tool_name": "AskUserQuestion", "tool_use_id": f"question-{i}",
                          "tool_input": {"questions": [{"question": question}]}}
                         for i, question in enumerate(("Which order?", second_question))]
                for call in calls:
                    record = transition(record, "PreToolUse", provider="claude", **call)
                    record = transition(record, "PermissionRequest", provider="claude",
                                        tool_name=call["tool_name"], tool_input=call["tool_input"])
                for i, call in enumerate(calls):
                    completed = {**call, "tool_input": {**call["tool_input"],
                                 "answers": {"answer": "Selected"}, "annotations": {}}}
                    record = transition(record, "PostToolUse", provider="claude", **completed)
                    self.assertEqual(record["state"], "waiting" if i == 0 else "working")
                    self.assertEqual(len(record["pending_questions"]), 1 - i)
                    if i == 0 and second_question != "Which order?":
                        self.assertEqual(len(record["pending_permissions"]), 1)
                self.assertFalse(record["pending_permissions"])

    def test_answer_fields_remain_part_of_other_tools_permission_identity(self):
        for field in ("answers", "annotations"):
            with self.subTest(field=field):
                tool_input = {"command": "synthetic", field: {"value": "first"}}
                record = transition(self.working(provider="claude"), "PermissionRequest",
                                    provider="claude", tool_name="OtherTool", tool_input=tool_input)
                record = transition(record, "PostToolUse", provider="claude", tool_name="OtherTool",
                                    tool_input={**tool_input, field: {"value": "second"}})
                self.assertEqual(record["state"], "waiting")
                record = transition(record, "PostToolUse", provider="claude", tool_name="OtherTool",
                                    tool_input=tool_input)
                self.assertEqual(record["state"], "working")

    def test_idle_notification_is_not_waiting(self):
        record = self.working()
        self.assertIs(transition(record, "Notification", notification_type="idle_prompt"), record)

    def test_explicit_interrupt_clears_outstanding_waits(self):
        record = transition(self.question(), "Interrupt", now=120)
        self.assertEqual(record["state"], "cancelled")
        self.assertFalse(record["pending_questions"])
        self.assertFalse(record["pending_permissions"])

    def test_mismatched_session_cannot_stop_or_clear_current_session(self):
        record = self.working()
        for name in ("Stop", "SessionEnd", "PostToolUse"):
            with self.subTest(name=name):
                self.assertIs(transition(record, name, session_id="other-session"), record)

    def test_old_turn_stop_cannot_finish_new_turn(self):
        record = self.working(turn_id="turn-a")
        record = transition(record, "UserPromptSubmit", now=200, turn_id="turn-b")
        self.assertIs(transition(record, "Stop", now=210, turn_id="turn-a"), record)
        self.assertIs(transition(record, "PostToolUse", now=220, turn_id="turn-a", tool_name="Bash"), record)
        self.assertEqual(transition(record, "Stop", now=230, turn_id="turn-b")["state"], "idle")

    def test_submit_without_turn_id_does_not_keep_previous_turn_identity(self):
        record = self.working(turn_id="turn-a")
        record = transition(record, "UserPromptSubmit", now=200)
        record = transition(record, "PreToolUse", now=210, turn_id="turn-b", tool_name="Bash")
        self.assertEqual(record["turn_id"], "turn-b")
        self.assertEqual(transition(record, "Stop", now=230, turn_id="turn-b")["state"], "idle")

    def test_explicit_session_start_can_replace_resumed_session(self):
        record = transition(self.working(), "SessionStart", source="resume", session_id="session-b")
        self.assertEqual(record["session_id"], "session-b")
        self.assertEqual(record["state"], "idle")

    def test_subagent_events_do_not_change_parent_state(self):
        record = self.question()
        for name, fields in (("SubagentStart", {}), ("SubagentStop", {}),
                             ("Stop", {"agent_id": "child-id"})):
            self.assertIs(transition(record, name, **fields), record)

    def test_compaction_preserves_parent_state_and_duration(self):
        record = self.question()
        self.assertIs(transition(record, "SessionStart", now=300, source="compact"), record)

    def test_work_duration_is_preserved_across_tools_and_permissions(self):
        record = self.working(turn_id="turn-a")
        for now, name, fields in (
                (110, "PreToolUse", {"tool_name": "exec_command", "tool_use_id": "t1"}),
                (120, "PermissionRequest", {"tool_name": "exec_command", "tool_use_id": "t1"}),
                (190, "PostToolUse", {"tool_name": "exec_command", "tool_use_id": "t1"}),
                (200, "Stop", {})):
            record = transition(record, name, now=now, turn_id="turn-a", **fields)
            self.assertEqual(record["turn_started_at"], 100)
        record = transition(record, "UserPromptSubmit", now=300, turn_id="turn-b")
        self.assertEqual(record["turn_started_at"], 300)

    def test_new_submit_without_turn_id_restarts_elapsed_time(self):
        # A replacement prompt may arrive after an interrupted turn with no Stop.
        record = transition(self.working(), "UserPromptSubmit", now=300)
        self.assertEqual(record["turn_started_at"], 300)

    def test_reducer_does_not_mutate_previous_record_or_store_prompt_text(self):
        record = self.question()
        before = copy.deepcopy(record)
        result = transition(record, "PostToolUse", tool_name="request_user_input", tool_use_id="q1",
                            prompt="private prompt", tool_response={"answer": "private answer"},
                            tool_input={"command": "private command"})
        self.assertEqual(record, before)
        serialized = json.dumps(result)
        for private in ("private prompt", "private answer", "private command"):
            self.assertNotIn(private, serialized)


class OwnershipTests(unittest.TestCase):
    @staticmethod
    def process(parent, command, start="start-a"):
        return {"ppid": parent, "command": command, "start": start}

    def test_native_codex_under_node_wrapper_is_identified(self):
        processes = {100: self.process(1, "/bin/zsh"),
                     200: self.process(100, "/opt/homebrew/bin/node"),
                     300: self.process(200, "/opt/vendor/aarch64-apple-darwin/codex/codex")}
        self.assertEqual(status.find_owner(100, processes),
                         {"provider": "codex", "pid": 300, "start": "start-a"})

    def test_generic_node_is_not_an_agent(self):
        processes = {100: self.process(1, "/bin/zsh"), 200: self.process(100, "/bin/node")}
        self.assertIsNone(status.find_owner(100, processes))
        self.assertIsNone(status.provider_for_command("node"))

    def test_outer_agent_wins_over_child_agent(self):
        processes = {100: self.process(1, "/bin/zsh"),
                     200: self.process(100, "/opt/claude/2.1.263"),
                     300: self.process(200, "/opt/bin/codex")}
        self.assertEqual(status.find_owner(100, processes)["provider"], "claude")

    def test_cycle_in_process_snapshot_does_not_hang(self):
        processes = {100: self.process(200, "/bin/zsh"), 200: self.process(100, "/bin/node")}
        self.assertIsNone(status.find_owner(100, processes))

    def test_reused_pid_shows_unknown_presence_not_false_completion(self):
        for state in ("working", "cancelled"):
            record = {**transition({}, "UserPromptSubmit"), "state": state}
            owner = {**OWNER, "start": "new-process-start"}
            visible = status.visible_record(record, owner)
            self.assertEqual(visible["state"], "unknown")
            self.assertNotIn("session_id", visible)
            new_record = status.reduce_event(record, "codex", event("UserPromptSubmit", session_id="new-session"),
                                             owner, now=500)
            self.assertEqual(new_record["session_id"], "new-session")
            self.assertEqual(new_record["turn_started_at"], 500)

    def test_no_owner_hides_even_cancelled_state(self):
        self.assertIsNone(status.visible_record({"state": "cancelled"}, None))

    def test_live_agent_without_hook_record_shows_unknown_presence(self):
        self.assertEqual(status.visible_record({}, OWNER)["state"], "unknown")

    def test_socket_directories_do_not_collide(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertNotEqual(status.state_directory("/tmp/server-a"), status.state_directory("/tmp/server-b"))

    def test_agentpulse_directory_has_precedence_and_legacy_environment_still_works(self):
        with mock.patch.dict(os.environ, {"AGENT_STATUS_DIR": "/fixture/legacy-state"}, clear=True):
            self.assertEqual(status.state_directory("/fixture/socket"), Path("/fixture/legacy-state"))
            with mock.patch.dict(os.environ, {"AGENT_PULSE_DIR": "/fixture/agent-pulse-state"}):
                self.assertEqual(status.state_directory("/fixture/socket"), Path("/fixture/agent-pulse-state"))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(status.state_directory("/fixture/socket").parent,
                             Path("/tmp") / f"tmux-agent-pulse-{os.getuid()}")

    def test_notify_setting_prefers_agentpulse_and_falls_back_to_legacy(self):
        previous = transition({}, "UserPromptSubmit")
        record = transition(previous, "PermissionRequest", tool_name="Bash")
        for settings, expected_calls in (({}, 0), ({"AGENT_STATUS_NOTIFY": "0"}, 0),
                ({"AGENT_STATUS_NOTIFY": "1", "AGENT_PULSE_NOTIFY": "0"}, 0),
                ({"AGENT_STATUS_NOTIFY": "0", "AGENT_PULSE_NOTIFY": "1"}, 1)):
            with self.subTest(settings=settings), mock.patch.dict(os.environ, settings, clear=True), \
                 mock.patch.object(status, "tmux", return_value="%1") as tmux:
                status.notify_transition("%1", previous, dict(record), event("PermissionRequest"), "/fixture/socket", 200)
                self.assertEqual(tmux.call_count, expected_calls)

    def test_hook_without_exact_pane_and_socket_does_not_guess(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(status, "tmux") as tmux, \
             mock.patch.object(status, "process_snapshot") as snapshot:
            for provider in ("claude", "codex"):
                status.handle_hook(provider, event("Stop"))
            tmux.assert_not_called()
            snapshot.assert_not_called()


class RoutingTests(unittest.TestCase):
    def processes(self):
        return {100: {"ppid": 1, "command": "/bin/zsh", "start": "shell-start"},
                200: {"ppid": 100, "command": "/bin/node", "start": "node-start"},
                300: {"ppid": 200, "command": "/bin/codex", "start": OWNER["start"]},
                400: {"ppid": 300, "command": "/bin/sh", "start": "hook-shell-start"},
                500: {"ppid": 400, "command": "/bin/python3", "start": "hook-start"},
                600: {"ppid": 1, "command": "/bin/zsh", "start": "other-pane-start"}}

    def test_hook_ancestry_resolves_native_codex_below_node(self):
        with mock.patch.object(status, "tmux", return_value="%1\t100\n%2\t600"):
            self.assertEqual(status.resolve_hook_pane("codex", "/test/socket", self.processes(), hook_pid=500), "%1")
            self.assertIsNone(status.resolve_hook_pane("claude", "/test/socket", self.processes(), hook_pid=500))

    def test_socket_setting_prefers_agentpulse_and_falls_back_to_legacy(self):
        for settings, expected in (({"AGENT_STATUS_SOCKET": "/fixture/legacy-socket"}, "/fixture/legacy-socket"),
                ({"AGENT_STATUS_SOCKET": "/fixture/legacy-socket", "AGENT_PULSE_SOCKET": "/fixture/new-socket"},
                 "/fixture/new-socket")):
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as directory, \
                 mock.patch.dict(os.environ, {**settings, "AGENT_PULSE_DIR": directory, "AGENT_PULSE_NOTIFY": "0"}, clear=True), \
                 mock.patch.object(status, "process_snapshot", return_value=self.processes()), \
                 mock.patch.object(status, "tmux", return_value="100") as tmux:
                status.handle_hook("codex", event("UserPromptSubmit"), pane="%1")
                self.assertEqual(tmux.call_args.args[1], expected)
                self.assertEqual(status.load_record("%1", directory)["state"], "working")

    def test_hook_ancestry_ignores_stale_environment_pane(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(os.environ, {"AGENT_PULSE_DIR": directory, "AGENT_PULSE_NOTIFY": "0",
                                          "TMUX": "/test/socket,1,1", "TMUX_PANE": "%99"}, clear=True), \
             mock.patch.object(status.os, "getpid", return_value=500), \
             mock.patch.object(status, "process_snapshot", return_value=self.processes()), \
             mock.patch.object(status, "tmux", side_effect=["%1\t100\n%99\t600", "100"]):
            status.handle_hook("codex", event("UserPromptSubmit"))
            self.assertEqual(status.load_record("%1", directory)["state"], "working")
            self.assertEqual(status.load_record("%99", directory), {})

    def test_detached_daemon_with_stale_tmux_environment_cannot_update_pane(self):
        processes = self.processes()
        processes[400]["ppid"] = 1  # Hook belongs to a detached daemon, outside the pane.
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(os.environ, {"AGENT_PULSE_DIR": directory, "TMUX": "/test/socket,1,1",
                                          "TMUX_PANE": "%1"}, clear=True), \
             mock.patch.object(status.os, "getpid", return_value=500), \
             mock.patch.object(status, "process_snapshot", return_value=processes), \
             mock.patch.object(status, "tmux", return_value="%1\t100\n%2\t600"), \
             mock.patch.object(status, "save_record") as save:
            status.handle_hook("codex", event("UserPromptSubmit"))
            save.assert_not_called()
            self.assertFalse(list(Path(directory).glob("%*.json")))
            audit = json.loads((Path(directory) / "events.jsonl").read_text())
            self.assertEqual(audit["outcome"], "unroutable")
            self.assertNotIn("pane", audit)  # Never report the stale environment pane as resolved.

    def test_linked_window_duplicate_pane_does_not_make_routing_ambiguous(self):
        with mock.patch.object(status, "tmux", return_value="%1\t100\n%1\t100"):
            self.assertEqual(status.resolve_hook_pane("codex", "/test/socket", self.processes(), hook_pid=500), "%1")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def test_atomic_replace_keeps_previous_json_until_commit(self):
        old, new = {"state": "idle", "updated_at": 1}, {"state": "working", "updated_at": 2}
        status.save_record("%1", old, self.directory)
        real_replace = os.replace

        def observe_replace(source, destination):
            self.assertEqual(status.load_record("%1", self.directory), old)
            self.assertEqual(json.loads(Path(source).read_text()), new)
            return real_replace(source, destination)

        with mock.patch.object(status.os, "replace", side_effect=observe_replace):
            status.save_record("%1", new, self.directory)
        self.assertEqual(status.load_record("%1", self.directory), new)
        self.assertEqual([path.name for path in self.directory.iterdir()], ["%1.json"])

    def test_failed_replace_preserves_previous_record_and_removes_temp_file(self):
        old = {"state": "waiting"}
        status.save_record("%1", old, self.directory)
        with mock.patch.object(status.os, "replace", side_effect=OSError("simulated write failure")):
            with self.assertRaises(OSError):
                status.save_record("%1", {"state": "idle"}, self.directory)
        self.assertEqual(status.load_record("%1", self.directory), old)
        self.assertEqual([path.name for path in self.directory.iterdir()], ["%1.json"])

    def test_recovery_compare_and_swap_rejects_newer_hook_record(self):
        waiting = transition({}, "PreToolUse", provider="claude", now=100,
                             tool_name="AskUserQuestion", tool_use_id="q1")
        status.save_record("%1", waiting, self.directory)
        newer = transition(waiting, "PreToolUse", provider="claude", now=200,
                           tool_name="AskUserQuestion", tool_use_id="q2")
        status.save_record("%1", newer, self.directory)
        self.assertFalse(status.update_recovery("%1", 100, "idle", self.directory))
        self.assertEqual(status.load_record("%1", self.directory), newer)

    def test_matching_claude_recovery_commits_once(self):
        waiting = transition({}, "PreToolUse", provider="claude", now=100,
                             tool_name="AskUserQuestion", tool_use_id="q1")
        status.save_record("%1", waiting, self.directory)
        self.assertTrue(status.update_recovery("%1", 100, "working", self.directory))
        recovered = status.load_record("%1", self.directory)
        self.assertEqual(recovered["state"], "working")
        self.assertEqual(recovered["turn_started_at"], 100)
        self.assertFalse(status.update_recovery("%1", 100, "idle", self.directory))

    def test_recovery_never_scrapes_codex_back_to_idle(self):
        waiting = transition({}, "PreToolUse", now=100, tool_name="request_user_input", tool_use_id="q1")
        status.save_record("%1", waiting, self.directory)
        self.assertFalse(status.update_recovery("%1", 100, "idle", self.directory))
        self.assertEqual(status.load_record("%1", self.directory), waiting)

    def test_malformed_or_missing_record_is_empty(self):
        self.assertEqual(status.load_record("%1", self.directory), {})
        (self.directory / "%1.json").write_text('{"state":')
        self.assertEqual(status.load_record("%1", self.directory), {})

    def test_invalid_pane_cannot_escape_state_directory(self):
        with self.assertRaises(ValueError):
            status.save_record("../escaped", {"state": "idle"}, self.directory)
        self.assertFalse(list(self.directory.iterdir()))

    def test_new_owner_does_not_notify_completion_of_previous_agents_turn(self):
        previous = transition({}, "UserPromptSubmit", now=100)
        status.save_record("%1", previous, self.directory)
        processes = {100: {"ppid": 1, "command": "/bin/zsh", "start": "shell-start"},
                     300: {"ppid": 100, "command": "/bin/codex", "start": "new-process-start"}}
        with mock.patch.dict(os.environ, {"AGENT_PULSE_DIR": str(self.directory), "AGENT_PULSE_NOTIFY": "1"}), \
             mock.patch.object(status, "process_snapshot", return_value=processes), \
             mock.patch.object(status, "tmux", side_effect=["100", "", "session\twindow\t1\t\tTask"]), \
             mock.patch.object(status.time, "time", return_value=500), \
             mock.patch.object(status.subprocess, "Popen") as popen:
            status.handle_hook("codex", event("Stop", session_id="new-session"), pane="%1", socket="/test/socket")
        popen.assert_not_called()
        current = status.load_record("%1", self.directory)
        self.assertEqual(current["owner_start"], "new-process-start")
        self.assertEqual(current["state"], "idle")

    def test_idle_reminder_notifies_without_changing_state_then_debounces(self):
        previous = transition({}, "SessionStart", provider="claude", now=100)
        status.save_record("%1", previous, self.directory)
        processes = {100: {"ppid": 1, "command": "/bin/zsh", "start": "shell-start"},
                     300: {"ppid": 100, "command": "/bin/claude", "start": OWNER["start"]}}
        reminder = event("Notification", notification_type="idle_prompt", message="Ready when you are")
        with mock.patch.dict(os.environ, {"AGENT_PULSE_DIR": str(self.directory), "AGENT_PULSE_NOTIFY": "1"}), \
             mock.patch.object(status, "process_snapshot", return_value=processes), \
             mock.patch.object(status, "tmux", side_effect=["100", "", "session\twindow\t1\tcustom-name\tTask", "100"]), \
             mock.patch.object(status.time, "time", side_effect=[200, 200, 202, 202]), \
             mock.patch.object(status.subprocess, "Popen") as popen:
            status.handle_hook("claude", reminder, pane="%1", socket="/test/socket")
            status.handle_hook("claude", reminder, pane="%1", socket="/test/socket")
        self.assertEqual(popen.call_count, 2)  # One notification and one sound, not two of each.
        self.assertEqual(popen.call_args_list[0].args[0][-2:],
                         ["claude · session · window · custom-name", "Ready when you are"])
        current = status.load_record("%1", self.directory)
        self.assertEqual(current["state"], "idle")
        self.assertEqual(current["activity"], "idle")
        self.assertEqual(current["updated_at"], previous["updated_at"])
        self.assertEqual(current["notified_at"], 200)

    def test_reminder_is_silent_for_focused_pane(self):
        previous = transition({}, "SessionStart", provider="claude", now=100)
        record = dict(previous)
        with mock.patch.dict(os.environ, {"AGENT_PULSE_NOTIFY": "1"}), \
             mock.patch.object(status, "tmux", return_value="%1\n%2"), \
             mock.patch.object(status.subprocess, "Popen") as popen:
            status.notify_transition("%1", previous, record,
                                     event("Notification", notification_type="idle_prompt", message="Ready"),
                                     "/test/socket", 200)
        popen.assert_not_called()
        self.assertEqual(record, previous)

    def test_ignored_foreign_or_subagent_reminder_cannot_notify_as_parent(self):
        previous = transition({}, "UserPromptSubmit", provider="claude", now=100, turn_id="turn-current")
        processes = {100: {"ppid": 1, "command": "/bin/zsh", "start": "shell-start"},
                     300: {"ppid": 100, "command": "/bin/claude", "start": OWNER["start"]}}
        for unrelated in ({"session_id": "foreign-session"}, {"agent_id": "child-agent"},
                          {"turn_id": "old-turn"}):
            with self.subTest(unrelated=unrelated):
                status.save_record("%1", previous, self.directory)
                reminder = event("Notification", notification_type="idle_prompt", message="Unrelated reminder", **unrelated)
                with mock.patch.dict(os.environ, {"AGENT_PULSE_DIR": str(self.directory), "AGENT_PULSE_NOTIFY": "1"}), \
                     mock.patch.object(status, "process_snapshot", return_value=processes), \
                     mock.patch.object(status, "tmux", side_effect=["100", "", "session\twindow\t1\t\tTask"]), \
                     mock.patch.object(status.time, "time", return_value=200), \
                     mock.patch.object(status.subprocess, "Popen") as popen:
                    status.handle_hook("claude", reminder, pane="%1", socket="/test/socket")
                popen.assert_not_called()
                self.assertEqual(status.load_record("%1", self.directory), previous)


if __name__ == "__main__":
    unittest.main()
