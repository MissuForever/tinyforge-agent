from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from tinyforge.model import ModelError, OpenAICompatibleClient


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return self.body


class ModelClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = OpenAICompatibleClient(
            api_key="test-key",
            base_url="https://example.test/v1",
            model="test-model",
        )

    @patch("tinyforge.model.request.urlopen")
    def test_parses_text_and_tool_calls(self, urlopen) -> None:
        urlopen.return_value = FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "Checking the file.",
                            "tool_calls": [
                                {
                                    "id": "abc",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"main.py"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        )
        reply = self.client.complete([{"role": "user", "content": "fix"}], [])
        self.assertEqual(reply.content, "Checking the file.")
        self.assertEqual(reply.tool_calls[0].name, "read_file")
        request_object = urlopen.call_args.args[0]
        sent = json.loads(request_object.data)
        self.assertEqual(sent["model"], "test-model")
        self.assertEqual(request_object.full_url, "https://example.test/v1/chat/completions")

    @patch("tinyforge.model.request.urlopen")
    def test_unexpected_response_is_reported(self, urlopen) -> None:
        urlopen.return_value = FakeResponse({"unexpected": True})
        with self.assertRaises(ModelError):
            self.client.complete([], [])


if __name__ == "__main__":
    unittest.main()
