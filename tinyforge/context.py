"""Conversation-size accounting and structural compaction."""

from __future__ import annotations

import json
from typing import Any


def message_size(messages: list[dict[str, Any]]) -> int:
    return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))


def compact_messages(
    messages: list[dict[str, Any]], max_chars: int
) -> tuple[list[dict[str, Any]], int]:
    """Drop old complete turns/tool rounds while preserving valid tool-call ordering."""
    if message_size(messages) <= max_chars or len(messages) <= 2:
        return messages, 0

    system = messages[0]
    latest_user = max(
        (index for index, message in enumerate(messages) if message.get("role") == "user"),
        default=1,
    )
    removed = max(0, latest_user - 1)
    working = [system, *messages[latest_user:]]

    note = {
        "role": "system",
        "content": "Older conversation and tool output were removed to fit the context budget.",
    }
    if removed:
        working.insert(1, note)

    while message_size(working) > max_chars:
        start = 3 if removed else 2
        if len(working) <= start + 2:
            break
        if working[start].get("role") != "assistant":
            del working[start]
            removed += 1
            continue

        end = start + 1
        while end < len(working) and working[end].get("role") == "tool":
            end += 1
        if end >= len(working):
            break
        removed += end - start
        del working[start:end]
        if note not in working:
            working.insert(1, note)

    return working, removed
