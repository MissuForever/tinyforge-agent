"""Configuration loading and validation."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


def load_env_file(path: Path) -> None:
    """Load a minimal KEY=VALUE env file without overriding real environment values."""
    if not path.is_file():
        return

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
            os.environ.setdefault(key, value)


def _env_int(name: str, default: int, minimum: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return parsed


@dataclass(frozen=True, slots=True)
class Config:
    api_key: str
    base_url: str
    model: str
    workspace: Path
    max_rounds: int = 30
    tool_timeout: int = 60
    request_timeout: int = 120
    max_tool_output: int = 30_000
    max_context_chars: int = 150_000
    allow_dangerous: bool = False

    @classmethod
    def from_env(cls, workspace: str | Path = ".", **overrides: object) -> "Config":
        root = Path(workspace).expanduser().resolve()
        launch_directory = Path.cwd().resolve()
        load_env_file(launch_directory / ".env")
        if root != launch_directory:
            load_env_file(root / ".env")

        api_key = os.getenv("TINYFORGE_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        base_url = (
            os.getenv("TINYFORGE_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        )
        model = os.getenv("TINYFORGE_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"

        config = cls(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            model=model,
            workspace=root,
            max_rounds=_env_int("TINYFORGE_MAX_ROUNDS", 30, 1),
            tool_timeout=_env_int("TINYFORGE_TOOL_TIMEOUT", 60, 1),
            request_timeout=_env_int("TINYFORGE_REQUEST_TIMEOUT", 120, 1),
            max_tool_output=_env_int("TINYFORGE_MAX_TOOL_OUTPUT", 30_000, 1_000),
            max_context_chars=_env_int("TINYFORGE_MAX_CONTEXT_CHARS", 150_000, 10_000),
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
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigError("Base URL must start with http:// or https://")
        if not self.workspace.is_dir():
            raise ConfigError(f"Workspace does not exist or is not a directory: {self.workspace}")
