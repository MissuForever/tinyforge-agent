from __future__ import annotations

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
                self.app._finish_result(result)
                states = self._timeline_states()
                self.assertIn(expected, states)
                self.assertNotIn("Queued", states)
                self.assertNotIn("Running", states)

    def test_runtime_error_clears_active_rows(self) -> None:
        self._seed_running_rows()
        self.app._finish_error("network unavailable")
        states = self._timeline_states()
        self.assertIn("Error", states)
        self.assertNotIn("Queued", states)
        self.assertNotIn("Running", states)

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


if __name__ == "__main__":
    unittest.main()
