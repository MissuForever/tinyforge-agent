from __future__ import annotations

import json
import os
import tempfile
import threading
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
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QMessageBox,
        QPlainTextEdit,
    )
except ImportError:
    Qt = None
    QPalette = None
    QAbstractItemView = None
    QApplication = None
    QMessageBox = None
    QPlainTextEdit = None

if QApplication is not None:
    from tinyforge.agent import AgentEvent, AgentResult
    from tinyforge.config import Config
    from tinyforge.gui import TinyForgeApp
    from tinyforge.gui_support import AgentWorker
    from tinyforge.workspace_view import WorkspaceFile, WorkspaceFilePreview, WorkspaceIndex
else:
    AgentEvent = None
    AgentResult = None
    Config = None
    AgentWorker = None
    TinyForgeApp = None
    WorkspaceFile = None
    WorkspaceFilePreview = None
    WorkspaceIndex = None


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

    def _skill_tree_text(self) -> str:
        values: list[str] = []

        def collect(item) -> None:
            values.extend(item.text(column) for column in range(item.columnCount()))
            for child_index in range(item.childCount()):
                collect(item.child(child_index))

        for item_index in range(self.app.skill_tree.topLevelItemCount()):
            collect(self.app.skill_tree.topLevelItem(item_index))
        return "\n".join(values)

    def _drain_worker(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while self.app.worker.is_running and time.monotonic() < deadline:
            self.app._drain_queue()
            self.qt_app.processEvents()
            time.sleep(0.005)
        self.app._drain_queue()
        self.qt_app.processEvents()

    def _wait_until(self, predicate, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app._drain_file_queues()
            self.qt_app.processEvents()
            time.sleep(0.005)
        self.app._drain_file_queues()
        self.qt_app.processEvents()
        self.assertTrue(predicate())

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

    def test_command_output_is_a_read_only_standalone_bottom_panel(self) -> None:
        assert Qt is not None
        assert QPlainTextEdit is not None
        self.assertEqual(self.app.inspector.count(), 4)
        self.assertFalse(self.app.inspector.tabBar().usesScrollButtons())
        self.assertEqual(self.app.inspector.indexOf(self.app.terminal_text), -1)
        self.assertEqual(self.app.center_splitter.orientation(), Qt.Orientation.Vertical)
        self.assertIs(self.app.center_splitter.widget(0), self.app.timeline_panel)
        self.assertIs(self.app.center_splitter.widget(1), self.app.terminal_panel)
        self.assertTrue(self.app.terminal_panel.isAncestorOf(self.app.terminal_text))
        self.assertTrue(self.app.terminal_panel.isVisibleTo(self.app))
        self.assertGreater(self.app.center_splitter.sizes()[1], 0)
        self.assertEqual(self.app.terminal_count_label.text(), "0 commands")
        self.assertTrue(self.app.terminal_text.isReadOnly())
        assert QAbstractItemView is not None
        self.assertTrue(self.app.skills_tab.isAncestorOf(self.app.skill_tree))
        self.assertEqual(
            self.app.skill_tree.editTriggers(),
            QAbstractItemView.EditTrigger.NoEditTriggers,
        )
        self.assertTrue(self.app.skills_check.isEnabled())
        self.assertFalse(self.app.terminal_text.isUndoRedoEnabled())
        self.assertEqual(
            self.app.terminal_text.lineWrapMode(),
            QPlainTextEdit.LineWrapMode.NoWrap,
        )
        self.assertNotEqual(
            self.app.terminal_text.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )

    def test_secondary_screen_minimum_keeps_settings_and_inspector_tabs_visible(self) -> None:
        self.app.setMinimumSize(960, 600)
        self.app.resize(960, 600)
        self.qt_app.processEvents()

        checks = (
            self.app.memory_check,
            self.app.skills_check,
            self.app.continue_check,
        )
        for check in checks:
            self.assertGreaterEqual(
                check.width(),
                check.minimumSizeHint().width(),
                f"{check.text()} is clipped at 960px",
            )
        for left, right in zip(checks, checks[1:]):
            self.assertLess(left.geometry().right(), right.geometry().left())

        tab_bar = self.app.inspector.tabBar()
        self.assertFalse(tab_bar.usesScrollButtons())
        self.assertEqual(
            [tab_bar.tabText(index) for index in range(tab_bar.count())],
            ["Info", "Diff", "Memory", "Skills"],
        )
        for index in range(tab_bar.count()):
            label = tab_bar.tabText(index)
            rect = tab_bar.tabRect(index)
            self.assertGreaterEqual(
                rect.width(),
                tab_bar.fontMetrics().horizontalAdvance(label),
                f"{label} text is clipped",
            )
            self.assertLessEqual(rect.right(), tab_bar.rect().right())

        self.assertGreaterEqual(
            self.app.main_splitter.sizes()[1],
            self.app.center_splitter.minimumWidth(),
        )

    def test_workspace_files_are_a_read_only_left_sidebar(self) -> None:
        assert Qt is not None
        assert QPlainTextEdit is not None
        self.assertEqual(self.app.main_splitter.orientation(), Qt.Orientation.Horizontal)
        self.assertEqual(self.app.main_splitter.count(), 3)
        self.assertIs(self.app.main_splitter.widget(0), self.app.workspace_panel)
        self.assertIs(self.app.main_splitter.widget(1), self.app.center_splitter)
        self.assertIs(self.app.main_splitter.widget(2), self.app.inspector_panel)
        self.assertEqual(self.app.inspector.indexOf(self.app.files_tab), -1)
        self.assertTrue(self.app.workspace_panel.isAncestorOf(self.app.file_tree))
        self.assertTrue(self.app.workspace_panel.isAncestorOf(self.app.file_search_entry))
        self.assertTrue(self.app.workspace_panel.isAncestorOf(self.app.file_refresh_button))
        self.assertTrue(self.app.workspace_panel.isVisibleTo(self.app))
        self.assertFalse(self.app.main_splitter.childrenCollapsible())
        self.assertFalse(self.app.center_splitter.childrenCollapsible())
        self.assertGreater(self.app.main_splitter.sizes()[0], 0)
        self.assertTrue(self.app.file_preview_text.isReadOnly())
        self.assertFalse(self.app.file_preview_text.isUndoRedoEnabled())
        self.assertEqual(
            self.app.file_preview_text.lineWrapMode(),
            QPlainTextEdit.LineWrapMode.NoWrap,
        )
        self.assertEqual(self.app.file_tree.columnCount(), 2)
        self.assertEqual(self.app.file_tree.headerItem().text(1), "GIT")

    def test_files_tree_filters_previews_and_preserves_selection_on_refresh(self) -> None:
        workspace = Path(self.temp.name)
        secret = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
        (workspace / "src").mkdir()
        (workspace / "src" / "app.py").write_text(
            f"token = '{secret}'\nprint('ready')\n",
            encoding="utf-8",
        )
        (workspace / "src" / "helper.py").write_text(
            "def helper():\n    return 1\n",
            encoding="utf-8",
        )
        (workspace / "binary.dat").write_bytes(b"binary\0payload")
        secret_filename = f"result-{secret}.py"
        (workspace / secret_filename).write_text("value = 1\n", encoding="utf-8")
        (workspace / ".env").write_text("PASSWORD=hidden\n", encoding="utf-8")
        (workspace / ".demo").mkdir()
        (workspace / ".demo" / "recording.mp4").write_bytes(b"video")

        self.app._request_files_refresh(delay_ms=0)
        self._wait_until(
            lambda: self.app.file_refresh_button.isEnabled()
            and "src/app.py" in self.app._file_entries
        )

        self.assertNotIn(".env", self.app._file_entries)
        self.assertNotIn(".demo/recording.mp4", self.app._file_entries)
        self.assertIn(secret_filename, self.app._file_entries)
        secret_item = self.app._file_items[secret_filename]
        self.assertNotIn(secret, secret_item.text(0))
        self.assertNotIn(secret, secret_item.toolTip(0))
        self.assertIn("REDACTED", secret_item.text(0))
        self.assertIn("src", self.app._file_items)
        self.assertNotIn("src/app.py", self.app._file_items)

        src_item = self.app._file_items["src"]
        self.app.file_tree.expandItem(src_item)
        self.qt_app.processEvents()
        self.assertIn("src/app.py", self.app._file_items)
        app_item = self.app._file_items["src/app.py"]
        self.app.file_tree.setCurrentItem(app_item)
        self.qt_app.processEvents()
        self._wait_until(lambda: self.app.file_preview_meta.text() != "Loading")

        rendered = self.app.file_preview_text.toPlainText()
        self.assertIn("1 |", rendered)
        self.assertIn("print('ready')", rendered)
        self.assertNotIn(secret, rendered)
        self.assertIn("REDACTED", rendered)

        (workspace / "src" / "new.py").write_text("created = True\n", encoding="utf-8")
        self.app._request_files_refresh(delay_ms=0)
        self._wait_until(
            lambda: self.app.file_refresh_button.isEnabled()
            and "src/new.py" in self.app._file_entries
        )
        self.assertTrue(self.app._file_items["src"].isExpanded())
        current = self.app.file_tree.currentItem()
        self.assertIsNotNone(current)
        self.assertEqual(
            current.data(0, self.app.FILE_PATH_ROLE),
            "src/app.py",
        )

        self.app.file_search_entry.setText("helper")
        self.app._apply_file_filter()
        self.assertEqual(self.app.file_tree.topLevelItemCount(), 1)
        self.assertEqual(
            self.app.file_tree.topLevelItem(0).data(0, self.app.FILE_PATH_ROLE),
            "src/helper.py",
        )
        self.assertIn("matches", self.app.file_count_label.text())

        self.app.file_search_entry.clear()
        self.app._apply_file_filter()
        self.assertTrue(self.app.file_tree.rootIsDecorated())
        binary_item = self.app._file_items["binary.dat"]
        self.app.file_tree.setCurrentItem(binary_item)
        self.qt_app.processEvents()
        self._wait_until(
            lambda: "unavailable" in self.app.file_preview_text.toPlainText()
        )

        secret_item = self.app._file_items[secret_filename]
        self.app.file_tree.setCurrentItem(secret_item)
        self.qt_app.processEvents()
        self._wait_until(lambda: self.app.file_preview_meta.text() != "Loading")
        self.assertNotIn(secret, self.app.file_preview_path.text())
        self.assertNotIn(secret, self.app.file_preview_path.toolTip())
        self.assertNotIn("\u202e", self.app._file_display_text("left\u202eright", 80))

    def test_file_preview_worker_keeps_only_the_latest_pending_request(self) -> None:
        assert WorkspaceFilePreview is not None
        started = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def slow_preview(_workspace: Path, relative_path: str) -> WorkspaceFilePreview:
            calls.append(relative_path)
            started.set()
            release.wait(timeout=2)
            return WorkspaceFilePreview(relative_path, "text", text="ok", line_count=1)

        with patch("tinyforge.gui.preview_workspace_file", side_effect=slow_preview):
            try:
                self.app._selected_file_path = "file-0.py"
                self.app._start_file_preview("file-0.py")
                self.assertTrue(started.wait(timeout=1))
                for index in range(1, 50):
                    relative_path = f"file-{index}.py"
                    self.app._selected_file_path = relative_path
                    self.app._start_file_preview(relative_path)
                self.assertLessEqual(self.app._file_preview_requests.qsize(), 1)
                self.assertLessEqual(self.app._file_preview_queue.qsize(), 1)
            finally:
                release.set()
            self._wait_until(lambda: len(calls) >= 2)

        self.assertEqual(calls, ["file-0.py", "file-49.py"])

    def test_file_refresh_does_not_restore_selection_over_a_newer_choice(self) -> None:
        assert WorkspaceFile is not None
        assert WorkspaceIndex is not None
        workspace = self.app._files_workspace
        index = WorkspaceIndex(
            workspace,
            (WorkspaceFile("a.py"), WorkspaceFile("b.py")),
        )
        self.app._apply_file_index(index, None)
        self.app.file_tree.setCurrentItem(self.app._file_items["a.py"])
        self.qt_app.processEvents()
        captured_revision = self.app._file_selection_revision
        self.app.file_tree.setCurrentItem(self.app._file_items["b.py"])
        self.qt_app.processEvents()

        self.app._offer_latest(
            self.app._file_index_queue,
            (
                self.app._file_generation,
                workspace,
                index,
                "a.py",
                captured_revision,
            ),
        )
        self.app._drain_file_queues()

        current = self.app.file_tree.currentItem()
        self.assertIsNotNone(current)
        self.assertEqual(current.data(0, self.app.FILE_PATH_ROLE), "b.py")

    def test_debounced_file_target_does_not_override_a_newer_choice(self) -> None:
        assert WorkspaceFile is not None
        assert WorkspaceIndex is not None
        workspace = self.app._files_workspace
        index = WorkspaceIndex(
            workspace,
            (WorkspaceFile("created.py"), WorkspaceFile("chosen.py")),
        )
        self.app._apply_file_index(index, None)
        requested_revision = self.app._file_selection_revision
        self.app._request_files_refresh("created.py")
        self.app.file_tree.setCurrentItem(self.app._file_items["chosen.py"])
        self.qt_app.processEvents()

        with patch.object(self.app, "_start_file_refresh") as start_refresh:
            self.app._refresh_files()

        start_refresh.assert_called_once_with(
            workspace,
            "created.py",
            selection_revision=requested_revision,
        )

    def test_files_tree_renders_git_status_for_files_and_directories(self) -> None:
        assert WorkspaceFile is not None
        assert WorkspaceIndex is not None
        self._wait_until(
            lambda: self.app.file_refresh_button.isEnabled()
            and self.app._file_index is not None
        )
        workspace = Path(self.temp.name).resolve()
        index = WorkspaceIndex(
            workspace,
            (
                WorkspaceFile("src/app.py", " M"),
                WorkspaceFile("new.py", "??"),
            ),
            git_available=True,
        )

        self.app._apply_file_index(index, None)

        self.assertEqual(self.app._file_items["src"].text(1), "M")
        self.assertEqual(self.app._file_items["new.py"].text(1), "?")
        self.app.file_tree.expandItem(self.app._file_items["src"])
        self.qt_app.processEvents()
        self.assertEqual(self.app._file_items["src/app.py"].text(1), "M")
        self.assertEqual(self.app.file_count_label.text(), "2 files")

    def test_successful_file_tools_schedule_refresh_and_failed_tools_do_not(self) -> None:
        assert AgentEvent is not None
        with patch.object(self.app, "_request_files_refresh") as refresh:
            self.app._render_agent_event(
                AgentEvent(
                    "tool_start",
                    {
                        "call_id": "write-ok",
                        "name": "write_file",
                        "arguments": {"path": "created.py", "content": "value = 1\n"},
                    },
                )
            )
            self.app._render_agent_event(
                AgentEvent(
                    "tool_end",
                    {
                        "call_id": "write-ok",
                        "name": "write_file",
                        "output": "{}",
                        "output_ok": True,
                        "output_summary": "created.py",
                    },
                )
            )
            refresh.assert_called_once_with("created.py")

            refresh.reset_mock()
            self.app._render_agent_event(
                AgentEvent(
                    "tool_start",
                    {
                        "call_id": "write-failed",
                        "name": "write_file",
                        "arguments": {"path": "failed.py", "content": ""},
                    },
                )
            )
            self.app._render_agent_event(
                AgentEvent(
                    "tool_end",
                    {
                        "call_id": "write-failed",
                        "name": "write_file",
                        "output": "{}",
                        "output_ok": False,
                        "output_summary": "failed",
                    },
                )
            )
            refresh.assert_not_called()

            self.app._render_agent_event(
                AgentEvent(
                    "tool_end",
                    {
                        "call_id": "command-failed",
                        "name": "run_command",
                        "output": "{}",
                        "output_ok": False,
                        "output_summary": "exit 1",
                    },
                )
            )
            refresh.assert_called_once_with(delay_ms=250)

    def test_files_discard_stale_background_results(self) -> None:
        assert WorkspaceFile is not None
        assert WorkspaceIndex is not None
        workspace = self.app._files_workspace
        current_generation = self.app._file_generation
        self.app._file_index_queue.put(
            (
                current_generation - 1,
                workspace,
                WorkspaceIndex(workspace, (WorkspaceFile("stale.py"),)),
                None,
                self.app._file_selection_revision,
            )
        )
        self.app._drain_file_queues()
        self.assertNotIn("stale.py", self.app._file_entries)

    def test_empty_or_failed_file_refresh_clears_old_preview(self) -> None:
        assert WorkspaceFile is not None
        assert WorkspaceIndex is not None
        workspace = self.app._files_workspace
        self.app._selected_file_path = "old.py"
        self.app.file_preview_text.setPlainText("1 | VISIBLE_OLD_CONTENT")

        self.app._apply_file_index(WorkspaceIndex(workspace, ()), None)

        self.assertIsNone(self.app._selected_file_path)
        self.assertNotIn("VISIBLE_OLD_CONTENT", self.app.file_preview_text.toPlainText())

        self.app._selected_file_path = "old.py"
        self.app.file_preview_text.setPlainText("1 | VISIBLE_OLD_CONTENT")
        self.app._apply_file_index(
            WorkspaceIndex(workspace, (), error="Workspace is not available."),
            None,
        )
        self.assertIsNone(self.app._selected_file_path)
        self.assertNotIn("VISIBLE_OLD_CONTENT", self.app.file_preview_text.toPlainText())

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
        self.app.inspector.setCurrentWidget(self.app.details_text)
        self.app._render_agent_event(start)
        self.assertIs(self.app.inspector.currentWidget(), self.app.details_text)
        self.assertEqual(self.app.terminal_count_label.text(), "1 command")
        self.assertTrue(self.app.terminal_panel.isVisibleTo(self.app))
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
        self.assertEqual(self.app.terminal_count_label.text(), "0 commands")
        self.assertEqual(self.app._terminal_commands, {})

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
        self.app._selected_file_path = "old.py"
        self.app.file_preview_text.setPlainText("old workspace preview")
        self.app._mark_settings_dirty()
        self.app.workspace_entry.setText(str(workspace))

        self.app._refresh_workspace_defaults()

        self.assertEqual(self.app.terminal_text.toPlainText(), "")
        self.assertEqual(self.app._terminal_commands, {})
        self.assertEqual(self.app._terminal_workspace, workspace.resolve())
        self.assertEqual(self.app._files_workspace, workspace.resolve())
        self.assertIsNone(self.app._selected_file_path)
        self.assertNotIn("old workspace preview", self.app.file_preview_text.toPlainText())
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
            skills_enabled=True,
        )
        with patch(
            "tinyforge.gui.QFileDialog.getExistingDirectory", return_value=str(workspace)
        ), patch.object(self.app, "_preview_config", return_value=config):
            self.app._browse_workspace()

        self.assertEqual(self.app.workspace_entry.text(), str(workspace.resolve()))
        self.assertEqual(self.app.model_entry.text(), "workspace-model")
        self.assertEqual(self.app.protocol_combo.currentText(), "responses")
        self.assertFalse(self.app.memory_check.isChecked())
        self.assertTrue(self.app.skills_check.isChecked())
        self.assertEqual(self.app._settings_workspace, workspace.resolve())
        self.assertEqual(self.app._files_workspace, workspace.resolve())
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
        self.app.skills_check.setChecked(True)
        self.app._mark_settings_dirty()
        self.app.task_input.setPlainText("Use my explicit settings")
        self.app._start_task()
        self._drain_worker()

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].workspace, workspace.resolve())
        self.assertEqual(captured[0].model, "explicit-model")
        self.assertEqual(captured[0].wire_api, "responses")
        self.assertFalse(captured[0].memory_enabled)
        self.assertTrue(captured[0].skills_enabled)
        self.assertFalse(self.app.worker.is_running)

    def test_skill_loaded_event_updates_inspector_once(self) -> None:
        assert AgentEvent is not None
        event = AgentEvent(
            "skill_loaded",
            {
                "id": "workspace:verified-change",
                "name": "verified-change",
                "scope": "workspace",
            },
        )

        self.app._render_agent_event(event)
        self.app._render_agent_event(event)

        self.assertEqual(self.app._loaded_skill_ids, {"workspace:verified-change"})
        index = self.app.inspector.indexOf(self.app.skills_tab)
        self.assertEqual(self.app.inspector.tabText(index), "Skills")
        rows = [
            self.app.timeline.topLevelItem(item).text(1)
            for item in range(self.app.timeline.topLevelItemCount())
        ]
        self.assertEqual(rows.count("Skill"), 1)

    def test_skill_adaptation_error_is_visible_redacted_and_read_only(self) -> None:
        assert AgentEvent is not None
        assert Qt is not None
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"

        self.app._render_agent_event(
            AgentEvent(
                "skill_adaptation_error",
                {"error": f"Skill review failed for {secret}\x00"},
            )
        )

        item = self.app.timeline.topLevelItem(self.app.timeline.topLevelItemCount() - 1)
        item_id = item.data(0, Qt.ItemDataRole.UserRole)
        details = self.app._entry_details[item_id]
        self.assertEqual(item.text(0), "Failed")
        self.assertEqual(item.text(1), "Skill review")
        self.assertIn("Skill review failed", details)
        self.assertNotIn(secret, details)
        self.assertNotIn("\x00", details)
        self.assertTrue(self.app.details_text.isReadOnly())

    def test_loaded_receipt_digests_survive_snapshot_without_path_or_body(self) -> None:
        assert AgentEvent is not None
        skill_digest = "A" * 64
        resource_digest = "B" * 64
        loaded = {
            "id": "workspace:verified-change",
            "name": "verified-change",
            "description": "Verify changes",
            "scope": "workspace",
            "sha256": skill_digest,
            "resource_manifest_sha256": resource_digest,
            "loaded_step": 4,
            "path": "C:/private/SKILL.md",
            "instructions": "SKILL_BODY_MUST_NOT_RENDER",
        }
        self.app._render_agent_event(AgentEvent("skill_loaded", loaded))

        self.app._apply_skill_snapshot(
            {
                "state": "ready",
                "enabled": True,
                "available": [
                    {
                        "id": "workspace:verified-change",
                        "name": "verified-change",
                        "description": "Verify changes",
                        "scope": "workspace",
                    }
                ],
                "loaded": [
                    {
                        "id": "workspace:verified-change",
                        "name": "verified-change",
                        "description": "Verify changes",
                        "scope": "workspace",
                    }
                ],
                "receipts": [
                    {
                        "id": "workspace:verified-change",
                        "sha256": skill_digest,
                        "resource_manifest_sha256": resource_digest,
                        "loaded_step": 4,
                        "path": "C:/private/receipt.json",
                        "content": "RECEIPT_BODY_MUST_NOT_RENDER",
                    }
                ],
                "invalid_entries_skipped": 0,
            }
        )

        loaded_group = next(
            self.app.skill_tree.topLevelItem(index)
            for index in range(self.app.skill_tree.topLevelItemCount())
            if self.app.skill_tree.topLevelItem(index).text(0) == "Loaded"
        )
        loaded_item = loaded_group.child(0)
        receipt_item = loaded_item.child(0)
        self.assertEqual(receipt_item.text(0), "Receipt")
        self.assertIn("sha256 aaaaaaaaaaaa", receipt_item.text(1))
        self.assertIn("resources bbbbbbbbbbbb", receipt_item.text(1))
        self.assertIn("sha256 aaaaaaaaaaaa", loaded_item.toolTip(1))
        self.assertIn("resources bbbbbbbbbbbb", loaded_item.toolTip(1))
        tree_text = self._skill_tree_text()
        self.assertNotIn("C:/private", tree_text)
        self.assertNotIn("SKILL_BODY_MUST_NOT_RENDER", tree_text)
        self.assertNotIn("RECEIPT_BODY_MUST_NOT_RENDER", tree_text)

    def test_skill_candidates_keep_result_order_and_hide_unexpected_fields(self) -> None:
        assert AgentEvent is not None
        self.app._apply_skill_snapshot(
            {
                "state": "ready",
                "enabled": True,
                "available": [
                    {
                        "id": "workspace:alpha",
                        "name": "alpha",
                        "description": "First catalog entry",
                        "scope": "workspace",
                        "path": "C:/private/alpha/SKILL.md",
                        "instructions": "CATALOG_BODY_MUST_NOT_RENDER",
                    }
                ],
                "loaded": [],
                "invalid_entries_skipped": 2,
            }
        )
        candidates = [
            {
                "id": f"workspace:skill-{index:02d}",
                "name": f"skill-{index:02d}",
                "description": f"Candidate {index}",
                "scope": "workspace",
                "path": "C:/private/SKILL.md",
                "instructions": "CANDIDATE_BODY_MUST_NOT_RENDER",
            }
            for index in range(55)
        ]
        self.app._render_agent_event(
            AgentEvent(
                "skills_listed",
                {
                    "call_id": "search-1",
                    "query": "verify change",
                    "scope": "workspace",
                    "skills": candidates,
                    "invalid_entries_skipped": 2,
                },
            )
        )
        self.app._render_agent_event(
            AgentEvent(
                "skills_listed",
                {
                    "call_id": "search-from-task",
                    "query": "",
                    "query_source": "task",
                    "scope": "any",
                    "skills": [],
                },
            )
        )

        rendered = self.app._skill_searches[0]["skills"]
        self.assertEqual(len(rendered), 50)
        self.assertEqual(
            [item["id"] for item in rendered[:3]],
            ["workspace:skill-00", "workspace:skill-01", "workspace:skill-02"],
        )
        tree_text = self._skill_tree_text()
        self.assertIn('query="verify change"', tree_text)
        self.assertIn("current task · scope=any", tree_text)
        self.assertIn("workspace:skill-00", tree_text)
        self.assertNotIn("C:/private", tree_text)
        self.assertNotIn("CATALOG_BODY_MUST_NOT_RENDER", tree_text)
        self.assertNotIn("CANDIDATE_BODY_MUST_NOT_RENDER", tree_text)

    def test_skill_load_order_and_resource_parent_are_stable(self) -> None:
        assert AgentEvent is not None
        first = AgentEvent(
            "skill_loaded",
            {"id": "workspace:first", "name": "first", "scope": "workspace"},
        )
        second = AgentEvent(
            "skill_loaded",
            {"id": "user:second", "name": "second", "scope": "user"},
        )
        self.app._render_agent_event(first)
        self.app._render_agent_event(second)
        self.app._render_agent_event(first)
        self.app._render_agent_event(
            AgentEvent(
                "skill_resource_read",
                {
                    "call_id": "resource-1",
                    "skill_id": "user:second",
                    "path": "references/checklist.md",
                    "start_line": 2,
                    "end_line": 8,
                    "total_lines": 20,
                    "truncated": False,
                },
            )
        )
        self.app._render_agent_event(
            AgentEvent(
                "skill_resource_read",
                {
                    "call_id": "resource-outside",
                    "skill_id": "workspace:first",
                    "path": "../private.txt",
                    "start_line": 1,
                    "end_line": 1,
                    "total_lines": 1,
                },
            )
        )

        self.assertEqual(
            [item["id"] for item in self.app._loaded_skills],
            ["workspace:first", "user:second"],
        )
        self.assertEqual(len(self.app._skill_resource_reads), 1)
        loaded_group = next(
            self.app.skill_tree.topLevelItem(index)
            for index in range(self.app.skill_tree.topLevelItemCount())
            if self.app.skill_tree.topLevelItem(index).text(0) == "Loaded"
        )
        self.assertEqual(loaded_group.child(0).childCount(), 0)
        self.assertEqual(loaded_group.child(1).childCount(), 1)
        self.assertIn("references/checklist.md", loaded_group.child(1).child(0).text(1))
        self.assertNotIn("private.txt", self._skill_tree_text())

    def test_new_skill_run_resets_evidence_and_new_context_resets_loaded_order(self) -> None:
        assert AgentEvent is not None
        self.app._skill_catalog = [
            {
                "id": "workspace:keep-catalog",
                "name": "keep-catalog",
                "description": "Catalog metadata",
                "scope": "workspace",
            }
        ]
        self.app._render_agent_event(
            AgentEvent(
                "skills_listed",
                {"call_id": "search-old", "skills": [], "query": "old", "scope": "any"},
            )
        )
        self.app._render_agent_event(
            AgentEvent(
                "skill_loaded",
                {"id": "workspace:loaded", "name": "loaded", "scope": "workspace"},
            )
        )
        self.app._skill_resource_reads.append(
            {
                "call_id": "resource-old",
                "skill_id": "workspace:loaded",
                "path": "references/old.md",
                "start_line": 1,
                "end_line": 1,
                "total_lines": 1,
                "truncated": False,
            }
        )
        self.app._skill_fault_reports.append({"call_id": "fault-old"})

        self.app._begin_skill_run(continue_session=True, enabled=True)
        self.assertEqual([item["id"] for item in self.app._loaded_skills], ["workspace:loaded"])
        self.assertEqual(self.app._skill_searches, [])
        self.assertEqual(self.app._skill_resource_reads, [])
        self.assertEqual(self.app._skill_fault_reports, [])

        self.app._begin_skill_run(continue_session=False, enabled=True)
        self.assertEqual(self.app._loaded_skills, [])
        self.assertEqual(self.app._loaded_skill_ids, set())
        self.assertEqual(self.app._skill_catalog[0]["id"], "workspace:keep-catalog")
        self.app._begin_skill_run(continue_session=False, enabled=False)
        self.assertEqual(self.app.skill_status_label.text(), "Disabled")

    def test_skill_fault_report_is_a_read_only_unqualified_audit_record(self) -> None:
        assert AgentEvent is not None
        self.app._render_agent_event(
            AgentEvent(
                "skill_fault_report",
                {
                    "call_id": "failed-tool-1",
                    "localized_step": 7,
                    "tool": "edit_file",
                    "observation": "Patch verification failed",
                    "active_skill_candidates": [
                        {
                            "id": "workspace:verified-change",
                            "sha256": "a" * 64,
                            "loaded_step": 3,
                        }
                    ],
                    "attribution_status": "unresolved",
                    "qualification_status": "not_run",
                    "skill_mutation_applied": False,
                    "trace_truncated": True,
                },
            )
        )

        tree_text = self._skill_tree_text()
        self.assertIn("Adaptation", tree_text)
        self.assertIn("step 7 · edit_file · trace clipped", tree_text)
        self.assertIn("unresolved", tree_text)
        self.assertIn("not_run", tree_text)
        self.assertIn("not applied", tree_text)
        self.assertIn("workspace:verified-change", tree_text)
        self.assertNotIn("Apply update", tree_text)
        self.assertEqual(len(self.app._skill_fault_reports), 1)

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
        self.assertIs(self.app.center_splitter.widget(1), self.app.terminal_panel)
        self.assertTrue(self.app.terminal_panel.isVisibleTo(self.app))
        self.assertEqual(self.app.terminal_count_label.text(), "1 command")
        self.assertEqual(self.app.status_label.text(), "Completed")


if __name__ == "__main__":
    unittest.main()
