from __future__ import annotations

import unittest

from tinyforge.context import compact_messages, message_size


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


if __name__ == "__main__":
    unittest.main()
