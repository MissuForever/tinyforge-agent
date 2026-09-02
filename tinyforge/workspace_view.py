"""Bounded, read-only workspace indexing and preview helpers for the GUI."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from .memory import redact_secrets


MAX_WORKSPACE_FILES = 5_000
MAX_WORKSPACE_SCAN_ENTRIES = 50_000
MAX_FILE_PREVIEW_BYTES = 512 * 1024
MAX_FILE_PREVIEW_CHARS = 200_000
MAX_FILE_PREVIEW_LINES = 4_000
MAX_GIT_OUTPUT_BYTES = 8 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 3

_IGNORED_DIRECTORIES = {
    ".aws",
    ".azure",
    ".codex",
    ".demo",
    ".direnv",
    ".docker",
    ".git",
    ".gnupg",
    ".hg",
    ".kube",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".ssh",
    ".svn",
    ".tinyforge-state",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "env",
    "node_modules",
    "venv",
}
_SENSITIVE_NAMES = {
    ".env",
    ".envrc",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "id_dsa",
    "id_ecdsa",
    "id_ecdsa_sk",
    "id_ed25519",
    "id_ed25519_sk",
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
    ".ppk",
}
_PRIVATE_KEY_PREFIXES = (
    "id_dsa.",
    "id_ecdsa.",
    "id_ecdsa_sk.",
    "id_ed25519.",
    "id_ed25519_sk.",
    "id_rsa.",
)
_SENSITIVE_DIRECTORIES = {
    ".aws",
    ".azure",
    ".codex",
    ".docker",
    ".direnv",
    ".gnupg",
    ".kube",
    ".ssh",
}
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BIDI_CONTROL_RE = re.compile(r"[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_PRIVATE_KEY_BLOCK_RE = re.compile(
    br"-----BEGIN [A-Z0-9 -]*PRIVATE KEY(?: BLOCK)?-----",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class WorkspaceFile:
    relative_path: str
    git_status: str = ""
    is_link: bool = False


@dataclass(frozen=True, slots=True)
class WorkspaceIndex:
    workspace: Path
    files: tuple[WorkspaceFile, ...]
    truncated: bool = False
    git_available: bool = False
    error: str = ""


@dataclass(frozen=True, slots=True)
class WorkspaceFilePreview:
    relative_path: str
    status: str
    text: str = ""
    size_bytes: int = 0
    line_count: int = 0
    truncated: bool = False


def _is_sensitive_workspace_name(value: str) -> bool:
    name = value.casefold()
    return (
        name in _SENSITIVE_NAMES
        or name in _SENSITIVE_DIRECTORIES
        or name.startswith(".env.")
        or name.startswith(".envrc")
        or name.startswith(_PRIVATE_KEY_PREFIXES)
        or "credential" in name
        or "private_key" in name
        or "private-key" in name
        or "secret" in name
        or Path(name).suffix in _SENSITIVE_SUFFIXES
    )


def is_sensitive_workspace_path(path: Path) -> bool:
    return any(_is_sensitive_workspace_name(part) for part in path.parts)


def _is_ignored_workspace_path(path: Path) -> bool:
    return bool({part.casefold() for part in path.parts} & _IGNORED_DIRECTORIES)


def _relative_parts(value: str) -> tuple[str, ...] | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    windows_path = PureWindowsPath(value)
    if Path(value).is_absolute() or windows_path.is_absolute() or windows_path.drive:
        return None
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if any(part in {"", ".", ".."} for part in path.parts):
        return None
    return tuple(path.parts)


def _is_link_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attributes & reparse_flag)
    except OSError:
        return False


def _safe_index_path(root: Path, value: str) -> tuple[str, Path, bool] | None:
    parts = _relative_parts(value)
    if parts is None:
        return None
    relative = Path(*parts)
    if _is_ignored_workspace_path(relative) or is_sensitive_workspace_path(relative):
        return None
    candidate = root.joinpath(*parts)
    is_link = _is_link_or_junction(candidate)
    try:
        validation_target = candidate.parent if is_link else candidate
        validation_target.resolve(strict=False).relative_to(root)
    except (OSError, ValueError):
        return None
    return PurePosixPath(*parts).as_posix(), candidate, is_link


def _run_git(workspace: Path, arguments: list[str]) -> bytes | None:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    command = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "status.relativePaths=true",
        "-C",
        str(workspace),
        *arguments,
    ]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return None

    output = bytearray()
    overflow = threading.Event()
    read_failed = threading.Event()

    def read_output() -> None:
        assert process.stdout is not None
        try:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    return
                if len(output) + len(chunk) > MAX_GIT_OUTPUT_BYTES:
                    overflow.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
                    return
                output.extend(chunk)
        except OSError:
            read_failed.set()

    reader = threading.Thread(
        target=read_output,
        name="tinyforge-git-output",
        daemon=True,
    )
    reader.start()
    try:
        return_code = process.wait(timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return_code = -1
    reader.join(timeout=1)
    if process.stdout is not None:
        try:
            process.stdout.close()
        except OSError:
            pass
    if reader.is_alive() or overflow.is_set() or read_failed.is_set() or return_code != 0:
        return None
    return bytes(output)


def _workspace_has_git_metadata(workspace: Path) -> bool:
    for directory in (workspace, *workspace.parents):
        try:
            if os.path.lexists(directory / ".git"):
                return True
        except OSError:
            continue
    return False


def _parse_git_status(raw: bytes) -> dict[str, str]:
    statuses: dict[str, str] = {}
    records = raw.split(b"\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4 or record[2:3] != b" ":
            continue
        code = record[:2].decode("ascii", "replace")
        path = record[3:].decode("utf-8", "replace").replace("\\", "/")
        if path:
            statuses[path] = code
        if any(marker in code for marker in "RC") and index < len(records):
            index += 1
    return statuses


def _git_workspace_files(workspace: Path) -> tuple[list[str], dict[str, str]] | None:
    listed = _run_git(
        workspace,
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."],
    )
    if listed is None:
        return None
    status_raw = _run_git(
        workspace,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=all",
            "--",
            ".",
        ],
    )
    paths = [
        item.decode("utf-8", "replace").replace("\\", "/")
        for item in listed.split(b"\0")
        if item
    ]
    statuses = _parse_git_status(status_raw) if status_raw is not None else {}
    return paths, statuses


def _fallback_workspace_files(
    workspace: Path,
    *,
    max_entries: int,
) -> tuple[list[tuple[str, bool]], bool]:
    files: list[tuple[str, bool]] = []
    stack = [workspace]
    scanned = 0
    truncated = False
    while stack and len(files) < max_entries and scanned < MAX_WORKSPACE_SCAN_ENTRIES:
        directory = stack.pop()
        try:
            children = []
            with os.scandir(directory) as iterator:
                for child in iterator:
                    if scanned >= MAX_WORKSPACE_SCAN_ENTRIES:
                        truncated = True
                        break
                    scanned += 1
                    children.append(child)
            children.sort(key=lambda item: item.name.casefold())
        except OSError:
            continue
        subdirectories: list[Path] = []
        for child in children:
            try:
                relative = Path(child.path).relative_to(workspace)
            except ValueError:
                continue
            if _is_ignored_workspace_path(relative) or is_sensitive_workspace_path(relative):
                continue
            child_path = Path(child.path)
            is_link = _is_link_or_junction(child_path)
            try:
                is_directory = child.is_dir(follow_symlinks=False)
                is_file = child.is_file(follow_symlinks=False)
            except OSError:
                continue
            if is_directory and not is_link:
                subdirectories.append(child_path)
            elif is_file or is_link:
                files.append((relative.as_posix(), is_link))
                if len(files) >= max_entries:
                    truncated = True
                    break
        stack.extend(reversed(subdirectories))
    if stack:
        truncated = True
    return files, truncated


def scan_workspace(
    workspace: Path,
    *,
    max_entries: int = MAX_WORKSPACE_FILES,
) -> WorkspaceIndex:
    max_entries = min(max(int(max_entries), 1), MAX_WORKSPACE_FILES)
    try:
        root = workspace.expanduser().resolve(strict=True)
    except (OSError, ValueError):
        return WorkspaceIndex(Path(workspace), (), error="Workspace is not available.")
    if not root.is_dir():
        return WorkspaceIndex(root, (), error="Workspace is not a directory.")

    git_result = _git_workspace_files(root)
    files: list[WorkspaceFile] = []
    truncated = False
    if git_result is not None:
        raw_paths, raw_statuses = git_result
        seen: set[str] = set()
        for raw_path in sorted(raw_paths, key=str.casefold):
            safe = _safe_index_path(root, raw_path)
            if safe is None:
                continue
            relative_path, _, is_link = safe
            if relative_path in seen:
                continue
            seen.add(relative_path)
            files.append(
                WorkspaceFile(
                    relative_path,
                    raw_statuses.get(raw_path, raw_statuses.get(relative_path, "")),
                    is_link,
                )
            )
            if len(files) >= max_entries:
                truncated = len(raw_paths) > len(seen)
                break
        return WorkspaceIndex(root, tuple(files), truncated, git_available=True)

    if _workspace_has_git_metadata(root):
        return WorkspaceIndex(
            root,
            (),
            git_available=True,
            error="Git file index is unavailable; ignored files were not scanned.",
        )

    fallback_files, truncated = _fallback_workspace_files(root, max_entries=max_entries)
    for relative_path, is_link in fallback_files:
        safe = _safe_index_path(root, relative_path)
        if safe is not None:
            normalized, _, confirmed_link = safe
            files.append(WorkspaceFile(normalized, is_link=is_link or confirmed_link))
    files.sort(key=lambda item: item.relative_path.casefold())
    return WorkspaceIndex(root, tuple(files), truncated, git_available=False)


def preview_workspace_file(
    workspace: Path,
    relative_path: str,
    *,
    max_bytes: int = MAX_FILE_PREVIEW_BYTES,
    max_chars: int = MAX_FILE_PREVIEW_CHARS,
    max_lines: int = MAX_FILE_PREVIEW_LINES,
) -> WorkspaceFilePreview:
    max_bytes = min(max(int(max_bytes), 1), MAX_FILE_PREVIEW_BYTES)
    max_chars = min(max(int(max_chars), 1), MAX_FILE_PREVIEW_CHARS)
    max_lines = min(max(int(max_lines), 1), MAX_FILE_PREVIEW_LINES)
    try:
        root = workspace.expanduser().resolve(strict=True)
    except (OSError, ValueError):
        return WorkspaceFilePreview(relative_path, "missing")
    parts = _relative_parts(relative_path)
    if parts is None:
        return WorkspaceFilePreview(relative_path, "outside")
    relative = Path(*parts)
    normalized = PurePosixPath(*parts).as_posix()
    if _is_ignored_workspace_path(relative) or is_sensitive_workspace_path(relative):
        return WorkspaceFilePreview(normalized, "sensitive")
    candidate = root.joinpath(*parts)
    if _is_link_or_junction(candidate):
        return WorkspaceFilePreview(normalized, "link")
    try:
        candidate.resolve(strict=True).relative_to(root)
    except FileNotFoundError:
        return WorkspaceFilePreview(normalized, "missing")
    except (OSError, ValueError):
        return WorkspaceFilePreview(normalized, "outside")
    if not candidate.is_file():
        return WorkspaceFilePreview(normalized, "directory")
    try:
        size_bytes = candidate.stat().st_size
        if size_bytes > max_bytes:
            return WorkspaceFilePreview(normalized, "too_large", size_bytes=size_bytes)
        with candidate.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError:
        return WorkspaceFilePreview(normalized, "unreadable")
    if len(raw) > max_bytes:
        return WorkspaceFilePreview(normalized, "too_large", size_bytes=size_bytes)
    if _PRIVATE_KEY_BLOCK_RE.search(raw):
        return WorkspaceFilePreview(normalized, "sensitive", size_bytes=size_bytes)
    if b"\x00" in raw:
        return WorkspaceFilePreview(normalized, "binary", size_bytes=size_bytes)
    control_count = sum(
        byte < 32 and byte not in {9, 10, 13}
        for byte in raw
    )
    if raw and control_count / len(raw) > 0.02:
        return WorkspaceFilePreview(normalized, "binary", size_bytes=size_bytes)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return WorkspaceFilePreview(normalized, "binary", size_bytes=size_bytes)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = _CONTROL_CHARACTER_RE.sub("", text)
    text = _BIDI_CONTROL_RE.sub("", text)
    text = redact_secrets(text)
    line_count = text.count("\n") + (1 if text else 0)
    rendered_lines = text.splitlines(keepends=True)
    truncated = len(text) > max_chars or len(rendered_lines) > max_lines
    if len(rendered_lines) > max_lines:
        text = "".join(rendered_lines[:max_lines])
    if truncated:
        text = text[:max_chars]
    return WorkspaceFilePreview(
        normalized,
        "text",
        text=text,
        size_bytes=size_bytes,
        line_count=line_count,
        truncated=truncated,
    )
