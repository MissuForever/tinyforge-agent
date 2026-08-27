"""OpenAI-compatible Chat Completions and Responses API client."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0


@dataclass(frozen=True, slots=True)
class AssistantReply:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: ModelUsage = field(default_factory=ModelUsage)

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
        wire_api: str = "chat_completions",
        reasoning_effort: str | None = None,
        store: bool = False,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.wire_api = wire_api
        self.reasoning_effort = reasoning_effort
        self.store = store

    @property
    def endpoint(self) -> str:
        if self.wire_api == "responses":
            if self.base_url.endswith("/responses"):
                return self.base_url
            return f"{self.base_url}/responses"
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AssistantReply:
        if self.wire_api == "responses":
            payload = self._responses_payload(messages, tools)
        else:
            payload = {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "store": self.store,
            }
        data = self._post(payload)
        if self.wire_api == "responses":
            return self._parse_responses(data)
        return self._parse_chat_completions(data)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
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
            if not isinstance(data, dict):
                raise TypeError("response root is not an object")
        except (json.JSONDecodeError, TypeError) as exc:
            raise ModelError(f"Unexpected model response: {raw_response[:2000]}") from exc
        return data

    @staticmethod
    def _parse_chat_completions(data: dict[str, Any]) -> AssistantReply:
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"Unexpected Chat Completions response: {str(data)[:2000]}") from exc

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

        return AssistantReply(
            content=content,
            tool_calls=tuple(calls),
            usage=OpenAICompatibleClient._parse_usage(data, responses=False),
        )

    def _responses_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        response_tools = []
        for item in tools:
            function = item["function"]
            response_tools.append(
                {
                    "type": "function",
                    "name": function["name"],
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {"type": "object"}),
                }
            )
        payload: dict[str, Any] = {
            "model": self.model,
            "input": self._responses_input(messages),
            "tools": response_tools,
            "tool_choice": "auto",
            "store": self.store,
        }
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        return payload

    @staticmethod
    def _responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role in {"system", "developer", "user"}:
                items.append({"role": role, "content": content or ""})
            elif role == "assistant":
                if content:
                    items.append({"role": "assistant", "content": content})
                for call in message.get("tool_calls") or []:
                    function = call["function"]
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": call["id"],
                            "name": function["name"],
                            "arguments": function.get("arguments", "{}"),
                        }
                    )
            elif role == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message["tool_call_id"],
                        "output": content or "",
                    }
                )
        return items

    @staticmethod
    def _parse_responses(data: dict[str, Any]) -> AssistantReply:
        content_parts: list[str] = []
        calls: list[ToolCall] = []
        output = data.get("output")
        if not isinstance(output, list):
            raise ModelError(f"Unexpected Responses API response: {str(data)[:2000]}")
        for index, item in enumerate(output):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                arguments = item.get("arguments", "{}")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                try:
                    calls.append(
                        ToolCall(
                            id=str(item.get("call_id") or item.get("id") or f"call_{index}"),
                            name=str(item["name"]),
                            arguments=arguments,
                        )
                    )
                except KeyError as exc:
                    raise ModelError(f"Malformed Responses function call: {item!r}") from exc
            elif item.get("type") == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        text = part.get("text")
                        if isinstance(text, str):
                            content_parts.append(text)
        if not content_parts and isinstance(data.get("output_text"), str):
            content_parts.append(data["output_text"])
        return AssistantReply(
            content="\n".join(content_parts),
            tool_calls=tuple(calls),
            usage=OpenAICompatibleClient._parse_usage(data, responses=True),
        )

    @staticmethod
    def _parse_usage(data: dict[str, Any], *, responses: bool) -> ModelUsage:
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return ModelUsage()
        if responses:
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            details = usage.get("input_tokens_details") or {}
        else:
            input_tokens = int(usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("completion_tokens") or 0)
            details = usage.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
        total = int(usage.get("total_tokens") or input_tokens + output_tokens)
        return ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total,
            cached_input_tokens=cached,
        )
