from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class ScriptedHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    paths: list[str] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request_body = json.loads(self.rfile.read(length))
        type(self).requests.append(request_body)
        type(self).paths.append(self.path)
        is_responses = "input" in request_body
        if is_responses and len(type(self).requests) == 1:
            payload = {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "write_1",
                        "name": "write_file",
                        "arguments": json.dumps(
                            {"path": "from_responses.txt", "content": "Responses loop works\n"}
                        ),
                    }
                ]
            }
        elif is_responses:
            payload = {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "Responses task complete."}
                        ],
                    }
                ]
            }
        elif len(type(self).requests) == 1:
            message = {
                "role": "assistant",
                "content": "Creating the requested file.",
                "tool_calls": [
                    {
                        "id": "write_1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {"path": "from_http.txt", "content": "HTTP loop works\n"}
                            ),
                        },
                    }
                ],
            }
            payload = {"choices": [{"message": message}]}
        else:
            message = {
                "role": "assistant",
                "content": "Created from_http.txt successfully.",
            }
            payload = {"choices": [{"message": message}]}
        response = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        return


class CliIntegrationTests(unittest.TestCase):
    def test_cli_completes_real_http_tool_loop(self) -> None:
        ScriptedHandler.requests = []
        ScriptedHandler.paths = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        project_root = Path(__file__).resolve().parents[1]
        try:
            with tempfile.TemporaryDirectory() as workspace:
                environment = os.environ.copy()
                environment["TINYFORGE_API_KEY"] = "integration-test-key"
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tinyforge",
                        "--no-color",
                        "--base-url",
                        f"http://127.0.0.1:{server.server_port}/v1",
                        "--model",
                        "scripted-model",
                        "--wire-api",
                        "chat_completions",
                        "--workspace",
                        workspace,
                        "Create from_http.txt",
                    ],
                    cwd=project_root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                    check=False,
                )
                generated = Path(workspace) / "from_http.txt"
                self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
                self.assertEqual(generated.read_text(encoding="utf-8"), "HTTP loop works\n")
                self.assertIn("[tool] write_file", completed.stdout)
                self.assertIn("[final] Created from_http.txt successfully.", completed.stdout)
                self.assertEqual(len(ScriptedHandler.requests), 2)
                second_messages = ScriptedHandler.requests[1]["messages"]
                self.assertEqual(second_messages[-1]["role"], "tool")
                self.assertTrue(json.loads(second_messages[-1]["content"])["ok"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_cli_completes_responses_api_tool_loop(self) -> None:
        ScriptedHandler.requests = []
        ScriptedHandler.paths = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        project_root = Path(__file__).resolve().parents[1]
        try:
            with tempfile.TemporaryDirectory() as workspace:
                environment = os.environ.copy()
                environment["TINYFORGE_API_KEY"] = "integration-test-key"
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tinyforge",
                        "--no-color",
                        "--base-url",
                        f"http://127.0.0.1:{server.server_port}/v1",
                        "--model",
                        "scripted-model",
                        "--wire-api",
                        "responses",
                        "--reasoning-effort",
                        "xhigh",
                        "--workspace",
                        workspace,
                        "Create from_responses.txt",
                    ],
                    cwd=project_root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                    check=False,
                )
                generated = Path(workspace) / "from_responses.txt"
                self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
                self.assertEqual(generated.read_text(encoding="utf-8"), "Responses loop works\n")
                self.assertIn("[final] Responses task complete.", completed.stdout)
                self.assertEqual(ScriptedHandler.paths, ["/v1/responses", "/v1/responses"])
                self.assertEqual(ScriptedHandler.requests[0]["reasoning"], {"effort": "xhigh"})
                self.assertFalse(ScriptedHandler.requests[0]["store"])
                second_input = ScriptedHandler.requests[1]["input"]
                self.assertEqual(second_input[-1]["type"], "function_call_output")
                self.assertEqual(second_input[-1]["call_id"], "write_1")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
