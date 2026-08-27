"""The model/tool execution loop."""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .context import compact_messages
from .model import AssistantReply


class ModelClient(Protocol):
    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AssistantReply: ...


class ToolProvider(Protocol):
    @property
    def definitions(self) -> list[dict[str, Any]]: ...

    def execute(self, name: str, arguments: str) -> str: ...


@dataclass(frozen=True, slots=True)
class AgentEvent:
    kind: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AgentResult:
    success: bool
    answer: str
    rounds: int
    tool_calls: int


EventHandler = Callable[[AgentEvent], None]


def build_system_prompt(workspace: Path) -> str:
    return f"""You are TinyForge, an autonomous coding agent working in a local repository.
Your workspace root is: {workspace}
The host operating system is: {platform.system()}.

Work until the user's programming task is actually complete. Inspect relevant files before editing.
Use the provided tools for all file access and command execution. Prefer targeted edits over rewriting
whole files. Run relevant tests or checks after changes. If a tool fails, read its error and recover.
Never claim a test passed unless you ran it and saw a successful result. Do not expose credentials.
Stay inside the workspace. Avoid destructive commands and unrelated changes. When finished, respond
with a concise summary of changes and verification. If blocked, state the exact blocker and evidence.
"""


class Agent:
    def __init__(
        self,
        *,
        model: ModelClient,
        tools: ToolProvider,
        workspace: Path,
        max_rounds: int = 30,
        max_context_chars: int = 150_000,
        on_event: EventHandler | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.workspace = workspace.resolve()
        self.max_rounds = max_rounds
        self.max_context_chars = max_context_chars
        self.on_event = on_event or (lambda event: None)
        self.messages: list[dict[str, Any]] = []

    def reset(self) -> None:
        self.messages = []

    def run(self, task: str, *, continue_session: bool = False) -> AgentResult:
        if not task.strip():
            return AgentResult(False, "Task cannot be empty.", 0, 0)
        if not continue_session or not self.messages:
            self.messages = [
                {"role": "system", "content": build_system_prompt(self.workspace)},
            ]
        self.messages.append({"role": "user", "content": task.strip()})

        total_tool_calls = 0
        previous_signature: tuple[tuple[str, str], ...] | None = None
        repeated_batches = 0

        for round_number in range(1, self.max_rounds + 1):
            self.messages, removed = compact_messages(
                self.messages, self.max_context_chars
            )
            if removed:
                self._emit("context_compacted", removed=removed)

            self._emit("model_start", round=round_number)
            reply = self.model.complete(self.messages, self.tools.definitions)
            self.messages.append(reply.as_message())

            if reply.content:
                self._emit("assistant_text", text=reply.content)

            if not reply.tool_calls:
                answer = reply.content.strip() or "The model stopped without a final response."
                return AgentResult(True, answer, round_number, total_tool_calls)

            signature = tuple((call.name, call.arguments) for call in reply.tool_calls)
            repeated_batches = repeated_batches + 1 if signature == previous_signature else 1
            previous_signature = signature
            if repeated_batches >= 3:
                answer = "Stopped because the model repeated the same tool call three times."
                for call in reply.tool_calls:
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps({"ok": False, "error": answer}),
                        }
                    )
                self.messages.append({"role": "assistant", "content": answer})
                self._emit("loop_stopped", reason=answer)
                return AgentResult(False, answer, round_number, total_tool_calls)

            for call in reply.tool_calls:
                total_tool_calls += 1
                self._emit(
                    "tool_start", name=call.name, arguments=self._safe_arguments(call.arguments)
                )
                output = self.tools.execute(call.name, call.arguments)
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": output,
                    }
                )
                self._emit("tool_end", name=call.name, output=output)

        answer = f"Stopped after reaching the maximum of {self.max_rounds} model rounds."
        self._emit("loop_stopped", reason=answer)
        return AgentResult(False, answer, self.max_rounds, total_tool_calls)

    def _emit(self, kind: str, **data: Any) -> None:
        self.on_event(AgentEvent(kind=kind, data=data))

    @staticmethod
    def _safe_arguments(arguments: str) -> dict[str, Any] | str:
        try:
            value = json.loads(arguments)
            if isinstance(value, dict):
                redacted = dict(value)
                if "content" in redacted and len(str(redacted["content"])) > 300:
                    redacted["content"] = str(redacted["content"])[:300] + "..."
                return redacted
            return arguments
        except json.JSONDecodeError:
            return arguments
