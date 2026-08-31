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
from .memory import redact_secrets
from .runtime import build_agent


MAX_UI_TEXT = 20_000
MAX_UI_EVENT_CHARS = 100_000
MAX_UI_EVENT_ITEMS = 500
MAX_UI_EVENT_DEPTH = 8
MAX_UI_KEY_CHARS = 200
MAX_DIFF_BYTES = 300_000
_TRUNCATED = "[TRUNCATED]"
_SENSITIVE_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "id_dsa",
    "id_ed25519",
    "id_rsa",
}
_SENSITIVE_SUFFIXES = {
    ".gpg",
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
    ".pgp",
}
_SENSITIVE_DIRECTORIES = {".aws", ".azure", ".gnupg", ".ssh"}
_PRIVATE_KEY_PREFIXES = ("id_dsa.", "id_ed25519.", "id_rsa.")
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


def _is_sensitive_path(path: Path) -> bool:
    lowered_parts = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    return (
        bool(lowered_parts & (_SENSITIVE_NAMES | _SENSITIVE_DIRECTORIES))
        or name.startswith(".env.")
        or name.startswith(_PRIVATE_KEY_PREFIXES)
        or "credential" in name
        or "private_key" in name
        or "private-key" in name
        or "secret" in name
        or path.suffix.casefold() in _SENSITIVE_SUFFIXES
    )


def snapshot_text_file(workspace: Path, relative_path: str) -> FileSnapshot | None:
    """Read a small workspace text file for diffing, without following paths outside the root."""
    try:
        root = workspace.resolve()
        requested = Path(relative_path)
        if requested.is_absolute() or _is_sensitive_path(requested):
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

    def __call__(self, event: AgentEvent) -> None:
        safe_event = sanitize_agent_event(event)
        if event.kind == "tool_end":
            ok, summary = summarize_tool_output(str(event.data.get("output", "")))
            safe_data = dict(safe_event.data)
            safe_data["output_ok"] = ok
            safe_data["output_summary"] = summary
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
