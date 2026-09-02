"""Hierarchical, on-demand memory inspired by GenericAgent's memory design."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?key(?:[_-]?id)?|"
        r"(?:access|api|auth|refresh|session)[_-]?token|token|authorization|credential|"
        r"github[_-]?pat|passphrase|password|private[_-]?key|secret|session[_-]?cookie)"
        r"[\"']?\s*[=:]\s*)([\"'])"
        r"((?:(?:\\|`)[\s\S]|\2\2|(?!\2)[\s\S])*)(\2|\Z)"
    ),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?key(?:[_-]?id)?|"
        r"(?:access|api|auth|refresh|session)[_-]?token|token|authorization|credential|"
        r"github[_-]?pat|passphrase|password|private[_-]?key|secret|session[_-]?cookie)"
        r"[\"']?\s*[=:]\s*(?:bearer\s+)?)(?!\s*[\"']|\s*\[REDACTED)"
        r"(?!\s*bearer\s+\[REDACTED)"
        r"([^,;\r\n;}\]]+)"
    ),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(
        r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*://[^:/\s@]+:)([^@\s/]+)(@)"
    ),
)
_CLI_SECRET_SUFFIX = (
    r"api[_-]?key|access[_-]?key(?:[_-]?id)?|"
    r"(?:access|api|auth|refresh|session)[_-]?token|token|authorization|credential|"
    r"github[_-]?pat|passphrase|password|passwd|private[_-]?key|secret|"
    r"session[_-]?cookie|auth|pat"
)
_CLI_SECRET_KEY = rf"(?:key|(?:[a-z0-9]+[_-])*(?:{_CLI_SECRET_SUFFIX}))"
_CLI_QUOTED_SECRET = re.compile(
    rf"(?i)((?<![\w-])--?(?:{_CLI_SECRET_KEY})\s+)([\"'])"
    r"((?:(?:\\|`)[\s\S]|\2\2|(?!\2)[\s\S])*)(\2|\Z)"
)
_CLI_BEARER_SECRET = re.compile(
    rf"(?i)((?<![\w-])--?(?:{_CLI_SECRET_KEY})\s+bearer\s+)"
    r"(?!\[REDACTED(?:_API_KEY)?\])([^\s;&|]+)"
)
_CLI_UNQUOTED_SECRET = re.compile(
    rf"(?i)((?<![\w-])--?(?:{_CLI_SECRET_KEY})\s+)"
    r"(?!bearer(?:\s|$)|\[REDACTED(?:_API_KEY)?\])([^\s;&|]+)"
)

_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()
ARCHIVE_MAX_BYTES = 60_000
ARCHIVE_MAX_MESSAGES = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = f" ... [{len(value) - limit} chars omitted] ... "
    head = max(0, (limit - len(marker)) * 2 // 3)
    tail = max(0, limit - len(marker) - head)
    suffix = value[-tail:] if tail else ""
    return (value[:head] + marker + suffix)[:limit]


def redact_secrets(value: str) -> str:
    value = value.encode("utf-8", errors="replace").decode("utf-8")
    redacted = _CLI_QUOTED_SECRET.sub(r"\1\2[REDACTED]\4", value)
    redacted = _CLI_BEARER_SECRET.sub(r"\1[REDACTED]", redacted)
    redacted = _CLI_UNQUOTED_SECRET.sub(r"\1[REDACTED]", redacted)
    redacted = SECRET_PATTERNS[4].sub(r"\1[REDACTED]\3", redacted)
    redacted = SECRET_PATTERNS[1].sub(r"\1\2[REDACTED]\4", redacted)
    redacted = SECRET_PATTERNS[2].sub(r"\1[REDACTED]", redacted)
    redacted = SECRET_PATTERNS[0].sub("[REDACTED_API_KEY]", redacted)
    return SECRET_PATTERNS[3].sub(r"\1[REDACTED]", redacted)


def _is_sensitive_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    markers = (
        "apikey",
        "accesskey",
        "authorization",
        "credential",
        "password",
        "passwd",
        "passphrase",
        "privatekey",
        "secret",
        "sessioncookie",
    )
    return (
        normalized == "key"
        or normalized.endswith(("pat", "token"))
        or any(marker in normalized for marker in markers)
    )


def _sanitize_value(value: Any, *, depth: int = 0) -> Any:
    """Redact and bound an arbitrary JSON-like value before persistence."""
    if depth >= 12:
        return "[MAX_DEPTH]"
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")) and stripped.endswith(("}", "]")):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(parsed, (dict, list)):
                    sanitized = _sanitize_value(parsed, depth=depth + 1)
                    return _clip(json.dumps(sanitized, ensure_ascii=False), 5000)
        return _clip(redact_secrets(value), 5000)
    if isinstance(value, dict):
        items = list(value.items())
        sanitized = {}
        for key, item in items[:100]:
            safe_key = _clip(redact_secrets(str(key)), 200)
            sanitized[safe_key] = (
                "[REDACTED]"
                if _is_sensitive_key(str(key))
                else _sanitize_value(item, depth=depth + 1)
            )
        if len(items) > 100:
            sanitized["[TRUNCATED]"] = f"{len(items) - 100} entries omitted"
        return sanitized
    if isinstance(value, (list, tuple)):
        sanitized = [_sanitize_value(item, depth=depth + 1) for item in value[:100]]
        if len(value) > 100:
            sanitized.append(f"[TRUNCATED: {len(value) - 100} items omitted]")
        return sanitized
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _clip(redact_secrets(str(value)), 5000)


def _bounded_json_value(value: Any, limit: int) -> tuple[Any | None, int]:
    """Return a valid JSON value whose encoded form fits the requested character budget."""
    if limit <= 0:
        return None, 0
    sanitized = _sanitize_value(value)
    encoded = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= limit:
        return sanitized, len(encoded)

    preview_limit = max(0, limit - 64)
    while preview_limit >= 0:
        bounded = {"truncated": True, "preview": _clip(encoded, preview_limit)}
        bounded_size = len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":")))
        if bounded_size <= limit:
            return bounded, bounded_size
        if preview_limit == 0:
            break
        preview_limit = max(0, preview_limit - max(1, bounded_size - limit))
    return None, 0


def _is_verification_command(command: str) -> bool:
    lowered = command.casefold()
    mutation_markers = (
        "--fix",
        "--write",
        "--update-snapshot",
        "--snapshot-update",
        "updatesnapshot",
        "write_text(",
        "write_bytes(",
        ".unlink(",
        ".rename(",
    )
    if any(marker in command for marker in ("\n", "\r", "$(", "`")) or any(
        marker in lowered for marker in mutation_markers
    ):
        return False

    try:
        lexer = shlex.shlex(command, posix=os.name != "nt", punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False

    segments: list[list[str]] = [[]]
    for index, token in enumerate(tokens):
        if token and all(character in "<>" for character in token):
            return False
        if token in {"|", "||"} or token == "&" and index != 0:
            return False
        if token in {"&", "&&", "|", "||", ";"}:
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append(token.strip("\"'"))
    segments = [segment for segment in segments if segment]
    return bool(segments) and _is_verification_segment(segments[-1])


def _is_verification_segment(tokens: list[str]) -> bool:
    while tokens and (tokens[0] == "env" or "=" in tokens[0] and not tokens[0].startswith("-")):
        tokens = tokens[1:]
    if not tokens:
        return False
    executable = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    executable = executable.removesuffix(".exe")
    arguments = [token.casefold() for token in tokens[1:]]
    informational_flags = {
        "--collect-only",
        "--co",
        "--env-info",
        "--fixtures",
        "--fixtures-per-test",
        "--help",
        "--list-tests",
        "--listtests",
        "--markers",
        "--no-run",
        "--print-config",
        "--setup-plan",
        "--setup-only",
        "--show-only",
        "--showconfig",
        "--trace-config",
        "--version",
        "--dry-run",
        "-h",
    }
    if set(arguments) & informational_flags:
        return False
    if executable == "tsc" and "-v" in arguments:
        return False
    if executable == "ctest" and "-n" in arguments:
        return False
    if executable == "go" and "-list" in arguments:
        return False

    direct = {
        "ctest",
        "eslint",
        "mypy",
        "nox",
        "phpunit",
        "py.test",
        "pyright",
        "pytest",
        "rspec",
        "tox",
        "tsc",
    }
    if executable in direct:
        return True
    if executable in {"python", "python3", "py"} or re.fullmatch(
        r"python\d+(?:\.\d+)?", executable
    ):
        if "-m" in arguments:
            module_index = arguments.index("-m") + 1
            return module_index < len(arguments) and arguments[module_index] in {
                "compileall",
                "mypy",
                "pytest",
                "unittest",
            }
        if "-c" in arguments:
            code_index = arguments.index("-c") + 1
            return code_index < len(arguments) and bool(
                re.search(r"\bassert\b", arguments[code_index])
            )
        return False
    if executable in {"npm", "pnpm", "yarn", "bun"}:
        return bool(arguments) and (
            arguments[0] == "test"
            or len(arguments) >= 2
            and arguments[0] == "run"
            and arguments[1] in {"check", "lint", "test", "typecheck"}
        )
    expected_arguments = {
        "biome": {"check"},
        "cargo": {"check", "test"},
        "deno": {"check", "test"},
        "dotnet": {"build", "test"},
        "go": {"test", "vet"},
        "gradle": {"check", "test"},
        "make": {"check", "lint", "test"},
        "mix": {"test"},
        "mvn": {"test", "verify"},
        "pre-commit": {"run"},
        "ruff": {"check"},
        "swift": {"test"},
    }
    return executable in expected_arguments and bool(
        set(arguments) & expected_arguments[executable]
    )


@contextmanager
def _file_lock(path: Path):
    """Serialize index read-modify-write operations across threads and processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    with _LOCAL_LOCKS_GUARD:
        local_lock = _LOCAL_LOCKS.setdefault(key, threading.Lock())
    with local_lock:
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _terms(value: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9_.-]+|[\u3400-\u9fff]", value)
        if len(token) > 1 or "\u3400" <= token <= "\u9fff"
    }


@dataclass(frozen=True, slots=True)
class Evidence:
    id: str
    sequence: int
    tool: str
    summary: str
    verifies_code: bool = False


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    kind: str
    title: str
    content: str
    keywords: tuple[str, ...]
    evidence: tuple[str, ...]
    evidence_ids: tuple[str, ...] = ()


@dataclass(slots=True)
class WorkingMemory:
    objective: str = ""
    constraints: list[str] = field(default_factory=list)
    progress: str = "Not started"
    key_facts: list[str] = field(default_factory=list)
    next_step: str = "Inspect the workspace and relevant files."
    turn_summaries: list[str] = field(default_factory=list)
    evidence: dict[str, Evidence] = field(default_factory=dict)
    staged: list[MemoryCandidate] = field(default_factory=list)
    sequence: int = 0
    last_risk_sequence: int = 0
    failure_streak: int = 0
    recovery_guidance: str = ""

    def start(self, task: str) -> None:
        self.objective = _clip(redact_secrets(task.strip()), 1000)
        self.constraints = []
        self.progress = "Not started"
        self.key_facts = []
        self.next_step = "Inspect the workspace and relevant files."
        self.turn_summaries = []
        self.evidence = {}
        self.staged = []
        self.sequence = 0
        self.last_risk_sequence = 0
        self.failure_streak = 0
        self.recovery_guidance = ""

    def update(
        self,
        *,
        progress: str,
        objective: str | None = None,
        constraints: list[str] | None = None,
        key_facts: list[str] | None = None,
        next_step: str | None = None,
    ) -> dict[str, Any]:
        if objective is not None:
            self.objective = _clip(redact_secrets(str(objective).strip()), 1000)
        self.progress = _clip(redact_secrets(str(progress).strip()), 1200)
        if constraints is not None:
            self.constraints = [
                _clip(redact_secrets(str(item).strip()), 300)
                for item in constraints[:12]
                if str(item).strip()
            ]
        if key_facts is not None:
            self.key_facts = [
                _clip(redact_secrets(str(item).strip()), 400)
                for item in key_facts[:16]
                if str(item).strip()
            ]
        if next_step is not None:
            self.next_step = _clip(redact_secrets(str(next_step).strip()), 500)
        return {
            "progress": self.progress,
            "key_fact_count": len(self.key_facts),
            "constraint_count": len(self.constraints),
        }

    def record_tool(self, name: str, output: str) -> None:
        if name in {
            "update_working_checkpoint",
            "recall_memory",
            "stage_memory",
            "list_skills",
            "load_skill",
            "read_skill_resource",
        }:
            return
        self.sequence += 1
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            payload = {"ok": False, "error": "non-JSON tool result"}
        result = payload.get("result") if isinstance(payload, dict) else None
        successful = bool(isinstance(payload, dict) and payload.get("ok"))
        if successful and isinstance(result, dict) and "exit_code" in result:
            successful = result.get("exit_code") == 0

        summary = redact_secrets(
            self._summarize_tool(name, payload if isinstance(payload, dict) else {})
        )
        status = "ok" if successful else "failed"
        self.turn_summaries.append(_clip(f"{self.sequence}. {name} {status}: {summary}", 180))
        self.turn_summaries = self.turn_summaries[-20:]

        if successful:
            evidence_id = f"e{self.sequence}"
            command = str(result.get("command", "")) if isinstance(result, dict) else ""
            verifies_code = (
                name == "run_command"
                and not bool(result.get("command_truncated"))
                and _is_verification_command(command)
            )
            self.evidence[evidence_id] = Evidence(
                id=evidence_id,
                sequence=self.sequence,
                tool=name,
                summary=_clip(summary, 300),
                verifies_code=verifies_code,
            )
            if name in {"write_file", "edit_file"}:
                self.last_risk_sequence = self.sequence
            elif name == "run_command" and not verifies_code:
                self.last_risk_sequence = self.sequence
            self.failure_streak = 0
            self.recovery_guidance = ""
        else:
            if name in {"write_file", "edit_file", "run_command"}:
                self.last_risk_sequence = self.sequence
            self.failure_streak += 1
            if self.failure_streak == 1:
                self.recovery_guidance = (
                    "Analyze the latest error and make one small, localized correction."
                )
            else:
                self.recovery_guidance = (
                    "The current approach is still failing. Stop repeating it; inspect missing "
                    "information or switch to a materially different strategy."
                )

    @staticmethod
    def _summarize_tool(name: str, payload: dict[str, Any]) -> str:
        if not payload.get("ok"):
            return _clip(str(payload.get("error", "unknown error")), 140)
        result = payload.get("result")
        if not isinstance(result, dict):
            return "completed"
        if "exit_code" in result:
            command = _clip(str(result.get("command", "command")), 80)
            return f"exit={result['exit_code']} for {command}"
        if "path" in result:
            details = []
            for key in ("replacements", "characters_written", "total_lines"):
                if key in result:
                    details.append(f"{key}={result[key]}")
            suffix = f" ({', '.join(details)})" if details else ""
            return f"{result['path']}{suffix}"
        if "matches" in result:
            return f"{len(result['matches'])} matches"
        if "entries" in result:
            return f"{len(result['entries'])} entries"
        return "completed"

    def validate_evidence(self, ids: list[str], kind: str) -> tuple[str, ...]:
        if not ids:
            raise ValueError("At least one evidence_id is required (No Execution, No Memory).")
        records = []
        for evidence_id in ids:
            record = self.evidence.get(evidence_id)
            if record is None:
                raise ValueError(f"Unknown or unsuccessful evidence_id: {evidence_id}")
            records.append(record)
        if kind == "sop":
            verified_after_edit = any(
                item.verifies_code and item.sequence > self.last_risk_sequence
                for item in records
            )
            if not verified_after_edit:
                raise ValueError(
                    "SOP memory requires successful run_command verification evidence "
                    "from after the latest file edit, failed command, or unverified command."
                )
        elif self.last_risk_sequence and not any(
            item.sequence > self.last_risk_sequence for item in records
        ):
            raise ValueError(
                "Memory evidence must be newer than the latest file edit, failed command, "
                "or unverified command."
            )
        return tuple(f"{item.id}: {item.tool} - {item.summary}" for item in records)

    def render(self, turn: int, orientation: str) -> str:
        evidence = [
            f"{item.id}={item.tool}:{item.summary}"
            for item in sorted(self.evidence.values(), key=lambda value: value.sequence)[-8:]
        ]
        parts = [
            "<working_memory>",
            f"turn: {turn}",
            f"objective: {self.objective}",
            f"progress: {self.progress}",
            f"next_step: {self.next_step}",
        ]
        if self.constraints:
            parts.append("constraints: " + " | ".join(self.constraints))
        if self.key_facts:
            parts.append("key_facts: " + " | ".join(self.key_facts))
        if self.turn_summaries:
            parts.append("recent_events:\n- " + "\n- ".join(self.turn_summaries))
        if evidence:
            parts.append("verified_evidence: " + " | ".join(evidence))
        if self.recovery_guidance:
            parts.append("recovery: " + self.recovery_guidance)
        parts.extend([orientation, "</working_memory>"])
        return "\n".join(parts)


class MemoryStore:
    """L1 index, L2 facts, L3 SOPs, and L4 session archives on local disk."""

    def __init__(self, state_dir: Path, workspace: Path, *, archive_sessions: bool = True) -> None:
        self.state_dir = state_dir.expanduser().resolve()
        self.workspace = workspace.resolve()
        self._workspace_key = os.path.normcase(str(self.workspace))
        workspace_id = hashlib.sha256(self._workspace_key.encode("utf-8")).hexdigest()[:16]
        self.root = self.state_dir / "workspaces" / workspace_id
        self.index_path = self.root / "index.json"
        self.lock_path = self.root / ".index.lock"
        self.archive_sessions = archive_sessions

    def _empty_index(self) -> dict[str, Any]:
        return {"version": 1, "workspace": str(self.workspace), "facts": [], "sops": []}

    def _load_index(self) -> dict[str, Any]:
        if not self.index_path.is_file():
            return self._empty_index()
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._empty_index()
        if not isinstance(data, dict):
            return self._empty_index()
        stored_workspace = data.get("workspace")
        if stored_workspace is not None and os.path.normcase(str(stored_workspace)) != self._workspace_key:
            raise OSError("Persistent memory workspace identity does not match its index")
        return data

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline=""
        )
        temporary.replace(path)

    def commit(self, candidate: MemoryCandidate) -> dict[str, Any]:
        with _file_lock(self.lock_path):
            return self._commit_unlocked(candidate)

    def _commit_unlocked(self, candidate: MemoryCandidate) -> dict[str, Any]:
        index = self._load_index()
        collection_name = "facts" if candidate.kind == "fact" else "sops"
        collection = index.setdefault(collection_name, [])
        safe_title = redact_secrets(candidate.title.strip())
        safe_keywords = tuple(redact_secrets(value) for value in candidate.keywords)
        safe_evidence = tuple(redact_secrets(value) for value in candidate.evidence)
        normalized_title = safe_title.casefold()
        metadata = next(
            (item for item in collection if str(item.get("title", "")).casefold() == normalized_title),
            None,
        )
        created = _now()
        if metadata is None:
            memory_id = hashlib.sha256(
                f"{candidate.kind}:{normalized_title}".encode("utf-8")
            ).hexdigest()[:12]
            metadata = {
                "id": memory_id,
                "kind": candidate.kind,
                "title": safe_title,
                "keywords": list(safe_keywords),
                "updated_at": created,
                "use_count": 0,
            }
            collection.append(metadata)
            revision = 1
        else:
            memory_id = str(metadata["id"])
            existing = self._read_entry(candidate.kind, memory_id) or {}
            created = str(existing.get("created_at", created))
            revision = int(existing.get("revision", 0)) + 1
            metadata.update(
                {
                    "title": safe_title,
                    "keywords": list(safe_keywords),
                    "updated_at": _now(),
                }
            )
        entry = {
            "id": memory_id,
            "kind": candidate.kind,
            "title": safe_title,
            "content": redact_secrets(candidate.content.strip()),
            "keywords": list(safe_keywords),
            "evidence": list(safe_evidence),
            "created_at": created,
            "updated_at": _now(),
            "revision": revision,
        }
        self._atomic_write(self.root / collection_name / f"{memory_id}.json", entry)
        collection.sort(key=lambda item: (str(item.get("title", "")).casefold(), str(item.get("id"))))
        self._atomic_write(self.index_path, index)
        return {"id": memory_id, "kind": candidate.kind, "revision": revision}

    def _read_entry(self, kind: str, memory_id: str) -> dict[str, Any] | None:
        collection = "facts" if kind == "fact" else "sops"
        path = self.root / collection / f"{memory_id}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def orientation(self, *, max_entries: int = 24, max_chars: int = 2200) -> str:
        index = self._load_index()
        entries = [*index.get("facts", []), *index.get("sops", [])]
        if not entries:
            return "persistent_memory_index: empty"
        entries.sort(
            key=lambda item: (int(item.get("use_count", 0)), str(item.get("updated_at", ""))),
            reverse=True,
        )
        lines = ["persistent_memory_index (use recall_memory for details):"]
        for item in entries[:max_entries]:
            keywords = ",".join(
                redact_secrets(str(value)) for value in item.get("keywords", [])[:5]
            )
            lines.append(
                f"- [{item.get('kind')}:{item.get('id')}] "
                f"{redact_secrets(str(item.get('title', '')))} ({keywords})"
            )
        return _clip("\n".join(lines), max_chars)

    def search(self, query: str, *, kind: str = "any", max_results: int = 5) -> list[dict[str, Any]]:
        with _file_lock(self.lock_path):
            return self._search_unlocked(query, kind=kind, max_results=max_results)

    def _search_unlocked(
        self, query: str, *, kind: str = "any", max_results: int = 5
    ) -> list[dict[str, Any]]:
        index = self._load_index()
        query_terms = _terms(query)
        candidates: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        collections = (("fact", "facts"), ("sop", "sops"))
        for item_kind, collection_name in collections:
            if kind != "any" and kind != item_kind:
                continue
            for metadata in index.get(collection_name, []):
                entry = self._read_entry(item_kind, str(metadata.get("id")))
                if entry is None:
                    continue
                title_terms = _terms(str(entry.get("title", "")))
                keyword_terms = _terms(" ".join(str(value) for value in entry.get("keywords", [])))
                content_terms = _terms(str(entry.get("content", "")))
                lowered_query = query.casefold().strip()
                score = 5 * len(query_terms & title_terms)
                score += 3 * len(query_terms & keyword_terms)
                score += len(query_terms & content_terms)
                if lowered_query and lowered_query in str(entry.get("title", "")).casefold():
                    score += 10
                if not query_terms or score > 0:
                    candidates.append((score, metadata, entry))
        candidates.sort(
            key=lambda item: (item[0], str(item[1].get("updated_at", ""))), reverse=True
        )
        results = []
        for score, metadata, entry in candidates[: max(1, min(max_results, 10))]:
            metadata["use_count"] = int(metadata.get("use_count", 0)) + 1
            results.append(
                {
                    "id": entry.get("id"),
                    "kind": entry.get("kind"),
                    "title": redact_secrets(str(entry.get("title", ""))),
                    "content": _clip(redact_secrets(str(entry.get("content", ""))), 5000),
                    "keywords": [
                        redact_secrets(str(value)) for value in entry.get("keywords", [])
                    ],
                    "evidence": [
                        _clip(redact_secrets(str(value)), 500)
                        for value in entry.get("evidence", [])[:20]
                    ],
                    "revision": entry.get("revision", 1),
                    "score": score,
                }
            )
        if results:
            self._atomic_write(self.index_path, index)
        return results

    def archive(self, task: str, answer: str, success: bool, messages: list[dict[str, Any]]) -> None:
        if not self.archive_sessions:
            return
        safe_messages = []
        safe_task = _clip(redact_secrets(task), 4000)
        safe_answer = _clip(redact_secrets(answer), 8000)
        selected_messages = messages[-ARCHIVE_MAX_MESSAGES:]
        for message in selected_messages:
            copied: dict[str, Any] = {
                "role": _clip(redact_secrets(str(message.get("role", ""))), 40)
            }
            if message.get("tool_call_id"):
                copied["tool_call_id"] = _clip(
                    redact_secrets(str(message["tool_call_id"])), 200
                )
            content = _sanitize_value(str(message.get("content") or ""))
            copied["content"] = _clip(str(content), 5000)
            if message.get("tool_calls"):
                calls, _ = _bounded_json_value(message["tool_calls"], 5000)
                if calls is not None:
                    copied["tool_calls"] = calls
            safe_messages.append(copied)
        record = {
            "created_at": _now(),
            "workspace": redact_secrets(str(self.workspace)),
            "task": safe_task,
            "success": success,
            "answer": safe_answer,
            "messages": safe_messages,
            "messages_truncated": len(selected_messages) < len(messages),
        }
        def serialized_size() -> int:
            return len(json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))

        while serialized_size() > ARCHIVE_MAX_BYTES and safe_messages:
            safe_messages.pop(0)
            record["messages_truncated"] = True
        while serialized_size() > ARCHIVE_MAX_BYTES and record["answer"]:
            record["answer"] = record["answer"][: len(record["answer"]) // 2]
        while serialized_size() > ARCHIVE_MAX_BYTES and record["task"]:
            record["task"] = record["task"][: len(record["task"]) // 2]
        if serialized_size() > ARCHIVE_MAX_BYTES:
            record["workspace"] = _clip(str(record["workspace"]), 256)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self._atomic_write(self.root / "sessions" / f"{stamp}-{uuid.uuid4().hex[:6]}.json", record)


class MemoryRuntime:
    """Agent-facing memory tools plus per-task working state."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.working = WorkingMemory()

    @property
    def definitions(self) -> list[dict[str, Any]]:
        base = {"type": "object", "additionalProperties": False}
        return [
            {
                "type": "function",
                "function": {
                    "name": "update_working_checkpoint",
                    "description": (
                        "Update the compact task checkpoint after a meaningful milestone. "
                        "Do not call after every trivial action."
                    ),
                    "parameters": {
                        **base,
                        "properties": {
                            "progress": {"type": "string"},
                            "objective": {"type": "string"},
                            "constraints": {"type": "array", "items": {"type": "string"}},
                            "key_facts": {"type": "array", "items": {"type": "string"}},
                            "next_step": {"type": "string"},
                        },
                        "required": ["progress"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "recall_memory",
                    "description": (
                        "Retrieve relevant verified facts or SOPs from persistent memory. "
                        "Use when the memory index indicates relevant knowledge exists."
                    ),
                    "parameters": {
                        **base,
                        "properties": {
                            "query": {"type": "string"},
                            "kind": {"type": "string", "enum": ["any", "fact", "sop"]},
                            "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "stage_memory",
                    "description": (
                        "Stage a stable reusable fact or SOP. It is committed only if the task "
                        "finishes successfully. Cite verified_evidence IDs from working memory; "
                        "never store guesses, temporary state, secrets, or failed approaches."
                    ),
                    "parameters": {
                        **base,
                        "properties": {
                            "kind": {"type": "string", "enum": ["fact", "sop"]},
                            "title": {"type": "string"},
                            "content": {"type": "string"},
                            "keywords": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 12,
                            },
                            "evidence_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "maxItems": 20,
                            },
                        },
                        "required": ["kind", "title", "content", "evidence_ids"],
                    },
                },
            },
        ]

    def start_task(self, task: str) -> None:
        self.working.start(task)

    def reset(self) -> None:
        self.working = WorkingMemory()

    def anchor(self, turn: int) -> str:
        return self.working.render(turn, self.store.orientation())

    def record_tool(self, name: str, output: str) -> None:
        self.working.record_tool(name, output)

    def execute(self, name: str, arguments: str) -> str:
        started = time.monotonic()
        try:
            parsed = json.loads(arguments or "{}")
            if not isinstance(parsed, dict):
                raise ValueError("Tool arguments must be a JSON object")
            if name == "update_working_checkpoint":
                result = self.working.update(**parsed)
            elif name == "recall_memory":
                query = str(parsed.get("query", "")).strip()
                kind = str(parsed.get("kind", "any"))
                if not query:
                    raise ValueError("query must be a non-empty string")
                if kind not in {"any", "fact", "sop"}:
                    raise ValueError("kind must be any, fact, or sop")
                result = {
                    "matches": self.store.search(
                        query,
                        kind=kind,
                        max_results=int(parsed.get("max_results", 5)),
                    )
                }
            elif name == "stage_memory":
                result = self._stage(**parsed)
            else:
                raise ValueError(f"Unknown memory tool: {name}")
            payload = {
                "ok": True,
                "result": result,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        except (ValueError, TypeError, json.JSONDecodeError, OSError) as exc:
            payload = {
                "ok": False,
                "error": str(exc),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        return json.dumps(payload, ensure_ascii=False)

    def _stage(
        self,
        *,
        kind: str,
        title: str,
        content: str,
        evidence_ids: list[str],
        keywords: list[str] | None = None,
    ) -> dict[str, Any]:
        if kind not in {"fact", "sop"}:
            raise ValueError("kind must be fact or sop")
        if not isinstance(title, str) or not isinstance(content, str):
            raise ValueError("title and content must be strings")
        if not isinstance(evidence_ids, list) or not all(
            isinstance(value, str) for value in evidence_ids
        ):
            raise ValueError("evidence_ids must be an array of strings")
        if not evidence_ids:
            raise ValueError("At least one evidence_id is required (No Execution, No Memory).")
        if len(evidence_ids) > 20:
            raise ValueError("evidence_ids must contain at most 20 items")
        if keywords is not None and (
            not isinstance(keywords, list)
            or not all(isinstance(value, str) for value in keywords)
        ):
            raise ValueError("keywords must be an array of strings")
        clean_title = redact_secrets(title.strip())
        clean_content = redact_secrets(content.strip())
        if not clean_title or len(clean_title) > 160:
            raise ValueError("title must contain 1-160 characters")
        if len(clean_content) < 20 or len(clean_content) > 8000:
            raise ValueError("content must contain 20-8000 characters")
        evidence = self.working.validate_evidence(evidence_ids, kind)
        candidate = MemoryCandidate(
            kind=kind,
            title=clean_title,
            content=redact_secrets(clean_content),
            keywords=tuple(
                dict.fromkeys(
                    _clip(redact_secrets(str(value).strip()), 80)
                    for value in (keywords or [])[:12]
                    if str(value).strip()
                )
            ),
            evidence=evidence,
            evidence_ids=tuple(evidence_ids),
        )
        self.working.staged.append(candidate)
        return {"staged": True, "kind": kind, "title": clean_title, "evidence": list(evidence)}

    def finish(
        self,
        *,
        success: bool,
        task: str,
        answer: str,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        committed = []
        self.store.archive(task, answer, success, messages)
        if success:
            for candidate in self.working.staged:
                try:
                    evidence = self.working.validate_evidence(
                        list(candidate.evidence_ids), candidate.kind
                    )
                except ValueError:
                    continue
                refreshed = MemoryCandidate(
                    kind=candidate.kind,
                    title=candidate.title,
                    content=candidate.content,
                    keywords=candidate.keywords,
                    evidence=evidence,
                    evidence_ids=candidate.evidence_ids,
                )
                committed.append(self.store.commit(refreshed))
        self.working.staged = []
        return committed
