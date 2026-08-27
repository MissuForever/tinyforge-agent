"""OpenAI-compatible chat-completions client."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import error, request


class ModelError(RuntimeError):
    """Raised when the model endpoint cannot provide a usable response."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    def as_message_part(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(frozen=True, slots=True)
class AssistantReply:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()

    def as_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = [call.as_message_part() for call in self.tool_calls]
        return message


class OpenAICompatibleClient:
    """Minimal HTTP client so the agent loop remains entirely visible in this project."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: int = 120,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AssistantReply:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.2,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        http_request = request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "TinyForge/0.1",
            },
        )

        try:
            with request.urlopen(http_request, timeout=self.timeout) as response:
                raw_response = response.read().decode("utf-8")
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise ModelError(f"Model API returned HTTP {exc.code}: {details[:2000]}") from exc
        except error.URLError as exc:
            raise ModelError(f"Could not reach model API: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ModelError(f"Model API timed out after {self.timeout} seconds") from exc

        try:
            data = json.loads(raw_response)
            message = data["choices"][0]["message"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"Unexpected model response: {raw_response[:2000]}") from exc

        content = message.get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)

        calls: list[ToolCall] = []
        for index, item in enumerate(message.get("tool_calls") or []):
            try:
                function = item["function"]
                arguments = function.get("arguments", "{}")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                calls.append(
                    ToolCall(
                        id=str(item.get("id") or f"call_{index}"),
                        name=str(function["name"]),
                        arguments=arguments,
                    )
                )
            except (KeyError, TypeError) as exc:
                raise ModelError(f"Malformed tool call in model response: {item!r}") from exc

        return AssistantReply(content=content, tool_calls=tuple(calls))
