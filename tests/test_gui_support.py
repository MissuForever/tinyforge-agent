from __future__ import annotations

import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path

from tinyforge.agent import AgentEvent, AgentResult
from tinyforge.config import Config
from tinyforge.gui_support import (
    MAX_UI_EVENT_CHARS,
    MAX_UI_STREAM_LINE,
    MAX_UI_TEXT,
    AgentWorker,
    GuiEventBridge,
    UiEnvelope,
    command_terminal_result,
    sanitize_agent_event,
    snapshot_text_file,
    summarize_tool_output,
)


class BlockingAgent:
    def __init__(self, on_event, *, fail: str | None = None) -> None:
        self.on_event = on_event
        self.fail = fail
        self.started = threading.Event()
        self.release = threading.Event()
        self.reset_count = 0

    def run(self, task, *, continue_session=False, cancel_event=None):
        self.on_event(AgentEvent("task_started", {"task": task}))
        self.started.set()
        if self.fail is not None:
            raise ValueError(self.fail)
        if cancel_event is not None:
            while not self.release.is_set() and not cancel_event.wait(0.01):
                pass
            cancelled = cancel_event.is_set()
        else:
            self.release.wait(2)
            cancelled = False
        return AgentResult(
            not cancelled,
            "Stopped" if cancelled else "Done",
            1,
            0,
            cancelled=cancelled,
        )

    def reset(self):
        self.reset_count += 1

    def memory_overview(self):
        return "persistent_memory_index: empty"

    def skills_overview(self):
        return "skills: enabled; available=1; loaded=0"


class GuiSupportTests(unittest.TestCase):
    def _config(self, workspace: Path) -> Config:
        return Config(
            api_key="test-key",
            base_url="https://example.test/v1",
            model="test-model",
            workspace=workspace,
            state_dir=workspace / "state",
        )

    def test_event_bridge_redacts_output_and_captures_file_diff(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            source = workspace / "source.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            events: queue.Queue[UiEnvelope] = queue.Queue()
            bridge = GuiEventBridge(events, "run-1", workspace)

            bridge(
                AgentEvent(
                    "tool_start",
                    {
                        "call_id": "edit-1",
                        "name": "edit_file",
                        "arguments": {"path": "source.py", "api_key": secret},
                    },
                )
            )
            source.write_text("VALUE = 2\n", encoding="utf-8")
            bridge(
                AgentEvent(
                    "tool_end",
                    {
                        "call_id": "edit-1",
                        "name": "edit_file",
                        "output": json.dumps(
                            {"ok": True, "result": {"path": "source.py", "token": secret}}
                        ),
                    },
                )
            )

            envelopes = [events.get(timeout=1) for _ in range(3)]
            serialized = repr(envelopes)
            self.assertNotIn(secret, serialized)
            self.assertEqual([item.kind for item in envelopes], ["event", "event", "file_diff"])
            diff = envelopes[-1].payload["diff"]
            self.assertIn("-VALUE = 1", diff)
            self.assertIn("+VALUE = 2", diff)

    def test_sensitive_and_outside_files_are_not_snapshotted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            (workspace / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            (workspace / ".env.local").write_text("TOKEN=secret\n", encoding="utf-8")
            (workspace / "id_rsa.backup").write_text("private key\n", encoding="utf-8")
            self.assertIsNone(snapshot_text_file(workspace, ".env"))
            self.assertIsNone(snapshot_text_file(workspace, ".env.local"))
            self.assertIsNone(snapshot_text_file(workspace, "id_rsa.backup"))
            self.assertIsNone(snapshot_text_file(workspace, "../outside.txt"))
            self.assertIsNone(snapshot_text_file(workspace, "bad\x00path.py"))

    def test_resolved_sensitive_symlink_is_not_snapshotted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            secret = workspace / ".env"
            alias = workspace / "settings.txt"
            secret.write_text("TOKEN=secret\n", encoding="utf-8")
            try:
                alias.symlink_to(secret)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"File symlinks are unavailable: {exc}")
            self.assertIsNone(snapshot_text_file(workspace, "settings.txt"))

    def test_event_sanitizer_bounds_nested_data_and_sensitive_fields(self) -> None:
        nested: dict[str, object] = {}
        cursor = nested
        for _ in range(20):
            child: dict[str, object] = {}
            cursor["child"] = child
            cursor = child
        event = AgentEvent(
            "memory_committed",
            {
                "auth": "basic-secret",
                "passwd": "password-secret",
                "passphrase": "phrase-secret",
                "nested": nested,
                "entries": [{"value": "x" * 1000} for _ in range(2000)],
            },
        )

        safe_event = sanitize_agent_event(event)
        serialized = json.dumps(safe_event.data)

        self.assertEqual(safe_event.data["auth"], "[REDACTED]")
        self.assertEqual(safe_event.data["passwd"], "[REDACTED]")
        self.assertEqual(safe_event.data["passphrase"], "[REDACTED]")
        self.assertIn("[TRUNCATED]", serialized)
        self.assertLess(len(serialized), MAX_UI_EVENT_CHARS + 10_000)

    def test_bounded_queue_applies_backpressure_without_losing_event(self) -> None:
        events: queue.Queue[UiEnvelope] = queue.Queue(maxsize=1)
        events.put(UiEnvelope("existing", "event", object()))
        with tempfile.TemporaryDirectory() as temp:
            bridge = GuiEventBridge(events, "run-bounded", Path(temp))
            producer = threading.Thread(
                target=bridge,
                args=(AgentEvent("assistant_text", {"text": "queued"}),),
                daemon=True,
            )
            producer.start()
            producer.join(0.05)
            self.assertTrue(producer.is_alive())
            events.get(timeout=1)
            producer.join(1)

        self.assertFalse(producer.is_alive())
        delivered = events.get(timeout=1)
        self.assertEqual(delivered.run_id, "run-bounded")
        self.assertEqual(delivered.payload.data["text"], "queued")

    def test_worker_rejects_parallel_run_and_delivers_terminal_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            events: queue.Queue[UiEnvelope] = queue.Queue()
            agents: list[BlockingAgent] = []

            def builder(config, on_event):
                agent = BlockingAgent(on_event)
                agents.append(agent)
                return agent

            worker = AgentWorker(events, builder=builder)
            self.assertEqual(
                worker.skills_overview(expected_workspace=workspace),
                "Skills have not been loaded for this workspace.",
            )
            run_id = worker.start(self._config(workspace), "First task", continue_session=False)
            self.assertIsNotNone(run_id)
            self.assertTrue(agents[0].started.wait(1))
            self.assertIn("after the current task", worker.skills_overview())
            self.assertIsNone(
                worker.start(self._config(workspace), "Second task", continue_session=True)
            )
            agents[0].release.set()

            first = events.get(timeout=1)
            terminal = events.get(timeout=1)
            self.assertEqual(first.kind, "event")
            self.assertEqual(terminal.kind, "result")
            self.assertEqual(first.run_id, run_id)
            self.assertEqual(terminal.run_id, run_id)
            self.assertTrue(terminal.payload.success)
            self.assertTrue(worker.is_running)
            self.assertIsNone(
                worker.start(self._config(workspace), "Too early", continue_session=True)
            )
            self.assertTrue(worker.acknowledge_terminal(run_id))
            self.assertFalse(worker.is_running)
            self.assertEqual(
                worker.skills_overview(expected_workspace=workspace),
                "skills: enabled; available=1; loaded=0",
            )

    def test_worker_cancel_is_forwarded_to_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            events: queue.Queue[UiEnvelope] = queue.Queue()
            agents: list[BlockingAgent] = []

            def builder(config, on_event):
                agent = BlockingAgent(on_event)
                agents.append(agent)
                return agent

            worker = AgentWorker(events, builder=builder)
            worker.start(self._config(workspace), "Cancelable task", continue_session=False)
            self.assertTrue(agents[0].started.wait(1))
            self.assertTrue(worker.cancel())
            events.get(timeout=1)
            terminal = events.get(timeout=1)
            self.assertEqual(terminal.kind, "result")
            self.assertTrue(terminal.payload.cancelled)
            self.assertTrue(worker.acknowledge_terminal(terminal.run_id))

    def test_worker_error_is_redacted(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            events: queue.Queue[UiEnvelope] = queue.Queue()

            def builder(config, on_event):
                return BlockingAgent(on_event, fail=f"request failed with {secret}")

            worker = AgentWorker(events, builder=builder)
            worker.start(self._config(workspace), "Fail safely", continue_session=False)
            events.get(timeout=1)
            terminal = events.get(timeout=1)
            self.assertEqual(terminal.kind, "error")
            self.assertNotIn(secret, str(terminal.payload))
            self.assertIn("REDACTED", str(terminal.payload))
            self.assertTrue(worker.acknowledge_terminal(terminal.run_id))

    def test_worker_redacts_and_bounds_terminal_answer(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"

        class LongAnswerAgent:
            def __init__(self, on_event) -> None:
                self.on_event = on_event

            def run(self, task, *, continue_session=False, cancel_event=None):
                return AgentResult(True, "x" * (MAX_UI_TEXT - 10) + secret, 1, 0)

        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            events: queue.Queue[UiEnvelope] = queue.Queue()
            worker = AgentWorker(
                events,
                builder=lambda config, on_event: LongAnswerAgent(on_event),
            )
            run_id = worker.start(
                self._config(workspace), "Bound the answer", continue_session=False
            )
            terminal = events.get(timeout=1)

        self.assertEqual(terminal.kind, "result")
        self.assertNotIn(secret, terminal.payload.answer)
        self.assertIn("REDACTED", terminal.payload.answer)
        self.assertLessEqual(len(terminal.payload.answer), MAX_UI_TEXT)
        self.assertTrue(worker.acknowledge_terminal(run_id))

    def test_tool_summary_preserves_exit_status(self) -> None:
        ok, summary = summarize_tool_output(
            json.dumps({"ok": True, "result": {"exit_code": 1, "stdout": "failed"}})
        )
        self.assertFalse(ok)
        self.assertEqual(summary, "exit=1; failed")

    def test_command_terminal_result_preserves_status_and_handles_bad_payloads(self) -> None:
        success = command_terminal_result(
            json.dumps(
                {
                    "ok": True,
                    "result": {
                        "exit_code": 7,
                        "stdout": "partial output\n",
                        "stderr": "failed\n",
                        "truncated": True,
                    },
                }
            )
        )
        self.assertEqual(success["exit_code"], 7)
        self.assertEqual(success["stdout"], "partial output\n")
        self.assertEqual(success["stderr"], "failed\n")
        self.assertTrue(success["truncated"])

        cases = (
            ("not-json", False),
            ("[]", False),
            (json.dumps({"ok": True}), False),
            (json.dumps({"ok": False, "error": "blocked"}), True),
        )
        for raw, parsed in cases:
            with self.subTest(raw=raw):
                result = command_terminal_result(raw)
                self.assertEqual(result["parsed"], parsed)
                self.assertIn("error", result)

        boolean_exit = command_terminal_result(
            json.dumps({"ok": True, "result": {"exit_code": True}})
        )
        self.assertNotIn("exit_code", boolean_exit)

        timeout = command_terminal_result(
            json.dumps(
                {
                    "ok": False,
                    "error": (
                        "Command timed out after 1s. Partial stdout: already streamed\n"
                        "Partial stderr: also streamed"
                    ),
                }
            )
        )
        self.assertEqual(timeout["error"], "Command timed out after 1s.")

        cancelled = command_terminal_result(
            json.dumps(
                {
                    "ok": False,
                    "cancelled": True,
                    "error": "Command cancelled by user",
                }
            )
        )
        self.assertTrue(cancelled["cancelled"])

    def test_command_event_redacts_cli_secret_flags(self) -> None:
        secret = "plain-command-line-secret"
        safe = sanitize_agent_event(
            AgentEvent(
                "tool_start",
                {
                    "call_id": "secret-command",
                    "name": "run_command",
                    "arguments": {
                        "command": f"tool --proxy-password {secret}",
                        "cwd": ".",
                    },
                },
            )
        )
        rendered = safe.data["arguments"]["command"]
        self.assertNotIn(secret, rendered)
        self.assertIn("--proxy-password [REDACTED]", rendered)

    def test_command_event_marks_a_truncated_command_explicitly(self) -> None:
        command = "head " + "x" * (MAX_UI_TEXT + 1_000) + " tail"
        safe = sanitize_agent_event(
            AgentEvent(
                "tool_start",
                {
                    "call_id": "long-command",
                    "name": "run_command",
                    "arguments": {"command": command, "cwd": "."},
                },
            )
        )
        rendered = safe.data["arguments"]["command"]
        self.assertLessEqual(len(rendered), MAX_UI_TEXT)
        self.assertIn("head ", rendered)
        self.assertIn(" tail", rendered)
        self.assertIn("[command truncated]", rendered)

    def test_command_terminal_result_redacts_and_bounds_streams(self) -> None:
        api_key = "sk-abcdefghijklmnopqrstuvwxyz123456"
        bearer = "bearer-token-abcdefghijklmnop"
        password = "database-password"
        result = command_terminal_result(
            json.dumps(
                {
                    "ok": True,
                    "result": {
                        "exit_code": 0,
                        "stdout": (
                            f"OPENAI_API_KEY={api_key}\n"
                            f"Authorization: Bearer {bearer}\n" + "x" * 30_000
                        ),
                        "stderr": (
                            f"postgres://user:{password}@host/db\n" + "y" * 30_000
                        ),
                    },
                }
            )
        )
        serialized = json.dumps(result)
        self.assertNotIn(api_key, serialized)
        self.assertNotIn(bearer, serialized)
        self.assertNotIn(password, serialized)
        self.assertIn("REDACTED", serialized)
        self.assertLessEqual(len(result["stdout"]) + len(result["stderr"]), MAX_UI_TEXT * 2)

    def test_bridge_sanitizes_stream_events_and_only_adds_command_result(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        events: queue.Queue[UiEnvelope] = queue.Queue()
        with tempfile.TemporaryDirectory() as temp:
            bridge = GuiEventBridge(events, "run-stream", Path(temp))
            bridge(
                AgentEvent(
                    "tool_output",
                    {
                        "call_id": "command-1",
                        "name": "run_command",
                        "stream": "stdout",
                        "text": secret + "x" * (MAX_UI_TEXT + 100),
                    },
                )
            )
            bridge(
                AgentEvent(
                    "tool_end",
                    {
                        "call_id": "command-1",
                        "name": "run_command",
                        "output": json.dumps(
                            {"ok": True, "result": {"exit_code": 0, "stdout": "done"}}
                        ),
                    },
                )
            )
            bridge(
                AgentEvent(
                    "tool_end",
                    {
                        "call_id": "read-1",
                        "name": "read_file",
                        "output": json.dumps({"ok": True, "result": {"path": "a.txt"}}),
                    },
                )
            )

        streamed = events.get(timeout=1).payload
        command_end = events.get(timeout=1).payload
        read_end = events.get(timeout=1).payload
        self.assertEqual(streamed.data["call_id"], "command-1")
        self.assertEqual(streamed.data["stream"], "stdout")
        self.assertNotIn(secret, streamed.data["text"])
        self.assertLessEqual(len(streamed.data["text"]), MAX_UI_TEXT)
        self.assertEqual(command_end.data["terminal_result"]["exit_code"], 0)
        self.assertNotIn("terminal_result", read_end.data)

    def test_bridge_redacts_secrets_split_across_stream_events(self) -> None:
        first_half = "ABCDEFGHIJKL"
        second_half = "MNOPQRSTUVWX"
        events: queue.Queue[UiEnvelope] = queue.Queue()
        with tempfile.TemporaryDirectory() as temp:
            bridge = GuiEventBridge(events, "run-split-secret", Path(temp))
            bridge(
                AgentEvent(
                    "tool_output",
                    {
                        "call_id": "command-secret",
                        "name": "run_command",
                        "stream": "stdout",
                        "text": f"Authorization: Bearer {first_half}",
                    },
                )
            )
            self.assertTrue(events.empty())
            bridge(
                AgentEvent(
                    "tool_output",
                    {
                        "call_id": "command-secret",
                        "name": "run_command",
                        "stream": "stdout",
                        "text": second_half + "\n",
                    },
                )
            )

        event = events.get(timeout=1).payload
        rendered = event.data["text"]
        self.assertIn("REDACTED", rendered)
        self.assertNotIn(first_half, rendered)
        self.assertNotIn(second_half, rendered)

    def test_bridge_batches_many_complete_lines_from_one_chunk(self) -> None:
        output = "".join(f"line {index:04d}\n" for index in range(5_000))
        events: queue.Queue[UiEnvelope] = queue.Queue()
        with tempfile.TemporaryDirectory() as temp:
            bridge = GuiEventBridge(events, "run-batched", Path(temp))
            bridge(
                AgentEvent(
                    "tool_output",
                    {
                        "call_id": "command-batched",
                        "name": "run_command",
                        "stream": "stdout",
                        "text": output,
                    },
                )
            )

        rendered = []
        while not events.empty():
            event = events.get_nowait().payload
            self.assertLessEqual(len(event.data["text"]), MAX_UI_TEXT)
            rendered.append(event.data["text"])
        self.assertLessEqual(len(rendered), 3)
        self.assertEqual("".join(rendered), output)

    def test_saturated_queue_does_not_block_terminal_progress(self) -> None:
        events: queue.Queue[UiEnvelope] = queue.Queue(maxsize=1)
        events.put(UiEnvelope("existing", "event", object()))
        with tempfile.TemporaryDirectory() as temp:
            bridge = GuiEventBridge(events, "run-busy", Path(temp))
            producer = threading.Thread(
                target=bridge,
                args=(
                    AgentEvent(
                        "tool_output",
                        {
                            "call_id": "command-busy",
                            "name": "run_command",
                            "stream": "stdout",
                            "text": "first line\n",
                        },
                    ),
                ),
                daemon=True,
            )
            producer.start()
            producer.join(1)

        self.assertFalse(producer.is_alive())
        self.assertEqual(events.qsize(), 1)

    def test_bridge_reports_busy_queue_omission_before_command_end(self) -> None:
        events: queue.Queue[UiEnvelope] = queue.Queue(maxsize=2)
        events.put(UiEnvelope("existing-1", "event", object()))
        events.put(UiEnvelope("existing-2", "event", object()))
        with tempfile.TemporaryDirectory() as temp:
            bridge = GuiEventBridge(events, "run-busy-end", Path(temp))
            bridge(
                AgentEvent(
                    "tool_output",
                    {
                        "call_id": "command-busy-end",
                        "name": "run_command",
                        "stream": "stderr",
                        "text": "dropped line\n",
                    },
                )
            )
            events.get_nowait()
            events.get_nowait()
            bridge(
                AgentEvent(
                    "tool_output",
                    {
                        "call_id": "command-busy-end",
                        "name": "run_command",
                        "stream": "stderr",
                        "text": "also dropped\n",
                    },
                )
            )
            self.assertTrue(events.empty())
            bridge(
                AgentEvent(
                    "tool_end",
                    {
                        "call_id": "command-busy-end",
                        "name": "run_command",
                        "output": json.dumps(
                            {"ok": True, "result": {"exit_code": 0, "stderr": ""}}
                        ),
                    },
                )
            )

        omitted = events.get_nowait().payload
        finished = events.get_nowait().payload
        self.assertEqual(omitted.kind, "tool_output")
        self.assertEqual(omitted.data["stream"], "stderr")
        self.assertEqual(omitted.data["text"], "[output omitted: GUI queue was busy]\n")
        self.assertEqual(finished.kind, "tool_end")
        self.assertEqual(bridge._dropped_terminal_streams, set())

    def test_bridge_flushes_safe_partial_line_before_command_end(self) -> None:
        events: queue.Queue[UiEnvelope] = queue.Queue()
        with tempfile.TemporaryDirectory() as temp:
            bridge = GuiEventBridge(events, "run-partial", Path(temp))
            bridge(
                AgentEvent(
                    "tool_output",
                    {
                        "call_id": "command-partial",
                        "name": "run_command",
                        "stream": "stdout",
                        "text": "ready without newline",
                    },
                )
            )
            self.assertTrue(events.empty())
            bridge(
                AgentEvent(
                    "tool_end",
                    {
                        "call_id": "command-partial",
                        "name": "run_command",
                        "output": json.dumps(
                            {
                                "ok": True,
                                "result": {"exit_code": 0, "stdout": "ready without newline"},
                            }
                        ),
                    },
                )
            )

        progress = events.get(timeout=1).payload
        finished = events.get(timeout=1).payload
        self.assertEqual(progress.kind, "tool_output")
        self.assertEqual(progress.data["text"], "ready without newline")
        self.assertEqual(finished.kind, "tool_end")

    def test_bridge_omits_oversized_lines_and_clears_discard_state(self) -> None:
        events: queue.Queue[UiEnvelope] = queue.Queue()
        with tempfile.TemporaryDirectory() as temp:
            bridge = GuiEventBridge(events, "run-oversized", Path(temp))
            bridge(
                AgentEvent(
                    "tool_output",
                    {
                        "call_id": "command-large-line",
                        "name": "run_command",
                        "stream": "stdout",
                        "text": "x" * (MAX_UI_STREAM_LINE + 1),
                    },
                )
            )
            bridge(
                AgentEvent(
                    "tool_output",
                    {
                        "call_id": "command-large-line",
                        "name": "run_command",
                        "stream": "stdout",
                        "text": "discarded remainder\nvisible next line\n",
                    },
                )
            )
            bridge(
                AgentEvent(
                    "tool_end",
                    {
                        "call_id": "command-large-line",
                        "name": "run_command",
                        "output": json.dumps(
                            {"ok": True, "result": {"exit_code": 0, "stdout": ""}}
                        ),
                    },
                )
            )

            omitted = events.get(timeout=1).payload
            visible = events.get(timeout=1).payload
            finished = events.get(timeout=1).payload
            self.assertIn("output line omitted", omitted.data["text"])
            self.assertEqual(visible.data["text"], "visible next line\n")
            self.assertEqual(finished.kind, "tool_end")
            self.assertEqual(bridge._terminal_lines, {})
            self.assertEqual(bridge._discarding_terminal_lines, set())

    def test_duplicate_command_start_discards_stale_partial_output(self) -> None:
        events: queue.Queue[UiEnvelope] = queue.Queue()
        stale = "Authorization: Bearer stale-partial-token"
        with tempfile.TemporaryDirectory() as temp:
            bridge = GuiEventBridge(events, "run-duplicate", Path(temp))
            start = AgentEvent(
                "tool_start",
                {
                    "call_id": "duplicate-command",
                    "name": "run_command",
                    "arguments": {"command": "verify", "cwd": "."},
                },
            )
            bridge(start)
            bridge(
                AgentEvent(
                    "tool_output",
                    {
                        "call_id": "duplicate-command",
                        "name": "run_command",
                        "stream": "stdout",
                        "text": stale,
                    },
                )
            )
            bridge(start)
            bridge(
                AgentEvent(
                    "tool_output",
                    {
                        "call_id": "duplicate-command",
                        "name": "run_command",
                        "stream": "stdout",
                        "text": "fresh output\n",
                    },
                )
            )

        rendered = [events.get(timeout=1).payload for _ in range(3)]
        serialized = "\n".join(str(event.data) for event in rendered)
        self.assertNotIn(stale, serialized)
        self.assertEqual(rendered[-1].data["text"], "fresh output\n")
        self.assertEqual(bridge._terminal_lines, {})

    def test_large_tool_output_keeps_structured_success_status(self) -> None:
        output = json.dumps(
            {"ok": True, "result": {"exit_code": 0, "stdout": "x" * 25_000}}
        )
        events: queue.Queue[UiEnvelope] = queue.Queue()
        with tempfile.TemporaryDirectory() as temp:
            bridge = GuiEventBridge(events, "run-large", Path(temp))
            bridge(
                AgentEvent(
                    "tool_end",
                    {"call_id": "large", "name": "run_command", "output": output},
                )
            )
        event = events.get(timeout=1).payload
        self.assertTrue(event.data["output_ok"])
        self.assertTrue(event.data["output_summary"].startswith("exit=0"))
        self.assertLessEqual(len(event.data["output"]), 20_000)
        self.assertTrue(event.data["terminal_result"]["parsed"])
        self.assertEqual(event.data["terminal_result"]["exit_code"], 0)

    def test_redaction_happens_before_ui_text_is_truncated(self) -> None:
        value = "x" * 19_980 + " postgres://user:password-super-secret@host/db"
        events: queue.Queue[UiEnvelope] = queue.Queue()
        with tempfile.TemporaryDirectory() as temp:
            bridge = GuiEventBridge(events, "run-secret", Path(temp))
            bridge(AgentEvent("assistant_text", {"text": value}))
        safe_text = events.get(timeout=1).payload.data["text"]
        self.assertNotIn("password-super-secret", safe_text)
        self.assertIn("REDACTED", safe_text)


if __name__ == "__main__":
    unittest.main()
