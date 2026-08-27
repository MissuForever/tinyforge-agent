from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from tinyforge.agent import Agent, AgentEvent
from tinyforge.model import AssistantReply, ToolCall
from tinyforge.tools import WorkspaceTools


class ScriptedModel:
    def __init__(self, replies: list[AssistantReply]) -> None:
        self.replies = replies
        self.calls: list[list[dict[str, object]]] = []

    def complete(self, messages, tools):
        self.calls.append(list(messages))
        return self.replies.pop(0)


class AgentTests(unittest.TestCase):
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
                    AssistantReply("Created answer.txt and verified its content."),
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
                    AssistantReply("Fixed calculator.py; the complete test suite passes."),
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


if __name__ == "__main__":
    unittest.main()
