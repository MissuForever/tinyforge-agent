"""Conversation-size accounting and structural compaction."""

from __future__ import annotations

import json
import math
from typing import Any


def message_size(messages: list[dict[str, Any]]) -> int:
    return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))


def estimated_tokens(value: str) -> int:
    """Conservative dependency-free estimate that does not underweight CJK text."""
    ascii_count = sum(1 for character in value if ord(character) < 128)
    non_ascii_count = len(value) - ascii_count
    return max(1, math.ceil(ascii_count / 4 + non_ascii_count * 1.25))


def estimated_message_tokens(messages: list[dict[str, Any]]) -> int:
    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    return estimated_tokens(serialized)


def _head_tail(value: str, limit: int, label: str) -> str:
    if len(value) <= limit:
        return value
    marker = f"\n... {label}; {len(value) - limit} chars omitted ...\n"
    head = max(0, (limit - len(marker)) // 2)
    tail = max(0, limit - len(marker) - head)
    suffix = value[-tail:] if tail else ""
    return (value[:head] + marker + suffix)[:limit]


def _compress_old_content(messages: list[dict[str, Any]], recent_exempt: int) -> None:
    boundary = max(1, len(messages) - recent_exempt)
    for index in range(1, boundary):
        message = messages[index]
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if message.get("role") == "tool":
            message["content"] = _head_tail(content, 1800, "older tool output compressed")
        elif message.get("role") == "assistant":
            message["content"] = _head_tail(content, 900, "older assistant text compressed")


def compact_messages(
    messages: list[dict[str, Any]],
    max_chars: int | None = None,
    *,
    max_tokens: int | None = None,
    tool_schema: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Compress then evict complete rounds, retaining headroom and valid tool ordering."""
    if max_tokens is None and max_chars is None:
        raise ValueError("max_chars or max_tokens is required")
    schema_text = json.dumps(tool_schema or [], ensure_ascii=False, separators=(",", ":"))
    schema_cost = estimated_tokens(schema_text) if max_tokens is not None else len(schema_text)
    budget = max_tokens if max_tokens is not None else int(max_chars or 0)

    def current_size(value: list[dict[str, Any]]) -> int:
        history = (
            estimated_message_tokens(value)
            if max_tokens is not None
            else message_size(value)
        )
        return history + schema_cost

    if current_size(messages) <= budget or len(messages) <= 2:
        return messages, 0

    working = [dict(message) for message in messages]
    _compress_old_content(working, recent_exempt=10)
    if current_size(working) <= budget:
        return working, 0

    system = working[0]
    latest_user = max(
        (index for index, message in enumerate(working) if message.get("role") == "user"),
        default=1,
    )
    removed = max(0, latest_user - 1)
    working = [system, *working[latest_user:]]

    note = {
        "role": "system",
        "content": (
            "Older history was compressed or removed to fit the context budget. "
            "Use the working-memory anchor as the authoritative task state."
        ),
    }
    if removed:
        working.insert(1, note)

    _compress_old_content(working, recent_exempt=4)
    target = max(1, int(budget * 0.60))
    while current_size(working) > target:
        start = 3 if removed else 2
        if len(working) <= start + 1:
            break
        if working[start].get("role") != "assistant":
            del working[start]
            removed += 1
            continue

        end = start + 1
        while end < len(working) and working[end].get("role") == "tool":
            end += 1
        if working[start].get("tool_calls"):
            # Keep the latest complete decision/tool batch even if the target is unreachable.
            has_newer_state = any(
                message.get("role") == "assistant" and message.get("tool_calls")
                for message in working[end:]
            )
        else:
            has_newer_state = any(
                message.get("role") in {"assistant", "user"}
                for message in working[end:]
            )
        if not has_newer_state:
            break
        removed += end - start
        del working[start:end]
        if note not in working:
            working.insert(1, note)

    return working, removed
