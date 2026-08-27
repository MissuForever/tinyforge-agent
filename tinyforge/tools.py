"""Local tools exposed to the model."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class ToolError(RuntimeError):
    """A recoverable tool error that should be returned to the model."""


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
        started = time.monotonic()
        try:
            tool = self._tools.get(name)
            if tool is None:
                raise ToolError(f"Unknown tool: {name}")
            try:
                parsed = json.loads(arguments or "{}")
            except json.JSONDecodeError as exc:
                raise ToolError(f"Arguments are not valid JSON: {exc.msg}") from exc
            if not isinstance(parsed, dict):
                raise ToolError("Tool arguments must be a JSON object")
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
        self, command: str, cwd: str = ".", timeout: int | None = None
    ) -> dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command must be a non-empty string")
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
            invocation = [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ]
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            invocation = ["/bin/sh", "-lc", command]
            creation_flags = 0
        environment = os.environ.copy()
        environment.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            completed = subprocess.run(
                invocation,
                cwd=directory,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=limit,
                env=environment,
                creationflags=creation_flags,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else exc.stdout
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else exc.stderr
            raise ToolError(
                f"Command timed out after {limit}s. Partial stdout: {stdout or ''}\n"
                f"Partial stderr: {stderr or ''}"
            ) from exc
        return {
            "command": command,
            "cwd": self._relative(directory),
            "exit_code": completed.returncode,
            "stdout": self._truncate(completed.stdout),
            "stderr": self._truncate(completed.stderr),
        }

    @staticmethod
    def _danger_reason(command: str) -> str | None:
        normalized = " ".join(command.lower().split())
        patterns = (
            (r"\bgit\s+reset\s+--hard\b", "git history/worktree destruction"),
            (r"\bgit\s+clean\b", "untracked-file deletion"),
            (r"\bgit\s+push\b.*(?:--force|-f\b)", "forced remote history rewrite"),
            (r"\brm\s+-(?:\w*r\w*f|\w*f\w*r)\b", "recursive forced deletion"),
            (r"\brmdir\s+/s\b", "recursive deletion"),
            (r"\bdel\s+/(?:s|q)\b", "bulk deletion"),
            (r"\bremove-item\b.*\s-recurse\b", "recursive deletion"),
            (r"\bformat(?:\.com)?\s+[a-z]:", "disk formatting"),
            (r"\b(?:shutdown|reboot|restart-computer)\b", "machine shutdown/restart"),
            (r"\bdiskpart\b", "disk partition modification"),
        )
        for pattern, reason in patterns:
            if re.search(pattern, normalized, flags=re.IGNORECASE):
                return reason
        return None
