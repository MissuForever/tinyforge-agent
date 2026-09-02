from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

# The platform must be selected before importing PySide6. This keeps the widget
# tests deterministic on developer machines and headless CI runners alike.
os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QApplication, QMessageBox, QPlainTextEdit
except ImportError:
    Qt = None
    QPalette = None
    QApplication = None
    QMessageBox = None
    QPlainTextEdit = None

if QApplication is not None:
    from tinyforge.agent import AgentEvent, AgentResult
    from tinyforge.config import Config
    from tinyforge.gui import TinyForgeApp
    from tinyforge.gui_support import AgentWorker
else:
    AgentEvent = None
    AgentResult = None
    Config = None
    AgentWorker = None
    TinyForgeApp = None


class ImmediateAgent:
    def __init__(self, on_event) -> None:
        self.on_event = on_event

    def run(self, task, *, continue_session=False, cancel_event=None):
        assert AgentEvent is not None
        assert AgentResult is not None
        self.on_event(AgentEvent("task_started", {"task": task}))
        self.on_event(AgentEvent("model_start", {"round": 1}))
        self.on_event(
            AgentEvent(
                "model_end",
                {
                    "round": 1,
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "cached_input_tokens": 0,
                    "elapsed_ms": 2,
                },
            )
        )
        return AgentResult(True, "GUI loop completed", 1, 0, 10, 4, elapsed_ms=2)

    def reset(self):
        return None

    def memory_overview(self):
        return "persistent_memory_index: empty"


class CommandAgent(ImmediateAgent):
    def run(self, task, *, continue_session=False, cancel_event=None):
        assert AgentEvent is not None
        assert AgentResult is not None
        command = "python -m unittest"
        secret = "ABCDEFGHIJKLMNOPQRSTUVWX"
        self.on_event(AgentEvent("task_started", {"task": task}))
        self.on_event(
            AgentEvent(
                "tool_start",
                {
                    "call_id": "command-1",
                    "name": "run_command",
                    "arguments": {"command": command, "cwd": "."},
                },
            )
        )
        self.on_event(
            AgentEvent(
                "tool_output",
                {
                    "call_id": "command-1",
                    "name": "run_command",
                    "stream": "stdout",
                    "text": "Ran 2 tests\n",
                },
            )
        )
        self.on_event(
            AgentEvent(
                "tool_output",
                {
                    "call_id": "command-1",
                    "name": "run_command",
                    "stream": "stderr",
                    "text": f"Authorization: Bearer {secret[:12]}",
                },
            )
        )
        self.on_event(
            AgentEvent(
                "tool_output",
                {
                    "call_id": "command-1",
                    "name": "run_command",
                    "stream": "stderr",
                    "text": secret[12:] + "\n",
                },
            )
        )
        output = json.dumps(
            {
                "ok": True,
                "result": {
                    "command": command,
                    "cwd": ".",
                    "exit_code": 0,
                    "stdout": "Ran 2 tests\n",
                    "stderr": f"Authorization: Bearer {secret}\n",
                },
            }
        )
        self.on_event(
            AgentEvent(
                "tool_end",
                {
                    "call_id": "command-1",
                    "name": "run_command",
                    "output": output,
                },
            )
        )
        return AgentResult(True, "Command completed", 1, 1, elapsed_ms=2)


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class GuiWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert QApplication is not None
        cls.qt_app = QApplication.instance() or QApplication(["tinyforge-gui-tests"])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(
            os.environ,
            {
                "TINYFORGE_API_KEY": "test-key",
                "TINYFORGE_BASE_URL": "https://example.test/v1",
                "TINYFORGE_MODEL": "test-model",
                "TINYFORGE_STATE_DIR": str(Path(self.temp.name) / "state"),
            },
            clear=False,
        )
        self.environment.start()
        assert TinyForgeApp is not None
        self.app = TinyForgeApp(Path(self.temp.name))
        self.app.show()
        self.qt_app.processEvents()

    def tearDown(self) -> None:
        if hasattr(self, "app"):
            if not self.app._closed:
                self.app._destroy_window()
            self.app.deleteLater()
            self.qt_app.processEvents()
        if hasattr(self, "environment"):
            self.environment.stop()
        if hasattr(self, "temp"):
            self.temp.cleanup()

    def _clear_timeline(self) -> None:
        self.app.timeline.clear()
        self.app._timeline_items.clear()
        self.app._entry_details.clear()
        self.app._active_items.clear()

    def _seed_running_rows(self) -> None:
        self.app._current_task_text = "Inspect the project"
        self.app._task_item = self.app._insert_timeline(
            "Queued",
            "Task",
            self.app._current_task_text,
            self.app._current_task_text,
            "running",
        )
        active = self.app._insert_timeline(
            "Running", "Model", "Round 1", "Waiting for model", "running"
        )
        self.app._active_items.add(active)

    def _timeline_states(self) -> list[str]:
        return [
            self.app.timeline.topLevelItem(index).text(0)
            for index in range(self.app.timeline.topLevelItemCount())
        ]

    def _drain_worker(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while self.app.worker.is_running and time.monotonic() < deadline:
            self.app._drain_queue()
            self.qt_app.processEvents()
            time.sleep(0.005)
        self.app._drain_queue()
        self.qt_app.processEvents()

    def test_all_result_states_clear_queued_and_running_rows(self) -> None:
        assert AgentResult is not None
        cases = (
            (AgentResult(True, "Done", 1, 0), "Completed"),
            (AgentResult(False, "Blocked", 1, 0), "Blocked"),
            (AgentResult(False, "Stopped", 1, 0, cancelled=True), "Stopped"),
        )
        for result, expected in cases:
            with self.subTest(expected=expected):
                self._clear_timeline()
                self._seed_running_rows()
                self.app._reset_terminal(Path(self.temp.name).resolve())
                self.app._start_terminal_command("pending", {"command": "wait"})
                self.app._finish_result(result)
                states = self._timeline_states()
                self.assertIn(expected, states)
                self.assertNotIn("Queued", states)
                self.assertNotIn("Running", states)
                terminal_state = "[stopped]" if result.cancelled else "[interrupted]"
                self.assertIn(terminal_state, self.app.terminal_text.toPlainText())

    def test_runtime_error_clears_active_rows(self) -> None:
        self._seed_running_rows()
        self.app._start_terminal_command("pending", {"command": "wait"})
        self.app._finish_error("network unavailable")
        states = self._timeline_states()
        self.assertIn("Error", states)
        self.assertNotIn("Queued", states)
        self.assertNotIn("Running", states)
        self.assertIn("[interrupted]", self.app.terminal_text.toPlainText())

    def test_diff_view_has_horizontal_scroll_and_task_controls_are_managed(self) -> None:
        assert Qt is not None
        assert QPalette is not None
        assert QPlainTextEdit is not None
        self.assertNotEqual(
            self.app.changes_text.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        self.assertEqual(
            self.app.changes_text.lineWrapMode(),
            QPlainTextEdit.LineWrapMode.NoWrap,
        )
        self.assertTrue(self.app.task_input.isVisibleTo(self.app))
        self.assertTrue(self.app.run_button.isVisibleTo(self.app))

        palette = self.app.timeline.palette()
        selected_background = palette.color(QPalette.ColorRole.Highlight)
        selected_text = palette.color(QPalette.ColorRole.HighlightedText)
        self.assertTrue(selected_background.isValid())
        self.assertTrue(selected_text.isValid())
        self.assertNotEqual(selected_background.rgba(), selected_text.rgba())

    def test_terminal_is_a_read_only_fourth_inspector_tab(self) -> None:
        assert Qt is not None
        assert QPlainTextEdit is not None
        self.assertEqual(self.app.inspector.count(), 4)
        self.assertEqual(self.app.inspector.tabText(3), "Terminal")
        self.assertIs(self.app.inspector.widget(3), self.app.terminal_text)
        self.assertTrue(self.app.terminal_text.isReadOnly())
        self.assertFalse(self.app.terminal_text.isUndoRedoEnabled())
        self.assertEqual(
            self.app.terminal_text.lineWrapMode(),
            QPlainTextEdit.LineWrapMode.NoWrap,
        )
        self.assertNotEqual(
            self.app.terminal_text.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )

    def test_terminal_renders_streams_exit_status_without_duplicate_output(self) -> None:
        assert AgentEvent is not None
        start = AgentEvent(
            "tool_start",
            {
                "call_id": "command-1",
                "name": "run_command",
                "arguments": {"command": "python -m unittest", "cwd": "tests"},
            },
        )
        self.app._render_agent_event(start)
        self.assertIs(self.app.inspector.currentWidget(), self.app.terminal_text)
        self.assertEqual(self.app.inspector.tabText(3), "Terminal (1)")
        prompt = "PS tests> " if os.name == "nt" else "$ tests> "
        self.assertIn(prompt + "python -m unittest", self.app.terminal_text.toPlainText())

        self.app._render_agent_event(
            AgentEvent(
                "tool_output",
                {
                    "call_id": "command-1",
                    "name": "run_command",
                    "stream": "stdout",
                    "text": "test passed\n",
                },
            )
        )
        self.app._render_agent_event(
            AgentEvent(
                "tool_output",
                {
                    "call_id": "command-1",
                    "name": "run_command",
                    "stream": "stderr",
                    "text": "warning one\nwarning two\n",
                },
            )
        )
        final_output = json.dumps(
            {
                "ok": True,
                "result": {
                    "exit_code": 7,
                    "stdout": "test passed\n",
                    "stderr": "warning one\nwarning two\n",
                },
            }
        )
        self.app._render_agent_event(
            AgentEvent(
                "tool_end",
                {
                    "call_id": "command-1",
                    "name": "run_command",
                    "output": final_output,
                    "terminal_result": {
                        "parsed": True,
                        "ok": True,
                        "exit_code": 7,
                        "stdout": "test passed\n",
                        "stderr": "warning one\nwarning two\n",
                    },
                },
            )
        )
        rendered = self.app.terminal_text.toPlainText()
        self.assertEqual(rendered.count("test passed"), 1)
        self.assertEqual(rendered.count("warning one"), 1)
        self.assertIn("[stderr] warning one", rendered)
        self.assertIn("[stderr] warning two", rendered)
        self.assertIn("[exit 7]", rendered)
        self.assertEqual(self.app._terminal_commands, {})

        self.app.inspector.setCurrentWidget(self.app.details_text)
        self.app._render_agent_event(
            AgentEvent(
                "tool_start",
                {
                    "call_id": "command-2",
                    "name": "run_command",
                    "arguments": {"command": "echo second", "cwd": "."},
                },
            )
        )
        self.assertIs(self.app.inspector.currentWidget(), self.app.details_text)

    def test_terminal_backfills_only_a_stream_missing_from_progress(self) -> None:
        assert AgentEvent is not None
        self.app._render_agent_event(
            AgentEvent(
                "tool_start",
                {
                    "call_id": "partial-progress",
                    "name": "run_command",
                    "arguments": {"command": "check", "cwd": "."},
                },
            )
        )
        self.app._render_agent_event(
            AgentEvent(
                "tool_output",
                {
                    "call_id": "partial-progress",
                    "name": "run_command",
                    "stream": "stdout",
                    "text": "streamed stdout\n",
                },
            )
        )
        self.app._render_agent_event(
            AgentEvent(
                "tool_end",
                {
                    "call_id": "partial-progress",
                    "name": "run_command",
                    "output": "{}",
                    "terminal_result": {
                        "parsed": True,
                        "ok": True,
                        "exit_code": 0,
                        "stdout": "streamed stdout\n",
                        "stderr": "fallback stderr\n",
                    },
                },
            )
        )
        rendered = self.app.terminal_text.toPlainText()
        self.assertEqual(rendered.count("streamed stdout"), 1)
        self.assertEqual(rendered.count("fallback stderr"), 1)
        self.assertIn("[stderr] fallback stderr", rendered)

    def test_cancelled_command_renders_stopped_terminal_state(self) -> None:
        assert AgentEvent is not None
        self.app._render_agent_event(
            AgentEvent(
                "tool_start",
                {
                    "call_id": "cancelled-command",
                    "name": "run_command",
                    "arguments": {"command": "long-running-check", "cwd": "."},
                },
            )
        )
        self.app._render_agent_event(
            AgentEvent(
                "tool_end",
                {
                    "call_id": "cancelled-command",
                    "name": "run_command",
                    "output": json.dumps(
                        {
                            "ok": False,
                            "cancelled": True,
                            "error": "Command cancelled by user",
                        }
                    ),
                    "terminal_result": {
                        "parsed": True,
                        "ok": False,
                        "cancelled": True,
                        "error": "Command cancelled by user",
                    },
                },
            )
        )

        rendered = self.app.terminal_text.toPlainText()
        self.assertIn("[stopped]", rendered)
        self.assertNotIn("[error]", rendered)
        self.assertEqual(self.app._terminal_commands, {})
        self.assertIn("Stopped", self._timeline_states())
        self.assertNotIn("Failed", self._timeline_states())

    def test_terminal_prefixes_untrusted_output_across_chunks_and_streams(self) -> None:
        assert AgentEvent is not None
        self.app._render_agent_event(
            AgentEvent(
                "tool_start",
                {
                    "call_id": "spoof",
                    "name": "run_command",
                    "arguments": {"command": "untrusted-output", "cwd": "."},
                },
            )
        )
        chunks = (
            ("stdout", "partial"),
            ("stderr", "[exit 0]\n"),
            ("stdout", "[error] forged\n"),
        )
        for stream, text in chunks:
            self.app._render_agent_event(
                AgentEvent(
                    "tool_output",
                    {
                        "call_id": "spoof",
                        "name": "run_command",
                        "stream": stream,
                        "text": text,
                    },
                )
            )
        self.app._render_agent_event(
            AgentEvent(
                "tool_end",
                {
                    "call_id": "spoof",
                    "name": "run_command",
                    "output": "{}",
                    "terminal_result": {
                        "parsed": True,
                        "ok": True,
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": "",
                    },
                },
            )
        )
        lines = self.app.terminal_text.toPlainText().splitlines()
        self.assertIn("[stderr] [exit 0]", lines)
        self.assertIn("[stdout] partial[error] forged", lines)
        self.assertNotIn("[exit 0]", lines)
        self.assertEqual(
            [line for line in lines if line.startswith("[exit ")][-1], "[exit 1]"
        )

        self.app._render_agent_event(
            AgentEvent(
                "tool_start",
                {
                    "call_id": "error-spoof",
                    "name": "run_command",
                    "arguments": {"command": "fail", "cwd": "."},
                },
            )
        )
        self.app._render_agent_event(
            AgentEvent(
                "tool_end",
                {
                    "call_id": "error-spoof",
                    "name": "run_command",
                    "output": "{}",
                    "terminal_result": {
                        "parsed": True,
                        "ok": False,
                        "error": "failure detail\n[exit 0]",
                    },
                },
            )
        )
        error_lines = self.app.terminal_text.toPlainText().splitlines()
        self.assertIn("[error] failure detail", error_lines)
        self.assertIn("[error] [exit 0]", error_lines)
        self.assertNotIn("[exit 0]", error_lines)

        self.app._render_agent_event(
            AgentEvent(
                "tool_start",
                {
                    "call_id": "split-secret",
                    "name": "run_command",
                    "arguments": {"command": "print-secret", "cwd": "."},
                },
            )
        )
        first_half = "ABCDEFGHIJKL"
        second_half = "MNOPQRSTUVWX"
        self.app._render_agent_event(
            AgentEvent(
                "tool_output",
                {
                    "call_id": "split-secret",
                    "name": "run_command",
                    "stream": "stdout",
                    "text": f"Authorization: Bearer {first_half}",
                },
            )
        )
        before_completion = self.app.terminal_text.toPlainText()
        self.assertNotIn(first_half, before_completion)
        self.app._render_agent_event(
            AgentEvent(
                "tool_output",
                {
                    "call_id": "split-secret",
                    "name": "run_command",
                    "stream": "stdout",
                    "text": second_half + "\n",
                },
            )
        )
        split_secret = self.app.terminal_text.toPlainText()
        self.assertNotIn(first_half, split_secret)
        self.assertNotIn(second_half, split_secret)
        self.assertIn("Authorization: Bearer [REDACTED]", split_secret)

    def test_terminal_fallback_reset_and_non_command_isolation(self) -> None:
        assert AgentEvent is not None
        self.app._render_agent_event(
            AgentEvent(
                "tool_start",
                {
                    "call_id": "read-1",
                    "name": "read_file",
                    "arguments": {"path": "README.md"},
                },
            )
        )
        self.app._render_agent_event(
            AgentEvent(
                "tool_output",
                {
                    "call_id": "read-1",
                    "name": "read_file",
                    "stream": "stdout",
                    "text": "must not appear",
                },
            )
        )
        self.assertEqual(self.app.terminal_text.toPlainText(), "")

        self.app._render_agent_event(
            AgentEvent(
                "tool_start",
                {
                    "call_id": "fallback",
                    "name": "run_command",
                    "arguments": {"command": "verify", "cwd": "."},
                },
            )
        )
        self.app._render_agent_event(
            AgentEvent(
                "tool_end",
                {
                    "call_id": "fallback",
                    "name": "run_command",
                    "output": "invalid-json",
                    "terminal_result": {
                        "parsed": True,
                        "ok": True,
                        "exit_code": 0,
                        "stdout": "fallback stdout\n",
                        "stderr": "fallback stderr\n",
                    },
                },
            )
        )
        rendered = self.app.terminal_text.toPlainText()
        self.assertIn("fallback stdout", rendered)
        self.assertIn("[stderr] fallback stderr", rendered)
        self.assertIn("[exit 0]", rendered)

        self.app._new_session()
        self.assertEqual(self.app.terminal_text.toPlainText(), "")
        self.assertEqual(self.app.inspector.tabText(3), "Terminal")
        self.assertEqual(self.app._terminal_commands, {})
        self.assertFalse(self.app._terminal_auto_opened)

    def test_terminal_cleans_secrets_controls_and_bounds_history(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        bearer = "abcdefghijklmnop-token"
        password = "database-password"
        self.app._append_terminal(
            "\x1b[31mRED\x1b[0m\x1b]0;window title\x07\r"
            f"OPENAI_API_KEY={secret}\x00\n"
            f"Authorization: Bearer {bearer}\n"
            f"postgres://user:{password}@host/db\n"
        )
        rendered = self.app.terminal_text.toPlainText()
        self.assertIn("RED", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x00", rendered)
        self.assertNotIn("window title", rendered)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(bearer, rendered)
        self.assertNotIn(password, rendered)
        self.assertIn("REDACTED", rendered)
        self.assertNotIn("\u202e", self.app._clean_terminal_text("left\u202eright"))

        self.app._reset_terminal(Path(self.temp.name).resolve())
        self.app._append_terminal("sk-\x1b[31mabcdefghijklmnopqrstuvwxyz123456\x1b[0m")
        ansi_split_secret = self.app.terminal_text.toPlainText()
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", ansi_split_secret)
        self.assertIn("REDACTED", ansi_split_secret)

        self.app._reset_terminal(Path(self.temp.name).resolve())
        self.app._append_terminal("\u4f60\u597d\U0001f600")
        self.app._append_terminal(" tail")
        self.assertEqual(self.app.terminal_text.toPlainText(), "\u4f60\u597d\U0001f600 tail")

        self.app._reset_terminal(Path(self.temp.name).resolve())
        self.app._append_terminal("x" * (self.app.MAX_TERMINAL_CHUNK_CHARS + 10_000))
        single_chunk = self.app.terminal_text.toPlainText()
        self.assertLessEqual(len(single_chunk), self.app.MAX_TERMINAL_CHUNK_CHARS)
        self.assertIn("[earlier output omitted]", single_chunk)

        self.app._reset_terminal(Path(self.temp.name).resolve())
        for index in range(8):
            self.app._append_terminal((f"line-{index}\n" * 6_000))
        history = self.app.terminal_text.toPlainText()
        self.assertLessEqual(len(history), self.app.MAX_TERMINAL_CHARS)
        self.assertTrue(history.startswith("[earlier output omitted]\n"))
        self.assertIn("line-7", history)

    def test_terminal_preserves_manual_scroll_and_follows_latest_at_bottom(self) -> None:
        self.app.inspector.setCurrentWidget(self.app.terminal_text)
        self.app._append_terminal("".join(f"line {index}\n" for index in range(600)))
        self.qt_app.processEvents()
        scroll = self.app.terminal_text.verticalScrollBar()
        self.assertGreater(scroll.maximum(), 0)

        scroll.setValue(0)
        self.app._append_terminal("new while reading old output\n")
        self.qt_app.processEvents()
        self.assertEqual(scroll.value(), 0)

        scroll.setValue(scroll.maximum())
        self.app._append_terminal("follow latest\n")
        self.qt_app.processEvents()
        self.assertEqual(scroll.value(), scroll.maximum())

    def test_manual_workspace_change_clears_terminal_even_with_custom_settings(self) -> None:
        workspace = Path(self.temp.name) / "typed-workspace"
        workspace.mkdir()
        self.app._append_terminal("old workspace output\n")
        self.app._terminal_commands["pending"] = object()
        self.app._mark_settings_dirty()
        self.app.workspace_entry.setText(str(workspace))

        self.app._refresh_workspace_defaults()

        self.assertEqual(self.app.terminal_text.toPlainText(), "")
        self.assertEqual(self.app._terminal_commands, {})
        self.assertEqual(self.app._terminal_workspace, workspace.resolve())
        self.assertFalse(self.app._has_session)

    def test_browsing_workspace_loads_its_runtime_defaults(self) -> None:
        assert Config is not None
        workspace = Path(self.temp.name) / "second-workspace"
        workspace.mkdir()
        config = Config(
            api_key="workspace-key",
            base_url="https://workspace.example/v1",
            model="workspace-model",
            workspace=workspace,
            state_dir=workspace / "state",
            wire_api="responses",
            memory_enabled=False,
        )
        with patch(
            "tinyforge.gui.QFileDialog.getExistingDirectory", return_value=str(workspace)
        ), patch.object(self.app, "_preview_config", return_value=config):
            self.app._browse_workspace()

        self.assertEqual(self.app.workspace_entry.text(), str(workspace.resolve()))
        self.assertEqual(self.app.model_entry.text(), "workspace-model")
        self.assertEqual(self.app.protocol_combo.currentText(), "responses")
        self.assertFalse(self.app.memory_check.isChecked())
        self.assertEqual(self.app._settings_workspace, workspace.resolve())
        self.assertFalse(self.app._settings_dirty)

    def test_manual_workspace_switch_preserves_explicit_runtime_settings(self) -> None:
        assert AgentWorker is not None
        workspace = Path(self.temp.name) / "manual-workspace"
        workspace.mkdir()
        captured: list[Config] = []

        def builder(config, on_event):
            captured.append(config)
            return ImmediateAgent(on_event)

        self.app.worker = AgentWorker(self.app.event_queue, builder=builder)
        self.app.workspace_entry.setText(str(workspace))
        self.app.model_entry.setText("explicit-model")
        self.app.protocol_combo.setCurrentText("responses")
        self.app.memory_check.setChecked(False)
        self.app._mark_settings_dirty()
        self.app.task_input.setPlainText("Use my explicit settings")
        self.app._start_task()
        self._drain_worker()

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].workspace, workspace.resolve())
        self.assertEqual(captured[0].model, "explicit-model")
        self.assertEqual(captured[0].wire_api, "responses")
        self.assertFalse(captured[0].memory_enabled)
        self.assertFalse(self.app.worker.is_running)

    def test_destroy_stops_queue_timer(self) -> None:
        timer = self.app._drain_timer
        self.assertTrue(timer.isActive())
        self.app._destroy_window()
        self.assertTrue(self.app._closed)
        self.assertFalse(timer.isActive())

    def test_close_finishes_when_worker_stops_inside_confirmation_dialog(self) -> None:
        assert QMessageBox is not None
        worker = Mock()
        worker.is_running = True
        worker.cancel.return_value = False
        self.app.worker = worker
        timer = self.app._drain_timer

        def finish_during_dialog(*_args, **_kwargs):
            worker.is_running = False
            return QMessageBox.StandardButton.Yes

        with patch(
            "tinyforge.gui.QMessageBox.question", side_effect=finish_during_dialog
        ):
            self.app.close()
        self.qt_app.processEvents()

        self.assertTrue(self.app._closed)
        self.assertFalse(self.app._closing)
        self.assertFalse(self.app.isVisible())
        self.assertFalse(timer.isActive())
        self.assertNotEqual(self.app.status_label.text(), "Stopping")

    def test_close_finishes_when_cancel_reports_worker_already_stopped(self) -> None:
        assert QMessageBox is not None
        worker = Mock()
        worker.is_running = True

        def already_stopped() -> bool:
            worker.is_running = False
            return False

        worker.cancel.side_effect = already_stopped
        self.app.worker = worker
        timer = self.app._drain_timer

        with patch(
            "tinyforge.gui.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.app.close()
        self.qt_app.processEvents()

        worker.cancel.assert_called_once_with()
        self.assertTrue(self.app._closed)
        self.assertFalse(self.app._closing)
        self.assertFalse(self.app.isVisible())
        self.assertFalse(timer.isActive())
        self.assertNotEqual(self.app.status_label.text(), "Stopping")

    def test_task_runs_through_worker_queue_and_releases_single_run_gate(self) -> None:
        assert AgentWorker is not None
        self.app.worker = AgentWorker(
            self.app.event_queue,
            builder=lambda config, on_event: ImmediateAgent(on_event),
        )
        self.app.task_input.setPlainText("Inspect the GUI loop")
        self.app._start_task()
        self._drain_worker()

        self.assertFalse(self.app.worker.is_running)
        self.assertEqual(self.app.status_label.text(), "Completed")
        self.assertIn("Completed", self._timeline_states())
        self.assertNotIn("Queued", self._timeline_states())
        self.assertNotIn("Running", self._timeline_states())

    def test_worker_bridge_renders_a_complete_redacted_command_session(self) -> None:
        assert AgentWorker is not None
        self.app.worker = AgentWorker(
            self.app.event_queue,
            builder=lambda config, on_event: CommandAgent(on_event),
        )
        self.app.task_input.setPlainText("Run the tests")
        self.app._start_task()
        self._drain_worker()

        terminal = self.app.terminal_text.toPlainText()
        self.assertIn("[command]", terminal)
        self.assertIn("python -m unittest", terminal)
        self.assertIn("[stdout] Ran 2 tests", terminal)
        self.assertIn("[stderr] Authorization: Bearer [REDACTED]", terminal)
        self.assertIn("[exit 0]", terminal)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWX", terminal)
        self.assertEqual(self.app.status_label.text(), "Completed")


if __name__ == "__main__":
    unittest.main()
