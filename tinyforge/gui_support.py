"""Thread-safe, headless support code for the TinyForge desktop frontend."""

from __future__ import annotations

import difflib
import json
import queue
import re
import threading
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .agent import Agent, AgentEvent, AgentResult
from .config import Config
from .memory import MemoryStore, redact_secrets
from .runtime import build_agent
from .workspace_view import is_sensitive_workspace_path as _is_sensitive_path


MAX_UI_TEXT = 20_000
MAX_UI_EVENT_CHARS = 100_000
MAX_UI_EVENT_ITEMS = 500
MAX_UI_EVENT_DEPTH = 8
MAX_UI_KEY_CHARS = 200
MAX_DIFF_BYTES = 300_000
MAX_UI_STREAM_LINE = MAX_UI_TEXT
_TRUNCATED = "[TRUNCATED]"
_OMITTED_STREAM_LINE = "[output line omitted: exceeds display limit]\n"
_OMITTED_BUSY_OUTPUT = "[output omitted: GUI queue was busy]\n"
_REDACTION_MARKERS = ("[REDACTED_API_KEY]", "[REDACTED]")


@dataclass(frozen=True, slots=True)
class UiEnvelope:
    """One immutable worker-to-UI queue message."""

    run_id: str
    kind: str
    payload: object


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    relative_path: str
    exists: bool
    text: str | None


@dataclass(slots=True)
class _UiBudget:
    items: int = MAX_UI_EVENT_ITEMS
    chars: int = MAX_UI_EVENT_CHARS


def _redact_and_clip(value: str, limit: int = MAX_UI_TEXT) -> str:
    safe_value = redact_secrets(value)
    if len(safe_value) <= limit:
        return safe_value
    for marker in _REDACTION_MARKERS:
        start = safe_value.rfind(
            marker,
            max(0, limit - len(marker)),
            min(len(safe_value), limit + len(marker)),
        )
        if 0 <= start < limit < start + len(marker):
            return safe_value[: limit - len(marker)] + marker
    return safe_value[:limit]


def _redact_and_clip_command(value: str, limit: int = MAX_UI_TEXT) -> str:
    safe_value = redact_secrets(value)
    if len(safe_value) <= limit:
        return safe_value
    marker = "\n[command truncated]\n"
    available = max(0, limit - len(marker))
    head = available * 2 // 3
    tail = available - head
    return safe_value[:head] + marker + (safe_value[-tail:] if tail else "")


def _is_sensitive_ui_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    markers = (
        "apikey",
        "accesskey",
        "authorization",
        "cookie",
        "credential",
        "password",
        "privatekey",
        "secret",
    )
    return (
        normalized in {"auth", "key", "passphrase", "passwd"}
        or normalized.endswith(("auth", "passphrase", "passwd", "pat", "token"))
        or any(marker in normalized for marker in markers)
    )


def _budgeted_ui_text(value: str, budget: _UiBudget, limit: int = MAX_UI_TEXT) -> str:
    if budget.chars <= 0:
        return _TRUNCATED
    safe_value = _redact_and_clip(value, min(limit, budget.chars))
    budget.chars -= len(safe_value)
    return safe_value


def _safe_ui_value(
    value: Any,
    key: str = "",
    *,
    depth: int = 0,
    budget: _UiBudget | None = None,
) -> Any:
    if budget is None:
        budget = _UiBudget()
    if budget.items <= 0:
        return _TRUNCATED
    budget.items -= 1
    if _is_sensitive_ui_key(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return _budgeted_ui_text(value, budget)
    if isinstance(value, dict):
        if depth >= MAX_UI_EVENT_DEPTH:
            return _TRUNCATED
        safe_items: dict[str, Any] = {}
        for item_key, item in value.items():
            if budget.items <= 0 or budget.chars <= 0:
                safe_items[_TRUNCATED] = _TRUNCATED
                break
            raw_key = str(item_key)
            safe_key = _budgeted_ui_text(raw_key, budget, MAX_UI_KEY_CHARS)
            safe_items[safe_key] = _safe_ui_value(
                item,
                raw_key,
                depth=depth + 1,
                budget=budget,
            )
        return safe_items
    if isinstance(value, (list, tuple)):
        if depth >= MAX_UI_EVENT_DEPTH:
            return _TRUNCATED
        safe_items = []
        for item in value:
            if budget.items <= 0 or budget.chars <= 0:
                safe_items.append(_TRUNCATED)
                break
            safe_items.append(_safe_ui_value(item, depth=depth + 1, budget=budget))
        return safe_items
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _budgeted_ui_text(str(value), budget)


def sanitize_agent_event(event: AgentEvent) -> AgentEvent:
    """Create a bounded, redacted copy before an event enters the UI queue."""
    safe_data = _safe_ui_value(event.data)
    if not isinstance(safe_data, dict):
        safe_data = {"value": safe_data}
    if event.kind == "tool_start" and event.data.get("name") == "run_command":
        raw_arguments = event.data.get("arguments")
        safe_arguments = safe_data.get("arguments")
        if isinstance(raw_arguments, dict) and isinstance(safe_arguments, dict):
            command = raw_arguments.get("command")
            if isinstance(command, str):
                safe_arguments = dict(safe_arguments)
                safe_arguments["command"] = _redact_and_clip_command(command)
                safe_data["arguments"] = safe_arguments
    return AgentEvent(kind=_redact_and_clip(str(event.kind), 100), data=safe_data)


def summarize_tool_output(output: str) -> tuple[bool, str]:
    """Return a short status line without exposing raw tool output."""
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        safe_output = _redact_and_clip(output)
        return False, safe_output.strip().replace("\n", " | ")[:320] or "Invalid tool result"
    if not isinstance(payload, dict) or not payload.get("ok"):
        error = (
            payload.get("error", "Unknown tool error")
            if isinstance(payload, dict)
            else "Invalid tool result"
        )
        return False, redact_secrets(str(error)).strip().replace("\n", " | ")[:320]
    result = payload.get("result", {})
    if isinstance(result, dict):
        if "exit_code" in result:
            stdout = redact_secrets(str(result.get("stdout", ""))).strip().replace("\n", " | ")
            summary = f"exit={result['exit_code']}"
            if stdout:
                summary += f"; {stdout[:280]}"
            return result.get("exit_code") == 0, summary
        if "path" in result:
            return True, redact_secrets(str(result["path"]))[:320]
        if "matches" in result and isinstance(result["matches"], list):
            return True, f"{len(result['matches'])} matches"
    return True, "Completed"


def command_terminal_result(output: str) -> dict[str, Any]:
    """Extract a bounded, redacted command result for the read-only terminal."""
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return {"parsed": False, "error": "Command result is not valid JSON"}
    if not isinstance(payload, dict):
        return {"parsed": False, "error": "Command result is not an object"}
    if not payload.get("ok"):
        error = str(payload.get("error", "Unknown command error"))
        if error.startswith("Command timed out after ") and ". Partial stdout:" in error:
            error = error.split(". Partial stdout:", 1)[0] + "."
        terminal_error = {
            "parsed": True,
            "ok": False,
            "error": _redact_and_clip(error),
        }
        if payload.get("cancelled") is True:
            terminal_error["cancelled"] = True
        return terminal_error
    result = payload.get("result")
    if not isinstance(result, dict):
        return {"parsed": False, "error": "Command result has no structured payload"}
    budget = _UiBudget(items=20, chars=MAX_UI_TEXT * 2)
    terminal_result: dict[str, Any] = {
        "parsed": True,
        "ok": True,
        "stdout": _budgeted_ui_text(str(result.get("stdout", "")), budget),
        "stderr": _budgeted_ui_text(str(result.get("stderr", "")), budget),
        "truncated": bool(result.get("truncated", False)),
    }
    exit_code = result.get("exit_code")
    if type(exit_code) is int:
        terminal_result["exit_code"] = exit_code
    return terminal_result


def snapshot_text_file(workspace: Path, relative_path: str) -> FileSnapshot | None:
    """Read a small workspace text file for diffing, without following paths outside the root."""
    try:
        root = workspace.resolve()
        requested = Path(relative_path)
        if "\x00" in str(requested) or requested.is_absolute() or _is_sensitive_path(requested):
            return None
        candidate = (root / requested).resolve(strict=False)
        resolved_relative = candidate.relative_to(root)
        if _is_sensitive_path(resolved_relative):
            return None
        normalized = resolved_relative.as_posix()
        exists = candidate.exists()
        is_file = candidate.is_file() if exists else False
    except (OSError, ValueError):
        return None
    if not exists:
        return FileSnapshot(normalized, False, "")
    if not is_file:
        return None
    try:
        with candidate.open("rb") as handle:
            raw = handle.read(MAX_DIFF_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_DIFF_BYTES or b"\x00" in raw:
        return FileSnapshot(normalized, True, None)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    return FileSnapshot(normalized, True, text)


def unified_file_diff(before: FileSnapshot, after: FileSnapshot) -> str:
    """Create a stable unified diff for two snapshots of the same file."""
    if before.text is None or after.text is None or before.text == after.text:
        return ""
    relative_path = after.relative_path or before.relative_path
    from_name = f"a/{relative_path}" if before.exists else "/dev/null"
    to_name = f"b/{relative_path}" if after.exists else "/dev/null"
    lines = difflib.unified_diff(
        before.text.splitlines(),
        after.text.splitlines(),
        fromfile=from_name,
        tofile=to_name,
        lineterm="",
    )
    rendered = "\n".join(lines)
    return _redact_and_clip(rendered) + ("\n" if rendered else "")


class GuiEventBridge:
    """Copy Agent events into a queue and derive file diffs from edit events."""

    def __init__(self, target: queue.Queue[UiEnvelope], run_id: str, workspace: Path) -> None:
        self.target = target
        self.run_id = run_id
        self.workspace = workspace.resolve()
        self._before_files: dict[str, FileSnapshot] = {}
        self._terminal_lines: dict[tuple[str, str], str] = {}
        self._discarding_terminal_lines: set[tuple[str, str]] = set()
        self._dropped_terminal_streams: set[tuple[str, str]] = set()

    def __call__(self, event: AgentEvent) -> None:
        raw_call_id = str(event.data.get("call_id", ""))
        if event.kind == "tool_start" and event.data.get("name") == "run_command":
            self._clear_terminal_lines(raw_call_id)
        if event.kind == "tool_output" and event.data.get("name") == "run_command":
            self._queue_terminal_output(event)
            return
        if event.kind == "tool_end" and event.data.get("name") == "run_command":
            self._finish_terminal_output(raw_call_id)

        safe_event = sanitize_agent_event(event)
        if event.kind == "tool_end":
            ok, summary = summarize_tool_output(str(event.data.get("output", "")))
            safe_data = dict(safe_event.data)
            safe_data["output_ok"] = ok
            safe_data["output_summary"] = summary
            if event.data.get("name") == "run_command":
                safe_data["terminal_result"] = command_terminal_result(
                    str(event.data.get("output", ""))
                )
            safe_event = AgentEvent(kind=safe_event.kind, data=safe_data)
        call_id = str(safe_event.data.get("call_id", ""))
        if safe_event.kind == "tool_start" and safe_event.data.get("name") in {
            "edit_file",
            "write_file",
        }:
            arguments = safe_event.data.get("arguments")
            if isinstance(arguments, dict) and isinstance(arguments.get("path"), str):
                snapshot = snapshot_text_file(self.workspace, arguments["path"])
                if snapshot is not None:
                    self._before_files[call_id] = snapshot

        self.target.put(UiEnvelope(self.run_id, "event", safe_event))

        if safe_event.kind == "tool_end" and call_id in self._before_files:
            before = self._before_files.pop(call_id)
            after = snapshot_text_file(self.workspace, before.relative_path)
            if after is not None:
                diff = unified_file_diff(before, after)
                if diff:
                    self.target.put(
                        UiEnvelope(
                            self.run_id,
                            "file_diff",
                            {"path": before.relative_path, "diff": diff},
                        )
                    )

    def _queue_terminal_output(self, event: AgentEvent) -> None:
        call_id = str(event.data.get("call_id", ""))
        stream = str(event.data.get("stream", ""))
        if not call_id or stream not in {"stdout", "stderr"}:
            return
        key = (call_id, stream)
        if key in self._dropped_terminal_streams:
            return
        value = str(event.data.get("text", ""))
        if key in self._discarding_terminal_lines:
            newline = value.find("\n")
            if newline < 0:
                return
            self._discarding_terminal_lines.discard(key)
            value = value[newline + 1 :]
        buffered = self._terminal_lines.pop(key, "") + value
        parts = buffered.split("\n")
        complete_lines = [part + "\n" for part in parts[:-1]]
        remainder = "" if buffered.endswith("\n") else parts[-1]
        if remainder:
            if len(remainder) > MAX_UI_STREAM_LINE:
                complete_lines.append(_OMITTED_STREAM_LINE)
                self._discarding_terminal_lines.add(key)
            else:
                self._terminal_lines[key] = remainder

        batch: list[str] = []
        batch_chars = 0
        for line in complete_lines:
            if len(line) > MAX_UI_STREAM_LINE:
                line = _OMITTED_STREAM_LINE
            if batch and batch_chars + len(line) > MAX_UI_TEXT:
                if not self._put_terminal_output(call_id, stream, "".join(batch)):
                    self._drop_terminal_stream(key)
                    return
                batch = []
                batch_chars = 0
            batch.append(line)
            batch_chars += len(line)
        if batch and not self._put_terminal_output(call_id, stream, "".join(batch)):
            self._drop_terminal_stream(key)

    def _finish_terminal_output(self, call_id: str) -> None:
        self._flush_terminal_lines(call_id)
        dropped = sorted(key for key in self._dropped_terminal_streams if key[0] == call_id)
        for _, stream in dropped:
            self._put_terminal_output(
                call_id,
                stream,
                _OMITTED_BUSY_OUTPUT,
                block=True,
            )
        self._clear_terminal_lines(call_id)

    def _flush_terminal_lines(self, call_id: str) -> None:
        keys = [key for key in self._terminal_lines if key[0] == call_id]
        for key in keys:
            value = self._terminal_lines.pop(key)
            if (
                value
                and key not in self._dropped_terminal_streams
                and not self._put_terminal_output(key[0], key[1], value)
            ):
                self._drop_terminal_stream(key)

    def _drop_terminal_stream(self, key: tuple[str, str]) -> None:
        self._terminal_lines.pop(key, None)
        self._discarding_terminal_lines.discard(key)
        self._dropped_terminal_streams.add(key)

    def _clear_terminal_lines(self, call_id: str) -> None:
        for key in tuple(self._terminal_lines):
            if key[0] == call_id:
                self._terminal_lines.pop(key, None)
        self._discarding_terminal_lines = {
            key for key in self._discarding_terminal_lines if key[0] != call_id
        }
        self._dropped_terminal_streams = {
            key for key in self._dropped_terminal_streams if key[0] != call_id
        }

    def _put_terminal_output(
        self,
        call_id: str,
        stream: str,
        value: str,
        *,
        block: bool = False,
    ) -> bool:
        safe_event = sanitize_agent_event(
            AgentEvent(
                "tool_output",
                {
                    "call_id": call_id,
                    "name": "run_command",
                    "stream": stream,
                    "text": value,
                },
            )
        )
        envelope = UiEnvelope(self.run_id, "event", safe_event)
        if block:
            self.target.put(envelope)
            return True
        try:
            self.target.put_nowait(envelope)
        except queue.Full:
            return False
        return True


AgentBuilder = Callable[[Config, Callable[[AgentEvent], None]], Agent]


def _default_builder(config: Config, event_handler: Callable[[AgentEvent], None]) -> Agent:
    return build_agent(config, on_event=event_handler)


class AgentWorker:
    """Enforce one active Agent run and isolate worker events by run ID."""

    def __init__(
        self,
        target: queue.Queue[UiEnvelope],
        *,
        builder: AgentBuilder = _default_builder,
    ) -> None:
        self.target = target
        self.builder = builder
        self._lock = threading.Lock()
        self._agent: Agent | None = None
        self._config: Config | None = None
        self._active_run_id: str | None = None
        self._cancel_event: threading.Event | None = None
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._active_run_id is not None

    def start(self, config: Config, task: str, *, continue_session: bool) -> str | None:
        with self._lock:
            if self._active_run_id is not None:
                return None
            run_id = uuid.uuid4().hex
            bridge = GuiEventBridge(self.target, run_id, config.workspace)
            if self._agent is None or self._config != config:
                self._agent = self.builder(config, bridge)
                self._config = config
            else:
                self._agent.on_event = bridge
            cancel_event = threading.Event()
            self._active_run_id = run_id
            self._cancel_event = cancel_event
            agent = self._agent

        thread = threading.Thread(
            target=self._run,
            args=(run_id, agent, task, continue_session, cancel_event),
            name=f"tinyforge-gui-{run_id[:8]}",
            daemon=True,
        )
        with self._lock:
            self._thread = thread
        try:
            thread.start()
        except RuntimeError:
            with self._lock:
                if self._active_run_id == run_id:
                    self._active_run_id = None
                    self._cancel_event = None
                if self._thread is thread:
                    self._thread = None
            raise
        return run_id

    def _run(
        self,
        run_id: str,
        agent: Agent,
        task: str,
        continue_session: bool,
        cancel_event: threading.Event,
    ) -> None:
        terminal_kind = "result"
        try:
            terminal_payload: object = agent.run(
                task,
                continue_session=continue_session,
                cancel_event=cancel_event,
            )
            if isinstance(terminal_payload, AgentResult):
                terminal_payload = replace(
                    terminal_payload,
                    answer=_redact_and_clip(str(terminal_payload.answer)),
                )
        except Exception as exc:  # The UI must recover from model, config, and tool failures.
            terminal_kind = "error"
            terminal_payload = _redact_and_clip(str(exc), 2000) or type(exc).__name__
        self.target.put(UiEnvelope(run_id, terminal_kind, terminal_payload))

    def acknowledge_terminal(self, run_id: str) -> bool:
        """Release the single-run gate after the UI consumes a terminal envelope."""
        with self._lock:
            if self._active_run_id != run_id:
                return False
            self._active_run_id = None
            self._cancel_event = None
            thread = self._thread
            self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        return True

    def configuration_changed(self, config: Config) -> bool:
        with self._lock:
            return self._config is not None and self._config != config

    def cancel(self) -> bool:
        with self._lock:
            if self._cancel_event is None:
                return False
            self._cancel_event.set()
            return True

    def reset(self) -> bool:
        with self._lock:
            if self._active_run_id is not None:
                return False
            if self._agent is not None:
                self._agent.reset()
            return True

    @staticmethod
    def _session_store(config: Config) -> MemoryStore:
        return MemoryStore(
            config.state_dir,
            config.workspace,
            archive_sessions=config.archive_sessions,
        )

    def list_sessions(self, config: Config, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            if self._active_run_id is not None:
                return []
        return self._session_store(config).list_sessions(limit=limit)

    def current_session_id(self) -> str | None:
        with self._lock:
            if self._agent is None:
                return None
            value = getattr(self._agent, "session_id", None)
            return str(value) if value else None

    def restore_session(self, config: Config, session_id: str) -> dict[str, Any]:
        with self._lock:
            if self._active_run_id is not None:
                raise ValueError("A task is currently running")
            if self._agent is None or self._config != config:
                self._agent = self.builder(config, lambda event: None)
                self._config = config
            restorer = getattr(self._agent, "restore_session", None)
            if not callable(restorer):
                raise ValueError("This Agent runtime cannot restore conversations")
            return restorer(session_id)

    def rename_session(self, config: Config, session_id: str, title: str) -> dict[str, Any]:
        with self._lock:
            if self._active_run_id is not None:
                raise ValueError("A task is currently running")
            agent = self._agent if self._config == config else None
            renamer = getattr(agent, "rename_session", None)
            if callable(renamer):
                return renamer(session_id, title)
        return self._session_store(config).rename_session(session_id, title)

    def delete_session(self, config: Config, session_id: str) -> bool:
        with self._lock:
            if self._active_run_id is not None:
                raise ValueError("A task is currently running")
            agent = self._agent if self._config == config else None
            deleter = getattr(agent, "delete_session", None)
            if callable(deleter):
                return bool(deleter(session_id))
        return self._session_store(config).delete_session(session_id)

    def memory_overview(self, *, expected_workspace: Path | None = None) -> str:
        with self._lock:
            if self._active_run_id is not None:
                return "Memory refresh is available after the current task finishes."
            if self._agent is None:
                return "Memory has not been loaded for this workspace."
            if expected_workspace is not None and (
                self._config is None
                or self._config.workspace != expected_workspace.expanduser().resolve()
            ):
                return "Memory has not been loaded for this workspace."
            return _redact_and_clip(self._agent.memory_overview())

    def skills_overview(self, *, expected_workspace: Path | None = None) -> str:
        with self._lock:
            if self._active_run_id is not None:
                return "Skill refresh is available after the current task finishes."
            if self._agent is None:
                return "Skills have not been loaded for this workspace."
            if expected_workspace is not None and (
                self._config is None
                or self._config.workspace != expected_workspace.expanduser().resolve()
            ):
                return "Skills have not been loaded for this workspace."
            overview = getattr(self._agent, "skills_overview", None)
            if not callable(overview):
                return "Skills are unavailable for this runtime."
            return _redact_and_clip(str(overview()))

    def skills_snapshot(self, *, expected_workspace: Path | None = None) -> dict[str, Any]:
        """Return a bounded metadata-only Skill snapshot for the desktop run view."""
        empty = {
            "state": "not_loaded",
            "enabled": False,
            "available": [],
            "loaded": [],
            "invalid_entries_skipped": 0,
        }
        with self._lock:
            if self._active_run_id is not None:
                return {**empty, "state": "busy"}
            if self._agent is None:
                return empty
            if expected_workspace is not None and (
                self._config is None
                or self._config.workspace != expected_workspace.expanduser().resolve()
            ):
                return empty
            snapshot = getattr(self._agent, "skills_snapshot", None)
            if not callable(snapshot):
                return {**empty, "state": "unavailable"}
            safe_snapshot = _safe_ui_value(snapshot())
            if not isinstance(safe_snapshot, dict):
                return {**empty, "state": "unavailable"}
            return safe_snapshot
