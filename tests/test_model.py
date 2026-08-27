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

    @patch("tinyforge.model.request.urlopen")
    def test_responses_api_request_and_tool_result_conversion(self, urlopen) -> None:
        client = OpenAICompatibleClient(
            api_key="test-key",
            base_url="https://example.test/v1",
            model="reasoning-model",
            wire_api="responses",
            reasoning_effort="xhigh",
            store=False,
        )
        urlopen.return_value = FakeResponse(
            {
                "output": [
                    {"type": "reasoning", "id": "reasoning_1", "summary": []},
                    {
                        "type": "function_call",
                        "id": "item_1",
                        "call_id": "call_1",
                        "name": "read_file",
                        "arguments": '{"path":"main.py"}',
                    },
                ]
            }
        )
        messages = [
            {"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "Read main.py"},
            {
                "role": "assistant",
                "content": "Reading it.",
                "tool_calls": [
                    {
                        "id": "prior_call",
                        "type": "function",
                        "function": {"name": "list_files", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "prior_call", "content": '{"ok":true}'},
        ]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                },
            }
        ]

        reply = client.complete(messages, tools)

        self.assertEqual(reply.tool_calls[0].id, "call_1")
        self.assertEqual(reply.tool_calls[0].name, "read_file")
        request_object = urlopen.call_args.args[0]
        payload = json.loads(request_object.data)
        self.assertEqual(request_object.full_url, "https://example.test/v1/responses")
        self.assertEqual(payload["reasoning"], {"effort": "xhigh"})
        self.assertFalse(payload["store"])
        self.assertEqual(payload["tools"][0]["name"], "read_file")
        self.assertNotIn("function", payload["tools"][0])
        self.assertEqual(payload["input"][-2]["type"], "function_call")
        self.assertEqual(payload["input"][-1]["type"], "function_call_output")
        self.assertEqual(payload["input"][-1]["call_id"], "prior_call")

    @patch("tinyforge.model.request.urlopen")
    def test_responses_api_parses_final_text(self, urlopen) -> None:
        client = OpenAICompatibleClient(
            api_key="test-key",
            base_url="https://example.test/v1/responses",
            model="reasoning-model",
            wire_api="responses",
        )
        urlopen.return_value = FakeResponse(
            {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Task complete."}],
                    }
                ]
            }
        )
        reply = client.complete([{"role": "user", "content": "finish"}], [])
        self.assertEqual(reply.content, "Task complete.")
        self.assertEqual(reply.tool_calls, ())
        self.assertEqual(urlopen.call_args.args[0].full_url, "https://example.test/v1/responses")


if __name__ == "__main__":
    unittest.main()
