"""Local tools exposed to the model."""

from __future__ import annotations

import base64
import codecs
import fnmatch
import inspect
import json
import os
import re
import select
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ToolProgressHandler = Callable[[str, str], None]


_COMMAND_SEPARATORS = {";", "|", "||", "&&"}
_REDIRECT_OPERATORS = {">", ">>"}
_EXECUTABLE_SUFFIXES = (".exe", ".com", ".cmd", ".bat")


def _shell_tokens(command: str) -> list[str]:
    """Tokenize common sh and PowerShell syntax for conservative policy checks."""
    lexer = shlex.shlex(command, posix=False, punctuation_chars=";&|<>\r\n")
    lexer.commenters = ""
    lexer.whitespace = " \t"
    lexer.whitespace_split = True
    try:
        raw_tokens = list(lexer)
    except ValueError:
        return [command]
    tokens = []
    for token in raw_tokens:
        if token and all(character in "\r\n" for character in token):
            tokens.append(";")
        elif len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
            tokens.append(token[1:-1])
        else:
            tokens.append(token)
    return tokens


def _command_segments(command: str) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in _shell_tokens(command):
        separator = token in _COMMAND_SEPARATORS or token == "&" and bool(current)
        if separator:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _command_name(token: str) -> str:
    name = token.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    for suffix in _EXECUTABLE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _executable_and_arguments(segment: list[str]) -> tuple[str, list[str]]:
    index = 0
    while index < len(segment) and segment[index] == "&":
        index += 1
    while index < len(segment) and re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*=.*", segment[index]
    ):
        index += 1
    if index >= len(segment):
        return "", []

    name = _command_name(segment[index])
    if name == "env":
        index += 1
        while index < len(segment) and (
            segment[index].startswith("-")
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", segment[index])
        ):
            index += 1
        if index >= len(segment):
            return "", []
        name = _command_name(segment[index])
    elif name in {"command", "builtin", "nohup"}:
        index += 1
        while index < len(segment) and segment[index].startswith("-"):
            index += 1
        if index >= len(segment):
            return "", []
        name = _command_name(segment[index])
    return name, segment[index + 1 :]


def _critical_system_target(value: str) -> bool:
    target = value.strip().strip(",").casefold().replace("\\", "/")
    if target.startswith("file://"):
        target = target[7:]
    if target.startswith("//?/"):
        target = target[4:]
    windows_environment_roots = (
        "%systemroot%",
        "%windir%",
        "$env:systemroot",
        "${env:systemroot}",
        "$env:windir",
        "${env:windir}",
    )
    if target.startswith(windows_environment_roots):
        return True
    if target.startswith(("//./physicaldrive", "//?/physicaldrive")):
        return True
    if re.match(r"^[a-z]:(?:/)?(?:$|[*?])", target):
        return True
    if re.match(
        r"^[a-z]:/(?:windows|program files(?: \(x86\))?|programdata)"
        r"(?:/|$|[*?\[])",
        target,
    ):
        return True
    if re.match(
        r"^/(?:windows|program files(?: \(x86\))?|programdata)(?:/|$|[*?\[])",
        target,
    ):
        return True
    if target in {"/", "/*", "/.*"}:
        return True
    if re.match(
        r"^/(?:bin|boot|dev|etc|lib(?:32|64)?|proc|root|sbin|sys|usr)"
        r"(?:/|$|[*?\[])",
        target,
    ):
        return True
    return bool(
        re.match(
            r"^/(?:system|library|applications|private/etc|private/var/db|"
            r"var/(?:lib|log|spool))(?:/|$|[*?\[])",
            target,
        )
    )


def _redirect_targets(segment: list[str]) -> list[str]:
    return [
        segment[index + 1]
        for index, token in enumerate(segment[:-1])
        if token in _REDIRECT_OPERATORS
    ]


def _mutation_targets(name: str, arguments: list[str]) -> list[str]:
    target_options = {
        "-destination",
        "-filepath",
        "-literalpath",
        "-path",
    }
    value_options = {
        "-credential",
        "-encoding",
        "-exclude",
        "-filter",
        "-include",
        "-stream",
        "-value",
    }
    named_targets: list[str] = []
    positional: list[str] = []
    skip_value = False
    for index, argument in enumerate(arguments):
        lowered = argument.casefold()
        if skip_value:
            skip_value = False
            continue
        if lowered in target_options and index + 1 < len(arguments):
            named_targets.append(arguments[index + 1])
            skip_value = True
            continue
        option, separator, _ = lowered.partition(":")
        if separator and option in target_options:
            named_targets.append(argument.split(":", 1)[1])
            continue
        if lowered in value_options:
            skip_value = True
            continue
        if lowered.startswith("of=") and name == "dd":
            named_targets.append(argument[3:])
            continue
        if argument in _REDIRECT_OPERATORS or argument.startswith(("-", "/")):
            if not _critical_system_target(argument):
                continue
        positional.append(argument)

    if named_targets:
        return named_targets
    if name in {"set-content", "add-content", "out-file", "new-item", "ac", "ni"}:
        return positional[:1]
    if name in {"copy-item", "cpi", "cp", "copy", "install"}:
        return positional[-1:]
    return positional


def _git_subcommand(arguments: list[str]) -> tuple[str, list[str]]:
    index = 0
    options_with_values = {"-c", "-C", "--git-dir", "--work-tree", "--namespace"}
    while index < len(arguments):
        argument = arguments[index]
        if argument in options_with_values:
            index += 2
            continue
        if argument.startswith("-"):
            index += 1
            continue
        return argument.casefold(), arguments[index + 1 :]
    return "", []


def _nested_shell_reason(name: str, arguments: list[str], depth: int) -> str | None:
    shell_options = {
        "bash": {"-c", "-lc"},
        "cmd": {"/c", "/k"},
        "dash": {"-c"},
        "eval": set(),
        "iex": set(),
        "invoke-expression": set(),
        "ksh": {"-c"},
        "powershell": {"-c", "-command"},
        "pwsh": {"-c", "-command"},
        "sh": {"-c", "-lc"},
        "zsh": {"-c"},
    }
    if name not in shell_options or depth >= 2:
        return None
    accepted = shell_options[name]
    if not accepted:
        nested = " ".join(arguments)
    else:
        option_index = next(
            (
                index
                for index, argument in enumerate(arguments)
                if argument.casefold() in accepted
            ),
            -1,
        )
        if option_index < 0:
            return None
        nested = " ".join(arguments[option_index + 1 :])
    return _dangerous_command_reason(nested, depth + 1) if nested else None


def _dangerous_command_reason(command: str, depth: int = 0) -> str | None:
    # This is a default guardrail, not a shell sandbox or complete command interpreter.
    mutation_commands = {
        "ac",
        "add-content",
        "chgrp",
        "chmod",
        "chown",
        "clear-content",
        "clc",
        "copy",
        "copy-item",
        "cp",
        "cpi",
        "dd",
        "del",
        "erase",
        "format",
        "install",
        "mkfs",
        "move",
        "move-item",
        "mv",
        "new-item",
        "ni",
        "out-file",
        "rd",
        "remove-item",
        "ri",
        "rm",
        "rmdir",
        "set-content",
        "shred",
        "tee",
        "touch",
        "truncate",
        "unlink",
    }
    for segment in _command_segments(command):
        name, arguments = _executable_and_arguments(segment)
        if not name:
            continue
        if name in {"sudo", "su"}:
            return "privilege escalation"

        nested_reason = _nested_shell_reason(name, arguments, depth)
        if nested_reason:
            return nested_reason

        lowered_arguments = [argument.casefold() for argument in arguments]
        if name == "git":
            subcommand, subcommand_arguments = _git_subcommand(arguments)
            lowered_subcommand_arguments = [
                argument.casefold() for argument in subcommand_arguments
            ]
            if subcommand == "reset" and "--hard" in lowered_subcommand_arguments:
                return "git history/worktree destruction"
            if subcommand == "clean":
                return "untracked-file deletion"
            if subcommand == "push" and any(
                argument == "-f" or argument.startswith("--force")
                for argument in lowered_subcommand_arguments
            ):
                return "forced remote history rewrite"
        if name == "rm":
            short_flags = "".join(
                argument[1:]
                for argument in lowered_arguments
                if argument.startswith("-") and not argument.startswith("--")
            )
            recursive = "r" in short_flags or "--recursive" in lowered_arguments
            forced = "f" in short_flags or "--force" in lowered_arguments
            if recursive and forced:
                return "recursive forced deletion"
        if name in {"rmdir", "rd"} and "/s" in lowered_arguments:
            return "recursive deletion"
        if name in {"del", "erase"} and any(
            argument in {"/s", "/q"} for argument in lowered_arguments
        ):
            return "bulk deletion"
        if name in {"remove-item", "ri"} and any(
            argument.startswith("-recurse") for argument in lowered_arguments
        ):
            return "recursive deletion"
        if name == "format" and any(re.fullmatch(r"[a-z]:", arg) for arg in arguments):
            return "disk formatting"
        if name in {"shutdown", "reboot", "restart-computer"}:
            return "machine shutdown/restart"
        if name == "diskpart":
            return "disk partition modification"

        if any(_critical_system_target(target) for target in _redirect_targets(segment)):
            return "system-critical path modification"
        if name in mutation_commands and any(
            _critical_system_target(target) for target in _mutation_targets(name, arguments)
        ):
            return "system-critical path modification"
    return None


class ToolError(RuntimeError):
    """A recoverable tool error that should be returned to the model."""


class _ToolCancelled(ToolError):
    """A tool stopped through the cooperative cancellation channel."""


def _accepts_cancel_event(callback: Callable[..., Any]) -> bool:
    try:
        parameters = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == "cancel_event"
            and parameter.kind is not inspect.Parameter.POSITIONAL_ONLY
        )
        for parameter in parameters
    )


class _BoundedOutput:
    """Keep a command stream's diagnostic head and tail without unbounded memory."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self.head_limit = self.limit * 2 // 3
        self.head: list[str] = []
        self.head_chars = 0
        self.tail = ""
        self.total = 0

    def append(self, value: str) -> None:
        if not value:
            return
        self.total += len(value)
        remaining = value
        if self.head_chars < self.head_limit:
            take = min(self.head_limit - self.head_chars, len(remaining))
            self.head.append(remaining[:take])
            self.head_chars += take
            remaining = remaining[take:]
        if remaining:
            self.tail = (self.tail + remaining)[-self.limit :]

    def render(self) -> str:
        head = "".join(self.head)
        if self.total <= self.limit:
            return head + self.tail
        marker = f"\n... output truncated ({self.total - self.limit} characters omitted) ...\n"
        tail_size = self.limit - len(head) - len(marker)
        suffix = self.tail[-tail_size:] if tail_size > 0 else ""
        return (head + marker + suffix)[: self.limit]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., dict[str, Any]]

    @property
    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class CompositeTools:
    """Expose several small tool providers through one Agent tool interface."""

    def __init__(self, *providers: Any) -> None:
        self.providers = providers
        self._routes: dict[str, Any] = {}
        for provider in providers:
            for definition in provider.definitions:
                name = str(definition["function"]["name"])
                if name in self._routes:
                    raise ValueError(f"Duplicate tool name: {name}")
                self._routes[name] = provider

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return [definition for provider in self.providers for definition in provider.definitions]

    def execute(self, name: str, arguments: str) -> str:
        provider = self._routes.get(name)
        if provider is None:
            return json.dumps({"ok": False, "error": f"Unknown tool: {name}"})
        return str(provider.execute(name, arguments))

    def execute_with_progress(
        self,
        name: str,
        arguments: str,
        on_progress: ToolProgressHandler,
        *,
        cancel_event: threading.Event | None = None,
    ) -> str:
        provider = self._routes.get(name)
        if provider is None:
            return json.dumps({"ok": False, "error": f"Unknown tool: {name}"})
        execute = getattr(provider, "execute_with_progress", None)
        if callable(execute):
            if cancel_event is not None and _accepts_cancel_event(execute):
                return str(
                    execute(
                        name,
                        arguments,
                        on_progress,
                        cancel_event=cancel_event,
                    )
                )
            return str(execute(name, arguments, on_progress))
        return str(provider.execute(name, arguments))


class WorkspaceTools:
    def __init__(
        self,
        root: Path,
        *,
        command_timeout: int = 60,
        max_output: int = 30_000,
        allow_dangerous: bool = False,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.command_timeout = command_timeout
        self.max_output = max_output
        self.allow_dangerous = allow_dangerous
        self._tools = self._build_tools()

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return [tool.definition for tool in self._tools.values()]

    def execute(self, name: str, arguments: str) -> str:
        return self._execute(name, arguments, on_progress=None, cancel_event=None)

    def execute_with_progress(
        self,
        name: str,
        arguments: str,
        on_progress: ToolProgressHandler,
        *,
        cancel_event: threading.Event | None = None,
    ) -> str:
        return self._execute(
            name,
            arguments,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

    def _execute(
        self,
        name: str,
        arguments: str,
        *,
        on_progress: ToolProgressHandler | None,
        cancel_event: threading.Event | None,
    ) -> str:
        started = time.monotonic()
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise _ToolCancelled("Tool execution cancelled by user")
            tool = self._tools.get(name)
            if tool is None:
                raise ToolError(f"Unknown tool: {name}")
            try:
                parsed = json.loads(arguments or "{}")
            except json.JSONDecodeError as exc:
                raise ToolError(f"Arguments are not valid JSON: {exc.msg}") from exc
            if not isinstance(parsed, dict):
                raise ToolError("Tool arguments must be a JSON object")
            if name == "run_command":
                result = self.run_command(
                    **parsed,
                    _on_output=on_progress,
                    _cancel_event=cancel_event,
                )
            else:
                result = tool.handler(**parsed)
            payload = {
                "ok": True,
                "result": result,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        except ToolError as exc:
            payload = {
                "ok": False,
                "error": str(exc),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
            if isinstance(exc, _ToolCancelled):
                payload["cancelled"] = True
        except TypeError as exc:
            payload = {
                "ok": False,
                "error": f"Invalid arguments for {name}: {exc}",
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        except Exception as exc:  # Keep one bad tool call from terminating the agent loop.
            payload = {
                "ok": False,
                "error": f"Unexpected {type(exc).__name__}: {exc}",
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        return self._serialize_payload(payload)

    def _resolve(self, raw_path: str, *, must_exist: bool = False) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ToolError("path must be a non-empty string")
        path = Path(raw_path).expanduser()
        candidate = (path if path.is_absolute() else self.root / path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ToolError(f"Path escapes the workspace: {raw_path}")
        if must_exist and not candidate.exists():
            raise ToolError(f"Path does not exist: {raw_path}")
        return candidate

    def _relative(self, path: Path) -> str:
        relative = path.relative_to(self.root)
        return "." if not relative.parts else relative.as_posix()

    def _truncate(self, value: str) -> str:
        if len(value) <= self.max_output:
            return value
        marker = f"\n... output truncated ({len(value) - self.max_output} characters omitted) ...\n"
        head_size = self.max_output * 2 // 3
        tail_size = self.max_output - head_size - len(marker)
        suffix = value[-tail_size:] if tail_size > 0 else ""
        return (value[:head_size] + marker + suffix)[: self.max_output]

    def _serialize_payload(self, payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded) <= self.max_output:
            return encoded

        # Preserve metadata consumed by the agent even when large text fields flood the response.
        result = payload.get("result")
        compact_result: dict[str, Any] = {"truncated": True}
        string_limit = max(40, min(240, self.max_output // 6))
        if isinstance(result, dict):
            metadata_keys = (
                "command",
                "cwd",
                "exit_code",
                "path",
                "start_line",
                "end_line",
                "total_lines",
                "replacements",
                "characters_written",
                "lines",
                "skipped_files",
            )
            for key in metadata_keys:
                value = result.get(key)
                if isinstance(value, str):
                    if len(value) <= string_limit:
                        compact_result[key] = value
                    else:
                        compact_result[key] = value[: string_limit - 3] + "..."
                        if key == "command":
                            compact_result["command_truncated"] = True
                elif value is None or isinstance(value, (bool, int, float)):
                    if key in result:
                        compact_result[key] = value

        compact = {
            "ok": payload.get("ok", False),
            "result": compact_result,
            "elapsed_ms": payload.get("elapsed_ms", 0),
        }
        if "error" in payload:
            compact["error"] = str(payload["error"])[:string_limit]

        # Use only the space left after metadata for a diagnostic head/tail preview.
        base = json.dumps(compact, ensure_ascii=False)
        preview_budget = max(0, self.max_output - len(base) - 30)
        if preview_budget:
            preview = encoded[: preview_budget * 2 // 3] + encoded[-preview_budget // 3 :]
            compact_result["preview"] = preview
            compact_encoded = json.dumps(compact, ensure_ascii=False)
            while len(compact_encoded) > self.max_output and preview:
                preview = preview[: -max(1, len(compact_encoded) - self.max_output)]
                compact_result["preview"] = preview
                compact_encoded = json.dumps(compact, ensure_ascii=False)
            if not preview:
                compact_result.pop("preview", None)
                compact_encoded = json.dumps(compact, ensure_ascii=False)
            return compact_encoded
        return base

    def _build_tools(self) -> dict[str, Tool]:
        object_schema = {"type": "object", "additionalProperties": False}
        tools = [
            Tool(
                "list_files",
                "List files and directories under a workspace path. Use this to inspect project structure.",
                {
                    **object_schema,
                    "properties": {
                        "path": {"type": "string", "description": "Workspace-relative directory, default '.'"},
                        "max_depth": {"type": "integer", "minimum": 1, "maximum": 8},
                        "max_entries": {"type": "integer", "minimum": 1, "maximum": 1000},
                    },
                },
                self.list_files,
            ),
            Tool(
                "read_file",
                "Read a UTF-8 text file with line numbers. Use start_line/end_line for large files.",
                {
                    **object_schema,
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                    },
                    "required": ["path"],
                },
                self.read_file,
            ),
            Tool(
                "search_files",
                "Search text across workspace files and return matching lines. Binary and very large files are skipped.",
                {
                    **object_schema,
                    "properties": {
                        "query": {"type": "string"},
                        "path": {"type": "string"},
                        "file_glob": {"type": "string", "description": "Optional glob such as '*.py'"},
                        "regex": {"type": "boolean"},
                        "case_sensitive": {"type": "boolean"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
                    },
                    "required": ["query"],
                },
                self.search_files,
            ),
            Tool(
                "write_file",
                "Create or completely overwrite a UTF-8 text file inside the workspace.",
                {
                    **object_schema,
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "create_parents": {"type": "boolean"},
                    },
                    "required": ["path", "content"],
                },
                self.write_file,
            ),
            Tool(
                "edit_file",
                "Replace exact text in an existing UTF-8 file. By default old_text must occur exactly once.",
                {
                    **object_schema,
                    "properties": {
                        "path": {"type": "string"},
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["path", "old_text", "new_text"],
                },
                self.edit_file,
            ),
            Tool(
                "run_command",
                "Run a shell command in the workspace and return exit code, stdout and stderr. Commands time out.",
                {
                    **object_schema,
                    "properties": {
                        "command": {"type": "string"},
                        "cwd": {"type": "string", "description": "Workspace-relative directory, default '.'"},
                        "timeout": {"type": "integer", "minimum": 1, "maximum": 600},
                    },
                    "required": ["command"],
                },
                self.run_command,
            ),
        ]
        return {tool.name: tool for tool in tools}

    def list_files(
        self, path: str = ".", max_depth: int = 3, max_entries: int = 500
    ) -> dict[str, Any]:
        directory = self._resolve(path, must_exist=True)
        if not directory.is_dir():
            raise ToolError(f"Not a directory: {path}")
        max_depth = min(max(int(max_depth), 1), 8)
        max_entries = min(max(int(max_entries), 1), 1000)
        entries: list[str] = []

        def visit(current: Path, depth: int) -> None:
            if depth > max_depth or len(entries) >= max_entries:
                return
            try:
                children = sorted(
                    current.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())
                )
            except OSError as exc:
                raise ToolError(f"Cannot list {self._relative(current)}: {exc}") from exc
            for child in children:
                if child.name in {".git", "__pycache__", ".venv", "node_modules"}:
                    continue
                if len(entries) >= max_entries:
                    break
                try:
                    is_dir = child.is_dir()
                except OSError:
                    is_dir = False
                entries.append(self._relative(child) + ("/" if is_dir else ""))
                if is_dir:
                    visit(child, depth + 1)

        visit(directory, 1)
        return {
            "path": self._relative(directory),
            "entries": entries,
            "truncated": len(entries) >= max_entries,
        }

    def read_file(
        self, path: str, start_line: int = 1, end_line: int | None = None
    ) -> dict[str, Any]:
        file_path = self._resolve(path, must_exist=True)
        if not file_path.is_file():
            raise ToolError(f"Not a file: {path}")
        if file_path.stat().st_size > 5_000_000:
            raise ToolError("File is larger than the 5 MB read limit")
        raw = file_path.read_bytes()
        if b"\x00" in raw[:8192]:
            raise ToolError("File appears to be binary")
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        start = max(int(start_line), 1)
        requested_end = int(end_line) if end_line is not None else start + 499
        end = min(max(requested_end, start), start + 999, len(lines))
        selected = "\n".join(f"{number:>5} | {lines[number - 1]}" for number in range(start, end + 1))
        return {
            "path": self._relative(file_path),
            "start_line": start,
            "end_line": end,
            "total_lines": len(lines),
            "content": selected,
        }

    def search_files(
        self,
        query: str,
        path: str = ".",
        file_glob: str = "*",
        regex: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> dict[str, Any]:
        if not query:
            raise ToolError("query cannot be empty")
        start = self._resolve(path, must_exist=True)
        max_results = min(max(int(max_results), 1), 500)
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(query if regex else re.escape(query), flags)
        except re.error as exc:
            raise ToolError(f"Invalid regular expression: {exc}") from exc

        candidates = [start] if start.is_file() else start.rglob("*")
        results: list[dict[str, Any]] = []
        skipped = 0
        for candidate in candidates:
            if len(results) >= max_results:
                break
            if not candidate.is_file() or ".git" in candidate.parts:
                continue
            if not fnmatch.fnmatch(candidate.name, file_glob):
                continue
            try:
                if candidate.stat().st_size > 2_000_000:
                    skipped += 1
                    continue
                raw = candidate.read_bytes()
                if b"\x00" in raw[:8192]:
                    skipped += 1
                    continue
                for line_number, line in enumerate(
                    raw.decode("utf-8", errors="replace").splitlines(), 1
                ):
                    if pattern.search(line):
                        results.append(
                            {
                                "path": self._relative(candidate),
                                "line": line_number,
                                "text": line[:500],
                            }
                        )
                        if len(results) >= max_results:
                            break
            except OSError:
                skipped += 1
        return {
            "matches": results,
            "truncated": len(results) >= max_results,
            "skipped_files": skipped,
        }

    def write_file(
        self, path: str, content: str, create_parents: bool = True
    ) -> dict[str, Any]:
        file_path = self._resolve(path)
        if file_path.exists() and file_path.is_dir():
            raise ToolError(f"Cannot overwrite a directory: {path}")
        if create_parents:
            file_path.parent.mkdir(parents=True, exist_ok=True)
        elif not file_path.parent.is_dir():
            raise ToolError(f"Parent directory does not exist: {self._relative(file_path.parent)}")
        file_path.write_text(content, encoding="utf-8", newline="")
        return {
            "path": self._relative(file_path),
            "characters_written": len(content),
            "lines": len(content.splitlines()),
        }

    def edit_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
    ) -> dict[str, Any]:
        if not old_text:
            raise ToolError("old_text cannot be empty")
        file_path = self._resolve(path, must_exist=True)
        if not file_path.is_file():
            raise ToolError(f"Not a file: {path}")
        text = file_path.read_text(encoding="utf-8")
        occurrences = text.count(old_text)
        if occurrences == 0:
            raise ToolError("old_text was not found; read the file again before retrying")
        if not replace_all and occurrences != 1:
            raise ToolError(
                f"old_text occurs {occurrences} times; provide more context or set replace_all=true"
            )
        updated = text.replace(old_text, new_text, -1 if replace_all else 1)
        file_path.write_text(updated, encoding="utf-8", newline="")
        return {
            "path": self._relative(file_path),
            "replacements": occurrences if replace_all else 1,
        }

    def run_command(
        self,
        command: str,
        cwd: str = ".",
        timeout: int | None = None,
        *,
        _on_output: ToolProgressHandler | None = None,
        _cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command must be a non-empty string")
        if _cancel_event is not None and _cancel_event.is_set():
            raise _ToolCancelled("Command cancelled by user")
        directory = self._resolve(cwd, must_exist=True)
        if not directory.is_dir():
            raise ToolError(f"cwd is not a directory: {cwd}")
        if not self.allow_dangerous:
            reason = self._danger_reason(command)
            if reason:
                raise ToolError(
                    f"Command blocked by safety policy ({reason}). The user may rerun TinyForge "
                    "with --allow-dangerous after reviewing the command."
                )

        limit = min(max(int(timeout or self.command_timeout), 1), 600)
        if os.name == "nt":
            encoded_command = base64.b64encode(command.encode("utf-8")).decode("ascii")
            powershell_wrapper = (
                "[Console]::InputEncoding = [Text.UTF8Encoding]::new($false); "
                "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
                "$OutputEncoding = [Console]::OutputEncoding; "
                "$global:LASTEXITCODE = 0; "
                "$tinyforgeCommand = [Text.Encoding]::UTF8.GetString("
                f"[Convert]::FromBase64String('{encoded_command}')); "
                "Invoke-Expression $tinyforgeCommand; "
                "$tinyforgeSucceeded = $?; $tinyforgeExitCode = $LASTEXITCODE; "
                "if (-not $tinyforgeSucceeded -or $tinyforgeExitCode -ne 0) { exit 1 }"
            )
            invocation = [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                powershell_wrapper,
            ]
            suspended_flag = getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
            creation_flags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | suspended_flag
            )
        else:
            invocation = ["/bin/sh", "-lc", command]
            creation_flags = 0
            suspended_flag = 0
        environment = os.environ.copy()
        # Provider credentials belong to the Agent process, not commands proposed by the model.
        environment.pop("OPENAI_API_KEY", None)
        environment.pop("TINYFORGE_API_KEY", None)
        environment.setdefault("PYTHONIOENCODING", "utf-8")
        process = subprocess.Popen(
            invocation,
            cwd=directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=environment,
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
        )
        windows_job = self._create_windows_job(process)
        if os.name == "nt" and windows_job is None:
            try:
                process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self._close_process_pipes(process)
            raise ToolError("Command process could not be assigned to a Windows Job Object")
        if suspended_flag and not self._resume_windows_process(process):
            self._terminate_process_tree(process, windows_job)
            self._close_process_pipes(process)
            self._close_windows_job(windows_job)
            raise ToolError("Command process could not be resumed after safe startup")
        stdout_output = _BoundedOutput(self.max_output)
        stderr_output = _BoundedOutput(self.max_output)
        reader_errors: list[BaseException] = []
        progress_lock = threading.Lock()
        progress_stopped = threading.Event()
        reader_shutdown = threading.Event()

        def read_stream(stream: Any, name: str, output: _BoundedOutput) -> None:
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            pending_carriage_return = False

            def normalize_newlines(value: str, *, final: bool = False) -> str:
                nonlocal pending_carriage_return
                if pending_carriage_return:
                    value = "\r" + value
                    pending_carriage_return = False
                if not final and value.endswith("\r"):
                    value = value[:-1]
                    pending_carriage_return = True
                return value.replace("\r\n", "\n").replace("\r", "\n")

            try:
                read_chunk = getattr(stream, "read1", stream.read)
                while not reader_shutdown.is_set():
                    if os.name != "nt":
                        readable, _, _ = select.select((stream,), (), (), 0.1)
                        if not readable:
                            continue
                    chunk = read_chunk(4096)
                    if not chunk:
                        break
                    text = normalize_newlines(decoder.decode(chunk))
                    output.append(text)
                    if _on_output is not None and text and not progress_stopped.is_set():
                        try:
                            with progress_lock:
                                if not progress_stopped.is_set():
                                    _on_output(name, text)
                        except Exception:
                            pass
                final_text = normalize_newlines(decoder.decode(b"", final=True), final=True)
                output.append(final_text)
                if _on_output is not None and final_text and not progress_stopped.is_set():
                    try:
                        with progress_lock:
                            if not progress_stopped.is_set():
                                _on_output(name, final_text)
                    except Exception:
                        pass
            except (OSError, ValueError) as exc:
                reader_errors.append(exc)
            finally:
                stream.close()

        assert process.stdout is not None
        assert process.stderr is not None
        readers = (
            threading.Thread(
                target=read_stream,
                args=(process.stdout, "stdout", stdout_output),
                name="tinyforge-command-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=read_stream,
                args=(process.stderr, "stderr", stderr_output),
                name="tinyforge-command-stderr",
                daemon=True,
            ),
        )
        started_readers: list[threading.Thread] = []
        timed_out = False
        cancelled = False
        try:
            for reader in readers:
                reader.start()
                started_readers.append(reader)
            deadline = time.monotonic() + limit
            while True:
                if _cancel_event is not None and _cancel_event.is_set():
                    cancelled = True
                    progress_stopped.set()
                    self._terminate_process_tree(process, windows_job)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    self._terminate_process_tree(process, windows_job)
                    break
                try:
                    process.wait(timeout=min(0.05, remaining))
                    if _cancel_event is not None and _cancel_event.is_set():
                        cancelled = True
                        progress_stopped.set()
                        self._terminate_process_tree(process, windows_job)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if len(started_readers) != len(readers) or process.poll() is None:
                progress_stopped.set()
            # A shell may exit while background descendants still own its pipes.
            # Always tear down the process group/job before waiting for EOF.
            self._terminate_process_tree(process, windows_job)
            join_deadline = time.monotonic() + 2
            for reader in started_readers:
                reader.join(timeout=max(0, join_deadline - time.monotonic()))
            if any(reader.is_alive() for reader in started_readers):
                progress_stopped.set()
                reader_shutdown.set()
                self._terminate_process_tree(process, windows_job)
                join_deadline = time.monotonic() + 2
                for reader in started_readers:
                    reader.join(timeout=max(0, join_deadline - time.monotonic()))
            if any(reader.is_alive() for reader in started_readers):
                self._close_process_pipes(process)
                for reader in started_readers:
                    reader.join(timeout=0.2)
            progress_stopped.set()
            for reader, stream in zip(readers, (process.stdout, process.stderr)):
                if reader not in started_readers and stream is not None:
                    stream.close()
            self._close_windows_job(windows_job)

        stdout = stdout_output.render()
        stderr = stderr_output.render()
        if cancelled:
            raise _ToolCancelled("Command cancelled by user")
        if timed_out:
            raise ToolError(
                f"Command timed out after {limit}s. Partial stdout: {self._truncate(stdout)}\n"
                f"Partial stderr: {self._truncate(stderr)}"
            )
        if any(reader.is_alive() for reader in readers):
            raise ToolError("Command finished but its output streams did not close")
        if reader_errors:
            raise ToolError("Command finished but its output stream could not be read")
        return {
            "command": command,
            "cwd": self._relative(directory),
            "exit_code": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }

    @staticmethod
    def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    @staticmethod
    def _resume_windows_process(process: subprocess.Popen[bytes]) -> bool:
        if os.name != "nt":
            return True
        try:
            import ctypes
            from ctypes import wintypes

            resume = ctypes.WinDLL("ntdll", use_last_error=True).NtResumeProcess
            resume.argtypes = (wintypes.HANDLE,)
            resume.restype = ctypes.c_long
            return resume(wintypes.HANDLE(process._handle)) >= 0
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    @staticmethod
    def _create_windows_job(process: subprocess.Popen[bytes]) -> Any | None:
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return None
            if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
                kernel32.CloseHandle(job)
                return None
            return job
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    @staticmethod
    def _terminate_windows_job(job: Any | None) -> bool:
        if os.name != "nt" or job is None:
            return False
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
            kernel32.TerminateJobObject.restype = wintypes.BOOL
            return bool(kernel32.TerminateJobObject(job, 1))
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    @staticmethod
    def _close_windows_job(job: Any | None) -> None:
        if os.name != "nt" or job is None:
            return
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(job)
        except (AttributeError, OSError, TypeError, ValueError):
            pass

    @classmethod
    def _terminate_process_tree(
        cls, process: subprocess.Popen[bytes], windows_job: Any | None = None
    ) -> None:
        if os.name == "nt":
            if not cls._terminate_windows_job(windows_job):
                try:
                    subprocess.run(
                        ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    @staticmethod
    def _danger_reason(command: str) -> str | None:
        return _dangerous_command_reason(command)
