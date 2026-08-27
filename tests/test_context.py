from __future__ import annotations

import unittest

from tinyforge.context import (
    compact_messages,
    estimated_message_tokens,
    estimated_tokens,
    message_size,
)


class ContextTests(unittest.TestCase):
    def test_small_history_is_unchanged(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
        ]
        compacted, removed = compact_messages(messages, 10_000)
        self.assertIs(compacted, messages)
        self.assertEqual(removed, 0)

    def test_old_complete_tool_round_is_removed(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "x" * 500},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "2"}]},
            {"role": "tool", "tool_call_id": "2", "content": "recent"},
            {"role": "assistant", "content": "done"},
        ]
        compacted, removed = compact_messages(messages, 350)
        self.assertGreater(removed, 0)
        self.assertLess(message_size(compacted), message_size(messages))
        self.assertFalse(any(message.get("tool_call_id") == "1" for message in compacted))
        self.assertTrue(any(message.get("tool_call_id") == "2" for message in compacted))

    def test_cjk_text_has_more_conservative_token_estimate(self) -> None:
        self.assertGreater(estimated_tokens("测试中文上下文"), estimated_tokens("abcdefg"))

    def test_tool_schema_counts_toward_token_budget(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "old" * 500},
            {"role": "assistant", "content": "latest"},
        ]
        schema = [
            {
                "type": "function",
                "function": {"name": "large", "description": "x" * 2000},
            }
        ]
        compacted, _ = compact_messages(messages, max_tokens=400, tool_schema=schema)
        self.assertLess(estimated_message_tokens(compacted), estimated_message_tokens(messages))


if __name__ == "__main__":
    unittest.main()
