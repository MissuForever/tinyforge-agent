"""The model/tool execution loop."""

from __future__ import annotations

import json
import platform
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .context import compact_messages
from .memory import redact_secrets
from .model import AssistantReply


TASK_COMPLETE_PREFIX = "TASK_COMPLETE:"
TASK_BLOCKED_PREFIX = "TASK_BLOCKED:"


class ModelClient(Protocol):
    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AssistantReply: ...


class ToolProvider(Protocol):
    @property
    def definitions(self) -> list[dict[str, Any]]: ...

    def execute(self, name: str, arguments: str) -> str: ...


class MemoryProvider(Protocol):
    def start_task(self, task: str) -> None: ...

    def reset(self) -> None: ...

    def anchor(self, turn: int) -> str: ...

    def record_tool(self, name: str, output: str) -> None: ...

    def finish(
        self,
        *,
        success: bool,
        task: str,
        answer: str,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]: ...


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
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    elapsed_ms: int = 0


EventHandler = Callable[[AgentEvent], None]


def build_system_prompt(workspace: Path, *, memory_enabled: bool = False) -> str:
    prompt = f"""You are TinyForge, an autonomous coding agent working in a local repository.
Your workspace root is: {workspace}
The host operating system is: {platform.system()}.

Work until the user's programming task is actually complete. Inspect relevant files before editing.
Use the provided tools for all file access and command execution. Prefer targeted edits over rewriting
whole files. Run relevant tests or checks after changes. If a tool fails, read its error and recover.
Never claim a test passed unless you ran it and saw a successful result. Do not expose credentials.
Stay inside the workspace. Avoid destructive commands and unrelated changes. When finished, respond
with a concise summary of changes and verification. Start a successful final response with exactly
TASK_COMPLETE:. If blocked or incomplete, start it with exactly TASK_BLOCKED: and state the blocker
and evidence. Do not use either marker in an intermediate response that contains tool calls.
"""
    if memory_enabled:
        prompt += """
The system prompt contains a compact working-memory anchor. Update it only after meaningful
milestones so the objective, constraints, verified facts, progress, and next step survive context
compression. The persistent-memory index contains pointers only; call recall_memory when an indexed
fact or SOP is relevant. Follow "No Execution, No Memory": stage only stable, reusable knowledge
supported by verified_evidence IDs from successful tool execution. Never store secrets, guesses,
temporary task state, or failed approaches. SOPs after code edits require a successful verification
command from after the latest file edit, failed command, or unverified command.
"""
    return prompt


class Agent:
    def __init__(
        self,
        *,
        model: ModelClient,
        tools: ToolProvider,
        workspace: Path,
        max_rounds: int = 30,
        max_context_chars: int = 150_000,
        max_context_tokens: int | None = None,
        on_event: EventHandler | None = None,
        memory: MemoryProvider | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.workspace = workspace.resolve()
        self.max_rounds = max_rounds
        self.max_context_chars = max_context_chars
        self.max_context_tokens = max_context_tokens
        self.on_event = on_event or (lambda event: None)
        self.memory = memory
        self.messages: list[dict[str, Any]] = []
        self._current_task = ""
        self._base_system_prompt = build_system_prompt(
            self.workspace, memory_enabled=self.memory is not None
        )

    def reset(self) -> None:
        self.messages = []
        self._current_task = ""
        if self.memory is not None:
            self.memory.reset()

    def memory_overview(self) -> str:
        if self.memory is None:
            return "Persistent memory is disabled."
        return self.memory.anchor(0)

    def run(self, task: str, *, continue_session: bool = False) -> AgentResult:
        if not task.strip():
            return AgentResult(False, "Task cannot be empty.", 0, 0)
        self._current_task = task.strip()
        if self.memory is not None:
            self.memory.start_task(self._current_task)
        if not continue_session or not self.messages:
            self.messages = [
                {"role": "system", "content": self._base_system_prompt},
            ]
        self.messages.append({"role": "user", "content": self._current_task})

        started = time.monotonic()
        total_tool_calls = 0
        input_tokens = 0
        output_tokens = 0
        cached_input_tokens = 0
        previous_signature: tuple[tuple[str, str], ...] | None = None
        repeated_batches = 0
        completion_repair_attempted = False

        for round_number in range(1, self.max_rounds + 1):
            self._refresh_system_prompt(round_number)
            self.messages, removed = compact_messages(
                self.messages,
                self.max_context_chars,
                max_tokens=self.max_context_tokens,
                tool_schema=self.tools.definitions,
            )
            if removed:
                self._emit("context_compacted", removed=removed)

            self._emit("model_start", round=round_number)
            reply = self.model.complete(self.messages, self.tools.definitions)
            input_tokens += reply.usage.input_tokens
            output_tokens += reply.usage.output_tokens
            cached_input_tokens += reply.usage.cached_input_tokens
            self.messages.append(reply.as_message())

            if reply.content and reply.tool_calls:
                self._emit("assistant_text", text=redact_secrets(reply.content))

            if not reply.tool_calls:
                if (
                    not self._has_completion_status(reply.content)
                    and not completion_repair_attempted
                    and round_number < self.max_rounds
                ):
                    completion_repair_attempted = True
                    correction = (
                        "Protocol correction: your previous response omitted the required task "
                        "status. If the task is complete, repeat the final answer starting exactly "
                        "with TASK_COMPLETE:. If it is blocked or incomplete, repeat it starting "
                        "exactly with TASK_BLOCKED:. Do not omit the marker."
                    )
                    self.messages.append({"role": "user", "content": correction})
                    self._emit("completion_repair", round=round_number)
                    continue
                success, answer = self._parse_completion(reply.content)
                return self._finish(
                    AgentResult(
                        success,
                        answer,
                        round_number,
                        total_tool_calls,
                        input_tokens,
                        output_tokens,
                        cached_input_tokens,
                        round((time.monotonic() - started) * 1000),
                    )
                )

            signature = tuple(
                (call.name, self._canonical_arguments(call.arguments))
                for call in reply.tool_calls
            )
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
                return self._finish(
                    AgentResult(
                        False,
                        answer,
                        round_number,
                        total_tool_calls,
                        input_tokens,
                        output_tokens,
                        cached_input_tokens,
                        round((time.monotonic() - started) * 1000),
                    )
                )

            for call in reply.tool_calls:
                total_tool_calls += 1
                self._emit(
                    "tool_start", name=call.name, arguments=self._safe_arguments(call.arguments)
                )
                output = self.tools.execute(call.name, call.arguments)
                if self.memory is not None:
                    self.memory.record_tool(call.name, output)
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
        return self._finish(
            AgentResult(
                False,
                answer,
                self.max_rounds,
                total_tool_calls,
                input_tokens,
                output_tokens,
                cached_input_tokens,
                round((time.monotonic() - started) * 1000),
            )
        )

    def _refresh_system_prompt(self, turn: int) -> None:
        content = self._base_system_prompt
        if self.memory is not None:
            content += "\n" + self.memory.anchor(turn)
        if self.messages:
            self.messages[0] = {"role": "system", "content": content}

    def _finish(self, result: AgentResult) -> AgentResult:
        if self.memory is None:
            return result
        try:
            committed = self.memory.finish(
                success=result.success,
                task=self._current_task,
                answer=result.answer,
                messages=self.messages,
            )
            if committed:
                self._emit("memory_committed", count=len(committed), entries=committed)
        except (OSError, TypeError, UnicodeError, ValueError) as exc:
            self._emit("memory_error", error=str(exc))
        return result

    def _emit(self, kind: str, **data: Any) -> None:
        self.on_event(AgentEvent(kind=kind, data=data))

    @staticmethod
    def _safe_arguments(arguments: str) -> dict[str, Any] | str:
        try:
            value = json.loads(arguments)
            if isinstance(value, dict):
                redacted = Agent._redact_log_value(value)
                if "content" in redacted and len(str(redacted["content"])) > 300:
                    redacted["content"] = str(redacted["content"])[:300] + "..."
                return redacted
            return redact_secrets(arguments)
        except json.JSONDecodeError:
            return redact_secrets(arguments)

    @staticmethod
    def _redact_log_value(value: Any, key: str = "") -> Any:
        sensitive = ("api_key", "apikey", "token", "password", "secret", "authorization")
        if any(marker in key.casefold().replace("-", "_") for marker in sensitive):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {
                str(item_key): Agent._redact_log_value(item, str(item_key))
                for item_key, item in value.items()
            }
        if isinstance(value, list):
            return [Agent._redact_log_value(item) for item in value]
        if isinstance(value, str):
            return redact_secrets(value)
        return value

    @staticmethod
    def _has_completion_status(content: str) -> bool:
        answer = content.strip()
        return answer.startswith((TASK_COMPLETE_PREFIX, TASK_BLOCKED_PREFIX))

    @staticmethod
    def _parse_completion(content: str) -> tuple[bool, str]:
        answer = redact_secrets(content.strip())
        if answer.startswith(TASK_COMPLETE_PREFIX):
            return True, answer[len(TASK_COMPLETE_PREFIX) :].strip() or "Task completed."
        if answer.startswith(TASK_BLOCKED_PREFIX):
            return False, answer[len(TASK_BLOCKED_PREFIX) :].strip() or "Task is blocked."
        if not answer:
            return False, "The model stopped without a final response."
        return (
            False,
            "The model stopped without the required TASK_COMPLETE or TASK_BLOCKED status. "
            f"Last response: {answer}",
        )

    @staticmethod
    def _canonical_arguments(arguments: str) -> str:
        try:
            return json.dumps(
                json.loads(arguments), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except json.JSONDecodeError:
            return arguments.strip()
