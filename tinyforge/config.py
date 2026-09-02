"""Configuration loading and validation."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal KEY=VALUE file without changing the process environment."""
    if not path.is_file():
        return {}

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"Invalid .env entry at {path}:{line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values.setdefault(key, value)
    return values


def load_env_file(path: Path) -> None:
    """Load a parsed env file without overriding existing process values."""
    for key, value in read_env_file(path).items():
        os.environ.setdefault(key, value)


def _env_int(
    environment: Mapping[str, str], name: str, default: int, minimum: int
) -> int:
    value = environment.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return parsed


def _env_bool(environment: Mapping[str, str], name: str, default: bool) -> bool:
    value = environment.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _default_state_dir() -> Path:
    try:
        return Path.home() / ".tinyforge"
    except RuntimeError:
        return Path.cwd() / ".tinyforge-state"


def _default_user_skills_dir() -> Path:
    configured = os.environ.get("TINYFORGE_SKILLS_DIR")
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    try:
        return (Path.home() / ".tinyforge" / "skills").resolve(strict=False)
    except RuntimeError:
        return (Path.cwd() / ".tinyforge-user-skills").resolve(strict=False)


@dataclass(frozen=True, slots=True)
class Config:
    api_key: str
    base_url: str
    model: str
    workspace: Path
    state_dir: Path
    user_skills_dir: Path = field(default_factory=_default_user_skills_dir)
    wire_api: str = "chat_completions"
    reasoning_effort: str | None = None
    store_responses: bool = False
    max_rounds: int = 30
    tool_timeout: int = 60
    request_timeout: int = 120
    max_tool_output: int = 30_000
    max_context_chars: int = 150_000
    max_context_tokens: int = 30_000
    allow_dangerous: bool = False
    memory_enabled: bool = True
    archive_sessions: bool = True
    skills_enabled: bool = False

    @classmethod
    def from_env(cls, workspace: str | Path = ".", **overrides: object) -> "Config":
        root = Path(workspace).expanduser().resolve()
        launch_directory = Path.cwd().resolve()
        process_environment = dict(os.environ)
        file_environment = read_env_file(launch_directory / ".env")
        if root != launch_directory:
            for key, value in read_env_file(root / ".env").items():
                file_environment.setdefault(key, value)
        environment = dict(file_environment)
        environment.update(process_environment)

        api_key = environment.get("TINYFORGE_API_KEY") or environment.get("OPENAI_API_KEY") or ""
        base_url = (
            environment.get("TINYFORGE_BASE_URL")
            or environment.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        )
        model = (
            environment.get("TINYFORGE_MODEL")
            or environment.get("OPENAI_MODEL")
            or "gpt-4o-mini"
        )
        wire_api = environment.get("TINYFORGE_WIRE_API", "chat_completions").strip().lower()
        if wire_api == "chat":
            wire_api = "chat_completions"
        state_dir = Path(
            environment.get("TINYFORGE_STATE_DIR") or _default_state_dir()
        ).expanduser().resolve()

        config = cls(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            model=model,
            workspace=root,
            state_dir=state_dir,
            user_skills_dir=Path(
                process_environment.get("TINYFORGE_SKILLS_DIR")
                or _default_user_skills_dir()
            ).expanduser().resolve(strict=False),
            wire_api=wire_api,
            reasoning_effort=environment.get("TINYFORGE_REASONING_EFFORT") or None,
            store_responses=_env_bool(environment, "TINYFORGE_STORE_RESPONSES", False),
            max_rounds=_env_int(environment, "TINYFORGE_MAX_ROUNDS", 30, 1),
            tool_timeout=_env_int(environment, "TINYFORGE_TOOL_TIMEOUT", 60, 1),
            request_timeout=_env_int(environment, "TINYFORGE_REQUEST_TIMEOUT", 120, 1),
            max_tool_output=_env_int(
                environment, "TINYFORGE_MAX_TOOL_OUTPUT", 30_000, 1_000
            ),
            max_context_chars=_env_int(
                environment, "TINYFORGE_MAX_CONTEXT_CHARS", 150_000, 10_000
            ),
            max_context_tokens=_env_int(
                environment, "TINYFORGE_MAX_CONTEXT_TOKENS", 30_000, 2_000
            ),
            memory_enabled=_env_bool(environment, "TINYFORGE_MEMORY_ENABLED", True),
            archive_sessions=_env_bool(environment, "TINYFORGE_ARCHIVE_SESSIONS", True),
            skills_enabled=_env_bool(
                process_environment, "TINYFORGE_SKILLS_ENABLED", False
            ),
        )
        known_overrides = {key: value for key, value in overrides.items() if value is not None}
        if known_overrides:
            config = replace(config, **known_overrides)
        config.validate()
        return config

    def validate(self) -> None:
        if not self.api_key.strip():
            raise ConfigError(
                "No API key found. Set TINYFORGE_API_KEY in the environment or a .env file."
            )
        if not self.model.strip():
            raise ConfigError("Model name cannot be empty")
        if self.wire_api not in {"chat_completions", "responses"}:
            raise ConfigError("TINYFORGE_WIRE_API must be chat_completions or responses")
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigError("Base URL must start with http:// or https://")
        if not self.workspace.is_dir():
            raise ConfigError(f"Workspace does not exist or is not a directory: {self.workspace}")
