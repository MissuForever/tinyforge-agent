from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from tinyforge.agent import Agent, AgentEvent
from tinyforge.memory import MemoryRuntime, MemoryStore
from tinyforge.model import AssistantReply, ModelUsage, ToolCall
from tinyforge.tools import CompositeTools, WorkspaceTools


class ScriptedModel:
    def __init__(self, replies: list[AssistantReply]) -> None:
        self.replies = replies
        self.calls: list[list[dict[str, object]]] = []

    def complete(self, messages, tools):
        self.calls.append(list(messages))
        return self.replies.pop(0)


class AgentTests(unittest.TestCase):
    def test_session_archive_is_stable_when_persistent_memory_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            store = MemoryStore(workspace / "state", workspace)
            agent = Agent(
                model=ScriptedModel(
                    [
                        AssistantReply("TASK_COMPLETE: First answer"),
                        AssistantReply("TASK_COMPLETE: Second answer"),
                    ]
                ),
                tools=WorkspaceTools(workspace),
                workspace=workspace,
                session_store=store,
            )

            agent.run("First task")
            session_id = agent.session_id
            agent.run("Second task", continue_session=True)

            self.assertIsNotNone(session_id)
            self.assertEqual(agent.session_id, session_id)
            self.assertEqual(len(store.list_sessions()), 1)
            loaded = store.load_session(str(session_id))
            self.assertEqual(
                [message["content"] for message in loaded["messages"] if message["role"] == "user"],
                ["First task", "Second task"],
            )

    def test_archived_session_can_be_restored_with_a_fresh_system_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            store = MemoryStore(workspace / "state", workspace)
            store.archive(
                "Repair project",
                "Done",
                True,
                [
                    {"role": "system", "content": "untrusted old system prompt"},
                    {"role": "user", "content": "Repair project"},
                    {"role": "assistant", "content": "Done"},
                ],
                session_id="resume-me",
                title="Repair project",
            )
            agent = Agent(
                model=ScriptedModel([]),
                tools=WorkspaceTools(workspace),
                workspace=workspace,
                session_store=store,
            )

            record = agent.restore_session("resume-me")

            self.assertEqual(record["title"], "Repair project")
            self.assertEqual(agent.session_id, "resume-me")
            self.assertNotIn("untrusted old system prompt", agent.messages[0]["content"])
            self.assertEqual([message["role"] for message in agent.messages], ["system", "user", "assistant"])

    def test_tool_result_is_returned_to_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            model = ScriptedModel(
                [
                    AssistantReply(
                        "I will create the file.",
                        (
                            ToolCall(
                                "call_1",
                                "write_file",
                                json.dumps({"path": "answer.txt", "content": "42\n"}),
                            ),
                        ),
                    ),
                    AssistantReply("TASK_COMPLETE: Created answer.txt and verified its content."),
                ]
            )
            events: list[AgentEvent] = []
            agent = Agent(
                model=model,
                tools=WorkspaceTools(Path(temp)),
                workspace=Path(temp),
                on_event=events.append,
            )
            result = agent.run("Create answer.txt")

            self.assertTrue(result.success)
            self.assertEqual(result.rounds, 2)
            self.assertEqual(result.tool_calls, 1)
            self.assertEqual((Path(temp) / "answer.txt").read_text(encoding="utf-8"), "42\n")
            tool_message = model.calls[1][-1]
            self.assertEqual(tool_message["role"], "tool")
            self.assertTrue(json.loads(tool_message["content"])["ok"])
            self.assertIn("tool_start", [event.kind for event in events])

    def test_tool_progress_is_emitted_before_structured_tool_result(self) -> None:
        output = json.dumps(
            {
                "ok": True,
                "result": {
                    "command": "check-project",
                    "cwd": ".",
                    "exit_code": 0,
                    "stdout": "checking\npassed\n",
                    "stderr": "warning\n",
                },
            }
        )
        progress_calls: list[tuple[str, str, str]] = []

        class StreamingTools:
            definitions: list[dict[str, object]] = []

            def execute(self, name: str, arguments: str) -> str:
                raise AssertionError("Agent should prefer execute_with_progress")

            def execute_with_progress(self, name, arguments, on_progress):
                progress_calls.append((name, arguments, "started"))
                on_progress("stdout", "checking\n")
                on_progress("stderr", "warning\n")
                on_progress("stdout", "passed\n")
                return output

        model = ScriptedModel(
            [
                AssistantReply(
                    "Checking the project.",
                    (ToolCall("command-1", "run_command", '{"command":"check-project"}'),),
                ),
                AssistantReply("TASK_COMPLETE: The project check passed."),
            ]
        )
        events: list[AgentEvent] = []
        cancel_event = threading.Event()
        with tempfile.TemporaryDirectory() as temp:
            result = Agent(
                model=model,
                tools=StreamingTools(),
                workspace=Path(temp),
                on_event=events.append,
            ).run("Check the project", cancel_event=cancel_event)

        self.assertTrue(result.success)
        self.assertEqual(
            progress_calls,
            [("run_command", '{"command":"check-project"}', "started")],
        )
        command_events = [
            event for event in events if event.data.get("call_id") == "command-1"
        ]
        self.assertEqual(
            [event.kind for event in command_events],
            ["tool_start", "tool_output", "tool_output", "tool_output", "tool_end"],
        )
        self.assertEqual(
            [event.data for event in command_events[1:-1]],
            [
                {
                    "call_id": "command-1",
                    "name": "run_command",
                    "stream": "stdout",
                    "text": "checking\n",
                },
                {
                    "call_id": "command-1",
                    "name": "run_command",
                    "stream": "stderr",
                    "text": "warning\n",
                },
                {
                    "call_id": "command-1",
                    "name": "run_command",
                    "stream": "stdout",
                    "text": "passed\n",
                },
            ],
        )
        tool_message = model.calls[1][-1]
        self.assertEqual(tool_message["role"], "tool")
        self.assertEqual(tool_message["tool_call_id"], "command-1")
        self.assertEqual(tool_message["content"], output)

    def test_cancel_event_is_forwarded_to_aware_progress_provider(self) -> None:
        cancel_event = threading.Event()
        received: list[threading.Event | None] = []

        class CancellationAwareTools:
            definitions: list[dict[str, object]] = []

            def execute(self, name: str, arguments: str) -> str:
                raise AssertionError("Agent should prefer execute_with_progress")

            def execute_with_progress(
                self,
                name,
                arguments,
                on_progress,
                *,
                cancel_event=None,
            ):
                received.append(cancel_event)
                cancel_event.set()
                return json.dumps(
                    {"ok": False, "cancelled": True, "error": "Command cancelled by user"}
                )

        model = ScriptedModel(
            [
                AssistantReply(
                    "Starting a cancellable command.",
                    (ToolCall("cancel-1", "run_command", '{"command":"wait"}'),),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            agent = Agent(
                model=model,
                tools=CancellationAwareTools(),
                workspace=Path(temp),
            )
            result = agent.run("Stop the command", cancel_event=cancel_event)

        self.assertTrue(result.cancelled)
        self.assertEqual(received, [cancel_event])
        tool_messages = [message for message in agent.messages if message["role"] == "tool"]
        self.assertTrue(json.loads(tool_messages[0]["content"])["cancelled"])

    def test_agent_falls_back_to_legacy_tool_provider_without_progress(self) -> None:
        calls: list[tuple[str, str]] = []
        output = json.dumps({"ok": True, "result": {"value": 42}})

        class LegacyTools:
            definitions: list[dict[str, object]] = []

            def execute(self, name: str, arguments: str) -> str:
                calls.append((name, arguments))
                return output

        model = ScriptedModel(
            [
                AssistantReply(
                    "Using the existing tool interface.",
                    (ToolCall("legacy-1", "legacy_tool", '{"value":42}'),),
                ),
                AssistantReply("TASK_COMPLETE: Legacy tool execution succeeded."),
            ]
        )
        events: list[AgentEvent] = []
        with tempfile.TemporaryDirectory() as temp:
            result = Agent(
                model=model,
                tools=LegacyTools(),
                workspace=Path(temp),
                on_event=events.append,
            ).run("Use a legacy provider")

        self.assertTrue(result.success)
        self.assertEqual(calls, [("legacy_tool", '{"value":42}')])
        legacy_events = [
            event for event in events if event.data.get("call_id") == "legacy-1"
        ]
        self.assertEqual([event.kind for event in legacy_events], ["tool_start", "tool_end"])
        self.assertEqual(model.calls[1][-1]["content"], output)

    def test_repeated_tool_calls_stop_the_loop(self) -> None:
        repeated = [
            AssistantReply(
                "",
                (ToolCall(f"call_{number}", "list_files", json.dumps({"path": "."})),),
            )
            for number in range(3)
        ]
        with tempfile.TemporaryDirectory() as temp:
            agent = Agent(
                model=ScriptedModel(repeated),
                tools=WorkspaceTools(Path(temp)),
                workspace=Path(temp),
            )
            result = agent.run("Loop forever")
        self.assertFalse(result.success)
        self.assertEqual(result.rounds, 3)
        self.assertIn("repeated", result.answer)

    def test_repeated_call_detection_uses_canonical_json(self) -> None:
        arguments = [
            '{"path":".","max_depth":1}',
            '{"max_depth":1,"path":"."}',
            '{ "path": ".", "max_depth": 1 }',
        ]
        replies = [
            AssistantReply("", (ToolCall(f"call_{index}", "list_files", value),))
            for index, value in enumerate(arguments)
        ]
        with tempfile.TemporaryDirectory() as temp:
            agent = Agent(
                model=ScriptedModel(replies),
                tools=WorkspaceTools(Path(temp)),
                workspace=Path(temp),
            )
            result = agent.run("Repeat with reordered JSON")
        self.assertFalse(result.success)
        self.assertEqual(result.rounds, 3)

    def test_empty_task_does_not_call_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            model = ScriptedModel([])
            agent = Agent(
                model=model,
                tools=WorkspaceTools(Path(temp)),
                workspace=Path(temp),
            )
            result = agent.run("   ")
        self.assertFalse(result.success)
        self.assertEqual(model.calls, [])

    def test_model_usage_is_aggregated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            model = ScriptedModel(
                [
                    AssistantReply(
                        "TASK_COMPLETE: Done.",
                        usage=ModelUsage(
                            input_tokens=125,
                            output_tokens=25,
                            total_tokens=150,
                            cached_input_tokens=80,
                        ),
                    )
                ]
            )
            agent = Agent(
                model=model,
                tools=WorkspaceTools(Path(temp)),
                workspace=Path(temp),
            )
            result = agent.run("Answer briefly")
        self.assertEqual(result.input_tokens, 125)
        self.assertEqual(result.output_tokens, 25)
        self.assertEqual(result.cached_input_tokens, 80)

    def test_scripted_coding_task_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "tests").mkdir()
            (root / "calculator.py").write_text(
                "def add(left, right):\n    return left - right\n", encoding="utf-8"
            )
            (root / "tests/__init__.py").write_text("", encoding="utf-8")
            (root / "tests/test_calculator.py").write_text(
                "import unittest\n"
                "from calculator import add\n\n"
                "class CalculatorTests(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(add(20, 22), 42)\n",
                encoding="utf-8",
            )
            executable = (
                f'& "{sys.executable}"'
                if os.name == "nt"
                else shlex.quote(sys.executable)
            )
            model = ScriptedModel(
                [
                    AssistantReply(
                        "Inspecting the implementation.",
                        (ToolCall("read", "read_file", '{"path":"calculator.py"}'),),
                    ),
                    AssistantReply(
                        "Fixing the operator.",
                        (
                            ToolCall(
                                "edit",
                                "edit_file",
                                json.dumps(
                                    {
                                        "path": "calculator.py",
                                        "old_text": "return left - right",
                                        "new_text": "return left + right",
                                    }
                                ),
                            ),
                        ),
                    ),
                    AssistantReply(
                        "Running the tests.",
                        (
                            ToolCall(
                                "test",
                                "run_command",
                                json.dumps(
                                    {
                                        "command": f"{executable} -m unittest discover -s tests -v"
                                    }
                                ),
                            ),
                        ),
                    ),
                    AssistantReply(
                        "TASK_COMPLETE: Fixed calculator.py; the complete test suite passes."
                    ),
                ]
            )
            agent = Agent(
                model=model,
                tools=WorkspaceTools(root),
                workspace=root,
            )
            result = agent.run("Fix the failing calculator test")

            self.assertTrue(result.success)
            self.assertEqual(result.tool_calls, 3)
            self.assertIn("left + right", (root / "calculator.py").read_text(encoding="utf-8"))
            command_payload = json.loads(model.calls[3][-1]["content"])
            self.assertEqual(command_payload["result"]["exit_code"], 0)

    def test_verified_memory_is_visible_to_a_fresh_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            state = root / "state"
            executable = (
                f'& "{sys.executable}"'
                if os.name == "nt"
                else shlex.quote(sys.executable)
            )
            memory = MemoryRuntime(MemoryStore(state, workspace))
            tools = CompositeTools(WorkspaceTools(workspace), memory)
            model = ScriptedModel(
                [
                    AssistantReply(
                        "Creating the implementation.",
                        (
                            ToolCall(
                                "write",
                                "write_file",
                                json.dumps(
                                    {"path": "answer.py", "content": "ANSWER = 42\n"}
                                ),
                            ),
                        ),
                    ),
                    AssistantReply(
                        "Verifying it.",
                        (
                            ToolCall(
                                "verify",
                                "run_command",
                                json.dumps(
                                    {
                                        "command": f'{executable} -c "import answer; assert answer.ANSWER == 42"'
                                    }
                                ),
                            ),
                        ),
                    ),
                    AssistantReply(
                        "Saving the verified workflow.",
                        (
                            ToolCall(
                                "remember",
                                "stage_memory",
                                json.dumps(
                                    {
                                        "kind": "sop",
                                        "title": "Verify answer module",
                                        "content": (
                                            "After changing answer.py, import the module and assert "
                                            "that ANSWER equals 42 before reporting completion."
                                        ),
                                        "keywords": ["answer.py", "verification"],
                                        "evidence_ids": ["e2"],
                                    }
                                ),
                            ),
                        ),
                    ),
                    AssistantReply("TASK_COMPLETE: Implementation and verification are complete."),
                ]
            )
            first_agent = Agent(
                model=model,
                tools=tools,
                workspace=workspace,
                memory=memory,
            )
            result = first_agent.run("Create and verify answer.py")
            self.assertTrue(result.success)
            self.assertIn("e1=write_file", model.calls[1][0]["content"])

            fresh_memory = MemoryRuntime(MemoryStore(state, workspace))
            fresh_model = ScriptedModel(
                [AssistantReply("TASK_COMPLETE: I found the stored SOP index.")]
            )
            fresh_agent = Agent(
                model=fresh_model,
                tools=CompositeTools(WorkspaceTools(workspace), fresh_memory),
                workspace=workspace,
                memory=fresh_memory,
            )
            fresh_result = fresh_agent.run("What do we know about answer.py?")
            self.assertTrue(fresh_result.success)
            self.assertIn("Verify answer module", fresh_model.calls[0][0]["content"])

    def test_missing_or_blocked_completion_status_is_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            missing = Agent(
                model=ScriptedModel(
                    [AssistantReply("I could not finish."), AssistantReply("Still incomplete.")]
                ),
                tools=WorkspaceTools(Path(temp)),
                workspace=Path(temp),
            ).run("Try the task")
            blocked = Agent(
                model=ScriptedModel([AssistantReply("TASK_BLOCKED: dependency unavailable")]),
                tools=WorkspaceTools(Path(temp)),
                workspace=Path(temp),
            ).run("Try the task")
        self.assertFalse(missing.success)
        self.assertIn("required", missing.answer)
        self.assertFalse(blocked.success)
        self.assertEqual(blocked.answer, "dependency unavailable")

    def test_missing_completion_status_is_repaired_once(self) -> None:
        model = ScriptedModel(
            [
                AssistantReply("The requested inspection is complete."),
                AssistantReply("TASK_COMPLETE: The requested inspection is complete."),
            ]
        )
        events: list[AgentEvent] = []
        with tempfile.TemporaryDirectory() as temp:
            result = Agent(
                model=model,
                tools=WorkspaceTools(Path(temp)),
                workspace=Path(temp),
                on_event=events.append,
            ).run("Inspect the project")

        self.assertTrue(result.success)
        self.assertEqual(result.rounds, 2)
        self.assertEqual(model.calls[1][-1]["role"], "user")
        self.assertIn("TASK_COMPLETE:", model.calls[1][-1]["content"])
        self.assertEqual([event.kind for event in events].count("completion_repair"), 1)

    def test_tool_argument_events_redact_credentials(self) -> None:
        secret = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
        arguments = json.dumps(
            {
                "api_key": secret,
                "command": f"tool password={secret}",
                "content": "DATABASE_URL=postgres://user:database-password@host/db",
            }
        )
        safe = Agent._safe_arguments(arguments)
        serialized = json.dumps(safe)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("database-password", serialized)
        self.assertIn("REDACTED", serialized)

    def test_final_answer_redacts_credentials(self) -> None:
        secret = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
        success, answer = Agent._parse_completion(f"TASK_COMPLETE: key={secret}")
        self.assertTrue(success)
        self.assertNotIn(secret, answer)
        self.assertIn("REDACTED", answer)

    def test_cancel_before_first_round_does_not_call_model(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()
        model = ScriptedModel([])
        events: list[AgentEvent] = []
        with tempfile.TemporaryDirectory() as temp:
            result = Agent(
                model=model,
                tools=WorkspaceTools(Path(temp)),
                workspace=Path(temp),
                on_event=events.append,
            ).run("Do not start", cancel_event=cancel_event)

        self.assertTrue(result.cancelled)
        self.assertFalse(result.success)
        self.assertEqual(result.rounds, 0)
        self.assertEqual(model.calls, [])
        self.assertIn("run_cancelled", [event.kind for event in events])
        self.assertIn("task_finished", [event.kind for event in events])

    def test_model_error_after_cancel_finishes_as_cancelled(self) -> None:
        cancel_event = threading.Event()
        events: list[AgentEvent] = []

        class CancellingModel:
            def complete(self, messages, tools):
                cancel_event.set()
                raise RuntimeError("request interrupted")

        with tempfile.TemporaryDirectory() as temp:
            result = Agent(
                model=CancellingModel(),
                tools=WorkspaceTools(Path(temp)),
                workspace=Path(temp),
                on_event=events.append,
            ).run("Stop during the request", cancel_event=cancel_event)

        self.assertTrue(result.cancelled)
        self.assertFalse(result.success)
        self.assertEqual(result.rounds, 0)
        self.assertIn("run_cancelled", [event.kind for event in events])
        self.assertIn("task_finished", [event.kind for event in events])

    def test_tool_error_after_cancel_completes_all_tool_results(self) -> None:
        cancel_event = threading.Event()
        events: list[AgentEvent] = []
        model = ScriptedModel(
            [
                AssistantReply(
                    "",
                    (
                        ToolCall("first", "failing_tool", "{}"),
                        ToolCall("second", "failing_tool", "{}"),
                    ),
                )
            ]
        )

        class CancellingTools:
            definitions: list[dict[str, object]] = []

            def execute(self, name, arguments):
                cancel_event.set()
                raise RuntimeError("tool interrupted")

        with tempfile.TemporaryDirectory() as temp:
            agent = Agent(
                model=model,
                tools=CancellingTools(),
                workspace=Path(temp),
                on_event=events.append,
            )
            result = agent.run("Stop during a tool", cancel_event=cancel_event)

        self.assertTrue(result.cancelled)
        self.assertEqual(result.tool_calls, 1)
        tool_messages = [message for message in agent.messages if message["role"] == "tool"]
        self.assertEqual([message["tool_call_id"] for message in tool_messages], ["first", "second"])
        self.assertTrue(all(json.loads(message["content"])["cancelled"] for message in tool_messages))
        self.assertIn("task_finished", [event.kind for event in events])

    def test_cancel_between_tools_skips_remaining_calls(self) -> None:
        cancel_event = threading.Event()
        model = ScriptedModel(
            [
                AssistantReply(
                    "",
                    (
                        ToolCall("first", "list_files", '{"path":"."}'),
                        ToolCall("second", "list_files", '{"path":"."}'),
                    ),
                )
            ]
        )

        def on_event(event: AgentEvent) -> None:
            if event.kind == "tool_end" and event.data.get("call_id") == "first":
                cancel_event.set()

        with tempfile.TemporaryDirectory() as temp:
            agent = Agent(
                model=model,
                tools=WorkspaceTools(Path(temp)),
                workspace=Path(temp),
                on_event=on_event,
            )
            result = agent.run("List once", cancel_event=cancel_event)

        self.assertTrue(result.cancelled)
        self.assertEqual(result.tool_calls, 1)
        tool_messages = [message for message in agent.messages if message["role"] == "tool"]
        self.assertEqual([message["tool_call_id"] for message in tool_messages], ["first", "second"])
        self.assertTrue(json.loads(tool_messages[1]["content"])["cancelled"])


if __name__ == "__main__":
    unittest.main()
