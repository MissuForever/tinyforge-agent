"""PySide6 desktop frontend for TinyForge."""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCloseEvent,
    QFont,
    QKeySequence,
    QShortcut,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStyle,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .agent import AgentEvent, AgentResult
from .config import Config, ConfigError
from .gui_support import AgentWorker, UiEnvelope, summarize_tool_output
from .memory import redact_secrets
from .workspace_view import (
    WorkspaceFile,
    WorkspaceFilePreview,
    WorkspaceIndex,
    preview_workspace_file,
    scan_workspace,
)


class DiffHighlighter(QSyntaxHighlighter):
    """Highlight unified diffs without changing their auditable text."""

    def __init__(self, document: Any) -> None:
        super().__init__(document)
        self.added = self._format("#12633f", "#e9f8ef")
        self.removed = self._format("#9f2f35", "#fff0f1")
        self.hunk = self._format("#345d9d", "#edf4ff", bold=True)
        self.file = self._format("#2f3941", "#eef1f3", bold=True)

    @staticmethod
    def _format(foreground: str, background: str, *, bold: bool = False) -> QTextCharFormat:
        value = QTextCharFormat()
        value.setForeground(QColor(foreground))
        value.setBackground(QColor(background))
        value.setFontWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
        return value

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt API
        if text.startswith(("File: ", "--- ", "+++ ")):
            selected = self.file
        elif text.startswith("@@"):
            selected = self.hunk
        elif text.startswith("+"):
            selected = self.added
        elif text.startswith("-"):
            selected = self.removed
        else:
            return
        self.setFormat(0, len(text), selected)


class MemoryHighlighter(QSyntaxHighlighter):
    """Expose the memory hierarchy and evidence lines at a glance."""

    def __init__(self, document: Any) -> None:
        super().__init__(document)
        self.heading = QTextCharFormat()
        self.heading.setForeground(QColor("#0f766e"))
        self.heading.setFontWeight(QFont.Weight.DemiBold)
        self.evidence = QTextCharFormat()
        self.evidence.setForeground(QColor("#8a5a00"))
        self.index = QTextCharFormat()
        self.index.setForeground(QColor("#12633f"))
        self.index.setFontWeight(QFont.Weight.DemiBold)

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt API
        stripped = text.strip()
        if stripped.startswith(("<working_memory>", "</working_memory>")):
            self.setFormat(0, len(text), self.heading)
        elif stripped.startswith("verified_evidence:"):
            self.setFormat(0, len(text), self.evidence)
        elif stripped.startswith(("persistent_memory_index", "- [fact:", "- [sop:")):
            self.setFormat(0, len(text), self.index)
        elif ":" in stripped and not stripped.startswith("-"):
            key = text.find(":")
            if key >= 0:
                self.setFormat(0, key + 1, self.heading)


class TerminalHighlighter(QSyntaxHighlighter):
    """Keep commands, stderr, and exit states scannable in the terminal log."""

    def __init__(self, document: Any) -> None:
        super().__init__(document)
        self.prompt = self._format("#79d9c0", bold=True)
        self.stdout = self._format("#d6dfdc")
        self.stderr = self._format("#ff9da4")
        self.success = self._format("#7fd6a5", bold=True)
        self.error = self._format("#ff9da4", bold=True)
        self.muted = self._format("#8c999e")

    @staticmethod
    def _format(color: str, *, bold: bool = False) -> QTextCharFormat:
        value = QTextCharFormat()
        value.setForeground(QColor(color))
        value.setFontWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
        return value

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt API
        if text.startswith(("[command]", "PS ", "$ ")):
            selected = self.prompt
        elif text.startswith("[stdout]"):
            selected = self.stdout
        elif text.startswith("[stderr]"):
            selected = self.stderr
        elif text.startswith("[exit 0]"):
            selected = self.success
        elif text.startswith(("[exit ", "[error]", "[interrupted]", "[stopped]")):
            selected = self.error
        elif text.startswith("[earlier output omitted]"):
            selected = self.muted
        else:
            return
        self.setFormat(0, len(text), selected)


@dataclass(slots=True)
class _TerminalCommandState:
    streamed: set[str] = field(default_factory=set)
    line_start: bool = True
    active_source: str | None = None
    pending: dict[str, str] = field(
        default_factory=lambda: {"stdout": "", "stderr": ""}
    )
    discarding: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _FileTreeNode:
    relative_path: str
    name: str
    is_directory: bool
    git_status: str = ""
    is_link: bool = False


class TinyForgeApp(QMainWindow):
    """A focused, auditable desktop workbench for the TinyForge runtime."""

    MAX_TIMELINE_ITEMS = 500
    MAX_CHANGES_CHARS = 200_000
    MAX_TERMINAL_CHARS = 200_000
    TERMINAL_TRIM_CHARS = 160_000
    MAX_TERMINAL_CHUNK_CHARS = 40_000
    MAX_FILE_SEARCH_RESULTS = 500
    FILE_PATH_ROLE = int(Qt.ItemDataRole.UserRole)
    FILE_KIND_ROLE = FILE_PATH_ROLE + 1
    FILE_LOADED_ROLE = FILE_PATH_ROLE + 2
    ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
    CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
    BIDI_CONTROL_RE = re.compile(r"[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")

    COLORS = {
        "canvas": "#f4f6f7",
        "panel": "#ffffff",
        "border": "#d8dee2",
        "text": "#20282e",
        "muted": "#65727a",
        "accent": "#0f766e",
        "accent_active": "#0b5f59",
        "success": "#12633f",
        "success_bg": "#e9f8ef",
        "warning": "#8a5a00",
        "warning_bg": "#fff7df",
        "danger": "#9f2f35",
        "danger_bg": "#fff0f1",
        "running": "#176c82",
        "running_bg": "#e9f7fa",
        "selected": "#dcecff",
    }

    ROW_STYLES = {
        "running": ("#e9f7fa", "#176c82", QStyle.StandardPixmap.SP_BrowserReload),
        "success": ("#e9f8ef", "#12633f", QStyle.StandardPixmap.SP_DialogApplyButton),
        "warning": ("#fff7df", "#8a5a00", QStyle.StandardPixmap.SP_MessageBoxWarning),
        "error": ("#fff0f1", "#9f2f35", QStyle.StandardPixmap.SP_MessageBoxCritical),
        "muted": ("#ffffff", "#65727a", QStyle.StandardPixmap.SP_MessageBoxInformation),
    }

    def __init__(self, workspace: str | Path = ".", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"TinyForge {__version__}")
        self.setObjectName("TinyForgeWindow")
        self.setMinimumSize(1080, 720)
        self.resize(1460, 900)

        initial_workspace = Path(workspace).expanduser().resolve()
        preview_config = self._preview_config(initial_workspace)
        initial_model = (
            preview_config.model
            if preview_config is not None
            else os.getenv("TINYFORGE_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
        )
        initial_wire_api = (
            preview_config.wire_api
            if preview_config is not None
            else os.getenv("TINYFORGE_WIRE_API", "chat_completions")
        )
        if initial_wire_api == "chat":
            initial_wire_api = "chat_completions"

        self._settings_workspace: Path | None = initial_workspace if preview_config else None
        self._settings_dirty = False
        self._has_session = False
        self._closed = False
        self._closing = False
        self.current_run_id: str | None = None
        self._entry_details: dict[str, str] = {}
        self._timeline_items: dict[str, QTreeWidgetItem] = {}
        self._tool_items: dict[str, str] = {}
        self._round_items: dict[int, str] = {}
        self._active_items: set[str] = set()
        self._task_item: str | None = None
        self._current_task_text = ""
        self._change_count = 0
        self._memory_commit_count = 0
        self._item_sequence = 0
        self._terminal_commands: dict[str, _TerminalCommandState] = {}
        self._terminal_command_count = 0
        self._terminal_auto_opened = False
        self._terminal_workspace = initial_workspace
        self._files_workspace = initial_workspace
        self._file_index: WorkspaceIndex | None = None
        self._file_nodes: dict[str, tuple[_FileTreeNode, ...]] = {}
        self._file_entries: dict[str, WorkspaceFile] = {}
        self._file_items: dict[str, QTreeWidgetItem] = {}
        self._file_expanded_paths: set[str] = {""}
        self._selected_file_path: str | None = None
        self._file_tool_paths: dict[str, str] = {}
        self._file_generation = 0
        self._file_preview_generation = 0
        self._file_selection_revision = 0
        self._pending_file_selection: str | None = None
        self._pending_file_selection_revision: int | None = None
        self._file_workers_stopped = False
        self._file_index_requests: queue.Queue[
            tuple[int, Path, str | None, int] | None
        ] = queue.Queue(maxsize=1)
        self._file_index_queue: queue.Queue[
            tuple[int, Path, WorkspaceIndex, str | None, int]
        ] = queue.Queue(maxsize=1)
        self._file_preview_requests: queue.Queue[
            tuple[int, Path, str] | None
        ] = queue.Queue(maxsize=1)
        self._file_preview_queue: queue.Queue[
            tuple[int, Path, WorkspaceFilePreview]
        ] = queue.Queue(maxsize=1)
        self._file_index_worker = threading.Thread(
            target=self._file_index_worker_loop,
            name="tinyforge-files-worker",
            daemon=True,
        )
        self._file_preview_worker = threading.Thread(
            target=self._file_preview_worker_loop,
            name="tinyforge-preview-worker",
            daemon=True,
        )
        self._file_index_worker.start()
        self._file_preview_worker.start()

        self._file_refresh_timer = QTimer(self)
        self._file_refresh_timer.setSingleShot(True)
        self._file_refresh_timer.timeout.connect(self._refresh_files)
        self._file_filter_timer = QTimer(self)
        self._file_filter_timer.setSingleShot(True)
        self._file_filter_timer.setInterval(180)
        self._file_filter_timer.timeout.connect(self._apply_file_filter)

        self.event_queue: queue.Queue[UiEnvelope] = queue.Queue(maxsize=2_000)
        self.worker = AgentWorker(self.event_queue)

        self._apply_stylesheet()
        self._build_layout(
            initial_workspace=initial_workspace,
            initial_model=initial_model,
            initial_wire_api=initial_wire_api,
            memory_enabled=preview_config.memory_enabled if preview_config else True,
        )
        self._set_memory_text("Memory has not been loaded for this workspace.")
        self._set_details_text("Select an execution step to inspect its evidence.")
        self._set_changes_text("No file changes captured.")
        self._reset_files(initial_workspace)
        self._set_status("Ready", "neutral")
        self._set_stats("No task has run in this session")

        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self._start_task)
        QShortcut(QKeySequence("Ctrl+Enter"), self, activated=self._start_task)
        QShortcut(QKeySequence("Escape"), self, activated=self._on_stop_shortcut)

        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(50)
        self._drain_timer.timeout.connect(self._drain_queue)
        self._drain_timer.start()
        self.task_input.setFocus()

    def _apply_stylesheet(self) -> None:
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget#AppCanvas {{ background: {self.COLORS['canvas']}; }}
            QWidget {{
                color: {self.COLORS['text']};
                font-family: "Segoe UI", "Microsoft YaHei UI";
                font-size: 10pt;
            }}
            QFrame#TopBar {{ background: #171b1e; border: 0; }}
            QLabel#Brand {{ color: #f8faf9; font-size: 18pt; font-weight: 700; }}
            QLabel#Version {{ color: #55c39d; font-family: "Cascadia Mono"; font-size: 9pt; }}
            QLabel#StatusBadge {{
                background: #2b3236; color: #d9e0de; border: 1px solid #424b50;
                border-radius: 8px; padding: 5px 10px; font-weight: 600;
            }}
            QLabel#StatusBadge[tone="running"] {{ background: #163944; color: #9edbe7; border-color: #266173; }}
            QLabel#StatusBadge[tone="success"] {{ background: #173b2b; color: #9addbb; border-color: #286347; }}
            QLabel#StatusBadge[tone="warning"] {{ background: #443713; color: #f1d37a; border-color: #705d22; }}
            QLabel#StatusBadge[tone="error"] {{ background: #482327; color: #f0afb4; border-color: #753a40; }}
            QFrame#SettingsBar {{
                background: {self.COLORS['panel']};
                border-bottom: 1px solid {self.COLORS['border']};
            }}
            QLabel#FieldLabel {{ color: {self.COLORS['muted']}; font-size: 8.5pt; font-weight: 600; }}
            QLineEdit, QComboBox {{
                background: #ffffff; border: 1px solid #cbd3d8; border-radius: 6px;
                padding: 7px 9px; selection-background-color: {self.COLORS['selected']};
            }}
            QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{ border: 1px solid {self.COLORS['accent']}; }}
            QComboBox::drop-down {{ border: 0; width: 28px; }}
            QCheckBox {{ spacing: 7px; color: #3d484f; }}
            QCheckBox::indicator {{ width: 17px; height: 17px; }}
            QPushButton {{
                background: #ffffff; border: 1px solid #cbd3d8; border-radius: 6px;
                padding: 7px 11px; font-weight: 600;
            }}
            QPushButton:hover {{ background: #f1f4f5; border-color: #aab5bb; }}
            QPushButton:pressed {{ background: #e7ebed; }}
            QPushButton:disabled {{ color: #9ba5aa; background: #f1f3f4; border-color: #e0e4e6; }}
            QPushButton#IconButton {{ padding: 0; min-width: 36px; max-width: 36px; min-height: 36px; max-height: 36px; }}
            QPushButton#PrimaryButton {{
                background: {self.COLORS['accent']}; color: white; border-color: {self.COLORS['accent']};
                padding: 10px 17px; min-width: 112px; min-height: 42px;
            }}
            QPushButton#PrimaryButton:hover {{ background: {self.COLORS['accent_active']}; border-color: {self.COLORS['accent_active']}; }}
            QFrame#ToolPanel {{
                background: {self.COLORS['panel']}; border: 1px solid {self.COLORS['border']};
                border-radius: 7px;
            }}
            QLabel#PanelTitle {{ font-size: 11pt; font-weight: 700; color: #293239; }}
            QLabel#PanelMeta {{ color: {self.COLORS['muted']}; font-size: 8.5pt; }}
            QTreeWidget {{
                background: #ffffff; alternate-background-color: #fafbfb; border: 0;
                outline: 0; show-decoration-selected: 1;
            }}
            QTreeWidget::item {{ height: 36px; border-bottom: 1px solid #edf0f2; }}
            QTreeWidget::item:selected {{ background: {self.COLORS['selected']}; color: #17242d; }}
            QHeaderView::section {{
                background: #f2f4f5; color: #56636b; border: 0;
                border-bottom: 1px solid {self.COLORS['border']}; padding: 8px;
                font-size: 8.5pt; font-weight: 700;
            }}
            QTabWidget {{ background: #ffffff; }}
            QTabWidget::pane {{ background: #ffffff; border: 0; border-top: 1px solid {self.COLORS['border']}; }}
            QTabBar {{ background: #f2f4f5; }}
            QTabBar::tab {{
                background: #f2f4f5; color: #5c686f; border: 0; padding: 10px 17px;
                min-width: 80px; font-weight: 600;
            }}
            QTabBar::tab:selected {{ background: #ffffff; color: {self.COLORS['accent']}; border-bottom: 2px solid {self.COLORS['accent']}; }}
            QPlainTextEdit {{
                background: #ffffff; border: 1px solid {self.COLORS['border']}; border-radius: 6px;
                padding: 10px; selection-background-color: {self.COLORS['selected']};
            }}
            QPlainTextEdit#InspectorText {{ border: 0; border-radius: 0; }}
            QPlainTextEdit#CodeText {{
                border: 0; border-radius: 0; font-family: "Cascadia Mono", Consolas;
                font-size: 9.5pt; color: #263238;
            }}
            QPlainTextEdit#TerminalText {{
                background: #111517; color: #d6dfdc; border: 0; border-radius: 0;
                font-family: "Cascadia Mono", Consolas; font-size: 9.5pt;
                selection-background-color: #315c66; selection-color: #ffffff;
            }}
            QWidget#FilesToolbar {{
                background: #ffffff; border-bottom: 1px solid {self.COLORS['border']};
            }}
            QTreeWidget#FilesTree::item {{ height: 28px; border-bottom: 0; }}
            QLabel#FilePath {{
                color: #293239; font-family: "Cascadia Mono", Consolas;
                font-size: 9pt; font-weight: 600;
            }}
            QPlainTextEdit#FilePreview {{
                background: #fbfcfc; border: 0; border-top: 1px solid {self.COLORS['border']};
                border-radius: 0; font-family: "Cascadia Mono", Consolas;
                font-size: 9pt; color: #263238;
            }}
            QFrame#Composer {{ background: {self.COLORS['panel']}; border-top: 1px solid {self.COLORS['border']}; }}
            QLabel#ShortcutHint {{ color: #7a858b; font-size: 8.5pt; }}
            QProgressBar {{ background: #dfe5e7; border: 0; border-radius: 2px; max-height: 4px; }}
            QProgressBar::chunk {{ background: {self.COLORS['accent']}; border-radius: 2px; }}
            QSplitter::handle {{ background: {self.COLORS['canvas']}; width: 8px; }}
            """
        )

    def _build_layout(
        self,
        *,
        initial_workspace: Path,
        initial_model: str,
        initial_wire_api: str,
        memory_enabled: bool,
    ) -> None:
        canvas = QWidget(self)
        canvas.setObjectName("AppCanvas")
        self.setCentralWidget(canvas)
        outer = QVBoxLayout(canvas)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_top_bar())
        outer.addWidget(
            self._build_settings_bar(
                initial_workspace, initial_model, initial_wire_api, memory_enabled
            )
        )
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(18, 14, 18, 12)
        content_layout.setSpacing(12)
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.addWidget(self._build_timeline_panel())
        self.main_splitter.addWidget(self._build_inspector())
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 2)
        self.main_splitter.setSizes([820, 560])
        content_layout.addWidget(self.main_splitter, 1)
        outer.addWidget(content, 1)
        outer.addWidget(self._build_composer())
        outer.addWidget(self._build_footer())

    def _build_top_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(62)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(20, 0, 18, 0)
        layout.setSpacing(10)
        brand = QLabel("TinyForge")
        brand.setObjectName("Brand")
        version = QLabel(__version__)
        version.setObjectName("Version")
        self.status_label = QLabel()
        self.status_label.setObjectName("StatusBadge")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setMinimumWidth(82)
        self.status_label.setFixedHeight(34)
        layout.addWidget(brand, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(version, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addSpacing(12)
        layout.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addStretch(1)
        self.new_button = QPushButton()
        self.new_button.setObjectName("IconButton")
        self.new_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.new_button.setToolTip("New session")
        self.new_button.setAccessibleName("New session")
        self.new_button.clicked.connect(self._new_session)
        self.stop_button = QPushButton()
        self.stop_button.setObjectName("IconButton")
        self.stop_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.stop_button.setToolTip("Stop after the current operation (Esc)")
        self.stop_button.setAccessibleName("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_task)
        layout.addWidget(self.new_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.stop_button, 0, Qt.AlignmentFlag.AlignVCenter)
        return bar

    def _field(self, label: str, widget: QWidget, *, stretch: bool = False) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        caption = QLabel(label)
        caption.setObjectName("FieldLabel")
        layout.addWidget(caption)
        layout.addWidget(widget)
        if stretch:
            container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return container

    def _build_settings_bar(
        self, workspace: Path, model: str, wire_api: str, memory_enabled: bool
    ) -> QWidget:
        bar = QFrame()
        bar.setObjectName("SettingsBar")
        layout = QGridLayout(bar)
        layout.setContentsMargins(18, 10, 18, 11)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(0)
        self.workspace_entry = QLineEdit(str(workspace))
        self.workspace_entry.setToolTip(str(workspace))
        self.workspace_entry.editingFinished.connect(self._refresh_workspace_defaults)
        workspace_field = self._field("WORKSPACE", self.workspace_entry, stretch=True)
        self.browse_button = QPushButton()
        self.browse_button.setObjectName("IconButton")
        self.browse_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        self.browse_button.setToolTip("Choose workspace")
        self.browse_button.setAccessibleName("Choose workspace")
        self.browse_button.clicked.connect(self._browse_workspace)
        self.model_entry = QLineEdit(model)
        self.model_entry.setMinimumWidth(180)
        self.model_entry.textEdited.connect(self._mark_settings_dirty)
        model_field = self._field("MODEL", self.model_entry)
        self.protocol_combo = QComboBox()
        self.protocol_combo.addItems(["chat_completions", "responses"])
        self.protocol_combo.setCurrentText(wire_api)
        self.protocol_combo.setMinimumWidth(160)
        self.protocol_combo.currentTextChanged.connect(self._mark_settings_dirty)
        protocol_field = self._field("PROTOCOL", self.protocol_combo)
        checks = QWidget()
        checks_layout = QHBoxLayout(checks)
        checks_layout.setContentsMargins(0, 17, 0, 0)
        checks_layout.setSpacing(16)
        self.memory_check = QCheckBox("Memory")
        self.memory_check.setChecked(memory_enabled)
        self.memory_check.toggled.connect(self._mark_settings_dirty)
        self.continue_check = QCheckBox("Continue context")
        self.continue_check.setChecked(True)
        checks_layout.addWidget(self.memory_check)
        checks_layout.addWidget(self.continue_check)
        layout.addWidget(workspace_field, 0, 0)
        layout.addWidget(self.browse_button, 0, 1, Qt.AlignmentFlag.AlignBottom)
        layout.addWidget(model_field, 0, 2)
        layout.addWidget(protocol_field, 0, 3)
        layout.addWidget(checks, 0, 4)
        layout.setColumnStretch(0, 1)
        return bar

    def _build_timeline_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("ToolPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        title_bar = QWidget()
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(14, 11, 12, 9)
        title = QLabel("Execution timeline")
        title.setObjectName("PanelTitle")
        self.timeline_count_label = QLabel("0 events")
        self.timeline_count_label.setObjectName("PanelMeta")
        title_layout.addWidget(title)
        title_layout.addStretch(1)
        title_layout.addWidget(self.timeline_count_label)
        layout.addWidget(title_bar)
        self.timeline = QTreeWidget()
        self.timeline.setColumnCount(3)
        self.timeline.setHeaderLabels(["STATUS", "ACTION", "SUMMARY"])
        self.timeline.setRootIsDecorated(False)
        self.timeline.setUniformRowHeights(True)
        self.timeline.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.timeline.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.timeline.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.timeline.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.timeline.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.timeline.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.timeline.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.timeline.setColumnWidth(0, 118)
        self.timeline.setColumnWidth(1, 176)
        self.timeline.itemSelectionChanged.connect(self._on_timeline_select)
        layout.addWidget(self.timeline, 1)
        return panel

    def _make_inspector_text(self, *, code: bool, wrap: bool) -> QPlainTextEdit:
        text = QPlainTextEdit()
        text.setObjectName("CodeText" if code else "InspectorText")
        text.setReadOnly(True)
        text.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth
            if wrap
            else QPlainTextEdit.LineWrapMode.NoWrap
        )
        if code:
            text.setFont(QFont("Cascadia Mono", 10))
        return text

    def _build_inspector(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("ToolPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        self.inspector = QTabWidget()
        self.inspector.setDocumentMode(True)
        self.details_text = self._make_inspector_text(code=False, wrap=True)
        self.changes_text = self._make_inspector_text(code=True, wrap=False)
        self.memory_text = self._make_inspector_text(code=True, wrap=True)
        self.terminal_text = self._make_inspector_text(code=True, wrap=False)
        self.terminal_text.setObjectName("TerminalText")
        self.terminal_text.setUndoRedoEnabled(False)
        self._diff_highlighter = DiffHighlighter(self.changes_text.document())
        self._memory_highlighter = MemoryHighlighter(self.memory_text.document())
        self._terminal_highlighter = TerminalHighlighter(self.terminal_text.document())
        self.inspector.addTab(self.details_text, "Details")
        self.inspector.addTab(self.changes_text, "Changes")
        self.inspector.addTab(self.memory_text, "Memory")
        self.inspector.addTab(self.terminal_text, "Terminal")
        self.inspector.addTab(self._build_files_tab(), "Files")
        layout.addWidget(self.inspector)
        return panel

    def _build_files_tab(self) -> QWidget:
        tab = QWidget()
        tab.setObjectName("FilesTab")
        self.files_tab = tab
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar = QWidget()
        toolbar.setObjectName("FilesToolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(10, 7, 10, 7)
        toolbar_layout.setSpacing(8)
        self.file_search_entry = QLineEdit()
        self.file_search_entry.setPlaceholderText("Filter files")
        self.file_search_entry.setClearButtonEnabled(True)
        self.file_search_entry.setAccessibleName("Filter workspace files")
        self.file_search_entry.textChanged.connect(self._schedule_file_filter)
        self.file_count_label = QLabel("0 files")
        self.file_count_label.setObjectName("PanelMeta")
        self.file_refresh_button = QPushButton()
        self.file_refresh_button.setObjectName("IconButton")
        self.file_refresh_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self.file_refresh_button.setToolTip("Refresh workspace files")
        self.file_refresh_button.setAccessibleName("Refresh workspace files")
        self.file_refresh_button.clicked.connect(self._refresh_files)
        toolbar_layout.addWidget(self.file_search_entry, 1)
        toolbar_layout.addWidget(self.file_count_label)
        toolbar_layout.addWidget(self.file_refresh_button)
        layout.addWidget(toolbar)

        self.files_splitter = QSplitter(Qt.Orientation.Vertical)
        self.files_splitter.setChildrenCollapsible(False)
        self.file_tree = QTreeWidget()
        self.file_tree.setObjectName("FilesTree")
        self.file_tree.setColumnCount(2)
        self.file_tree.setHeaderLabels(["NAME", "GIT"])
        self.file_tree.setRootIsDecorated(True)
        self.file_tree.setUniformRowHeights(True)
        self.file_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.file_tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_tree.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.file_tree.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.file_tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.file_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.file_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.file_tree.setColumnWidth(1, 48)
        self.file_tree.itemExpanded.connect(self._on_file_item_expanded)
        self.file_tree.itemCollapsed.connect(self._on_file_item_collapsed)
        self.file_tree.itemSelectionChanged.connect(self._on_file_tree_select)

        preview = QWidget()
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(0)
        preview_header = QWidget()
        preview_header_layout = QHBoxLayout(preview_header)
        preview_header_layout.setContentsMargins(10, 7, 10, 7)
        preview_header_layout.setSpacing(8)
        self.file_preview_path = QLabel("Select a file")
        self.file_preview_path.setObjectName("FilePath")
        self.file_preview_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.file_preview_meta = QLabel("")
        self.file_preview_meta.setObjectName("PanelMeta")
        preview_header_layout.addWidget(self.file_preview_path, 1)
        preview_header_layout.addWidget(self.file_preview_meta)
        self.file_preview_text = QPlainTextEdit()
        self.file_preview_text.setObjectName("FilePreview")
        self.file_preview_text.setReadOnly(True)
        self.file_preview_text.setUndoRedoEnabled(False)
        self.file_preview_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.file_preview_text.setPlainText("Select a file to preview.")
        preview_layout.addWidget(preview_header)
        preview_layout.addWidget(self.file_preview_text, 1)

        self.files_splitter.addWidget(self.file_tree)
        self.files_splitter.addWidget(preview)
        self.files_splitter.setStretchFactor(0, 3)
        self.files_splitter.setStretchFactor(1, 2)
        self.files_splitter.setSizes([260, 190])
        layout.addWidget(self.files_splitter, 1)
        return tab

    def _build_composer(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Composer")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 10, 18, 10)
        layout.setSpacing(6)
        heading = QHBoxLayout()
        label = QLabel("Task")
        label.setObjectName("PanelTitle")
        hint = QLabel("Ctrl+Enter to run")
        hint.setObjectName("ShortcutHint")
        heading.addWidget(label)
        heading.addStretch(1)
        heading.addWidget(hint)
        layout.addLayout(heading)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.task_input = QPlainTextEdit()
        self.task_input.setPlaceholderText("Describe the change, constraints, and verification you need...")
        self.task_input.setMinimumHeight(72)
        self.task_input.setMaximumHeight(112)
        self.run_button = QPushButton("Run task")
        self.run_button.setObjectName("PrimaryButton")
        self.run_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.run_button.setToolTip("Run task (Ctrl+Enter)")
        self.run_button.clicked.connect(self._start_task)
        row.addWidget(self.task_input, 1)
        row.addWidget(self.run_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(row)
        return panel

    def _build_footer(self) -> QWidget:
        footer = QWidget()
        footer.setFixedHeight(34)
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(18, 0, 18, 8)
        self.stats_label = QLabel()
        self.stats_label.setObjectName("PanelMeta")
        self.stats_label.setMinimumWidth(0)
        self.stats_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(150)
        self.progress.setVisible(False)
        layout.addWidget(self.stats_label)
        layout.addStretch(1)
        layout.addWidget(self.progress)
        return footer

    def _preview_config(self, workspace: Path) -> Config | None:
        try:
            return Config.from_env(workspace)
        except (ConfigError, OSError, ValueError):
            return None

    def _apply_workspace_defaults(self, workspace: Path, config: Config) -> None:
        self.model_entry.setText(config.model)
        self.protocol_combo.setCurrentText(config.wire_api)
        self.memory_check.setChecked(config.memory_enabled)
        self._settings_workspace = workspace
        self._settings_dirty = False

    def _mark_settings_dirty(self, _value: object = None) -> None:
        self._settings_dirty = True

    def _refresh_workspace_defaults(self) -> None:
        if self.worker.is_running:
            return
        try:
            workspace = Path(self.workspace_entry.text()).expanduser().resolve()
        except (OSError, ValueError):
            return
        if workspace != self._terminal_workspace:
            self.workspace_entry.setToolTip(str(workspace))
            self._has_session = False
            self._set_memory_text("Memory has not been loaded for this workspace.")
            self._reset_terminal(workspace)
        if workspace != self._files_workspace:
            self._reset_files(workspace)
        if self._settings_dirty:
            return
        if workspace == self._settings_workspace:
            return
        preview_config = self._preview_config(workspace)
        if preview_config is not None:
            self._apply_workspace_defaults(workspace, preview_config)

    def _browse_workspace(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose workspace",
            self.workspace_entry.text() or str(Path.cwd()),
            QFileDialog.Option.ShowDirsOnly,
        )
        if selected:
            workspace = Path(selected).resolve()
            self.workspace_entry.setText(str(workspace))
            self.workspace_entry.setToolTip(str(workspace))
            self._settings_workspace = None
            self._settings_dirty = False
            preview_config = self._preview_config(workspace)
            if preview_config is not None:
                self._apply_workspace_defaults(workspace, preview_config)
            self._has_session = False
            self._set_memory_text("Memory has not been loaded for this workspace.")
            self._reset_terminal(workspace)
            self._reset_files(workspace)

    def _new_session(self) -> None:
        if not self.worker.reset():
            return
        self._has_session = False
        self.current_run_id = None
        self._entry_details.clear()
        self._timeline_items.clear()
        self._tool_items.clear()
        self._round_items.clear()
        self._active_items.clear()
        self._task_item = None
        self._current_task_text = ""
        self._change_count = 0
        self._memory_commit_count = 0
        self.timeline.clear()
        self.timeline_count_label.setText("0 events")
        self.inspector.setTabText(self.inspector.indexOf(self.changes_text), "Changes")
        self.inspector.setTabText(self.inspector.indexOf(self.memory_text), "Memory")
        try:
            terminal_workspace = Path(self.workspace_entry.text()).expanduser().resolve()
        except (OSError, ValueError):
            terminal_workspace = self._terminal_workspace
        self._reset_terminal(terminal_workspace)
        self._set_details_text("Select an execution step to inspect its evidence.")
        self._set_changes_text("No file changes captured.")
        self._refresh_memory()
        self._request_files_refresh(delay_ms=0)
        self._set_status("Ready", "neutral")
        self._set_stats("New session")
        self.task_input.setFocus()

    def _start_task(self) -> None:
        if self.worker.is_running:
            return
        task = self.task_input.toPlainText().strip()
        if not task:
            self._set_status("Enter a task", "warning")
            self.task_input.setFocus()
            return
        try:
            workspace = Path(self.workspace_entry.text()).expanduser().resolve()
            if self._settings_workspace != workspace and not self._settings_dirty:
                workspace_defaults = Config.from_env(workspace)
                self._apply_workspace_defaults(workspace, workspace_defaults)
            model = self.model_entry.text().strip()
            config = Config.from_env(
                workspace,
                model=model or None,
                wire_api=self.protocol_combo.currentText(),
                memory_enabled=self.memory_check.isChecked(),
            )
            self._settings_workspace = workspace
        except (ConfigError, OSError, ValueError) as exc:
            self._show_local_error(str(exc))
            return
        if self.worker.configuration_changed(config) and self._has_session:
            self._insert_timeline(
                "Info",
                "Session",
                "Runtime configuration changed; starting a new context",
                "The workspace, model, protocol, or memory configuration changed.",
                "muted",
            )
            self._has_session = False
        self._tool_items.clear()
        self._round_items.clear()
        self._active_items.clear()
        continue_session = self.continue_check.isChecked() and self._has_session
        try:
            run_id = self.worker.start(config, task, continue_session=continue_session)
        except (OSError, TypeError, ValueError) as exc:
            self._show_local_error(str(exc))
            return
        if run_id is None:
            self._set_status("Already running", "warning")
            return
        self.current_run_id = run_id
        if workspace != self._terminal_workspace:
            self._reset_terminal(workspace)
        if workspace != self._files_workspace:
            self._reset_files(workspace)
        self.model_entry.setText(config.model)
        self.protocol_combo.setCurrentText(config.wire_api)
        safe_task = redact_secrets(task)
        self._current_task_text = safe_task
        self._task_item = self._insert_timeline("Queued", "Task", safe_task, safe_task, "running")
        self.task_input.clear()
        self._set_status("Running", "running")
        self._set_stats(
            f"model={config.model}  ·  protocol={config.wire_api}  ·  "
            f"memory={'on' if config.memory_enabled else 'off'}"
        )
        self._set_running(True)

    def _stop_task(self) -> None:
        if self.worker.cancel():
            self._set_status("Stopping", "warning")
            self.status_label.setToolTip("Stopping after the current operation")
            self.stop_button.setEnabled(False)

    def _set_running(self, running: bool) -> None:
        for widget in (
            self.workspace_entry,
            self.model_entry,
            self.protocol_combo,
            self.browse_button,
            self.memory_check,
            self.new_button,
            self.run_button,
            self.continue_check,
        ):
            widget.setEnabled(not running)
        self.stop_button.setEnabled(running)
        if running:
            self.progress.setRange(0, 0)
            self.progress.setVisible(True)
        else:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            self.progress.setVisible(False)

    def _set_status(self, text: str, tone: str) -> None:
        self.status_label.setText(text)
        self.status_label.setToolTip("")
        self.status_label.setProperty("tone", tone)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _set_stats(self, text: str) -> None:
        self.stats_label.setText(text)
        self.stats_label.setToolTip(text)

    def _drain_queue(self) -> None:
        processed = 0
        while processed < 200:
            try:
                envelope = self.event_queue.get_nowait()
            except queue.Empty:
                break
            processed += 1
            if envelope.run_id != self.current_run_id:
                if envelope.kind in {"result", "error"}:
                    self.worker.acknowledge_terminal(envelope.run_id)
                continue
            self._handle_envelope(envelope)
        self._drain_file_queues()

    def _handle_envelope(self, envelope: UiEnvelope) -> None:
        if envelope.kind == "event" and isinstance(envelope.payload, AgentEvent):
            self._render_agent_event(envelope.payload)
            return
        if envelope.kind == "file_diff" and isinstance(envelope.payload, dict):
            self._append_changes(
                str(envelope.payload.get("path", "changed file")),
                str(envelope.payload.get("diff", "")),
            )
            return
        if envelope.kind == "result" and isinstance(envelope.payload, AgentResult):
            self.worker.acknowledge_terminal(envelope.run_id)
            self._finish_result(envelope.payload)
            return
        if envelope.kind == "error":
            self.worker.acknowledge_terminal(envelope.run_id)
            self._finish_error(str(envelope.payload))

    def _render_agent_event(self, event: AgentEvent) -> None:
        data = event.data
        if event.kind == "task_started":
            if self._task_item:
                task = str(data.get("task", self._current_task_text))
                self._update_timeline(self._task_item, "Running", "Task", task, task, "running")
        elif event.kind == "model_start":
            round_number = int(data.get("round", 0))
            item = self._insert_timeline(
                "Running", "Model", f"Round {round_number}", f"Model round {round_number}", "running"
            )
            self._round_items[round_number] = item
            self._active_items.add(item)
        elif event.kind == "model_end":
            round_number = int(data.get("round", 0))
            elapsed = int(data.get("elapsed_ms", 0)) / 1000
            tokens = int(data.get("input_tokens", 0)) + int(data.get("output_tokens", 0))
            details = json.dumps(data, ensure_ascii=False, indent=2)
            item = self._round_items.get(round_number)
            if item:
                self._update_timeline(
                    item,
                    "Responded",
                    "Model",
                    f"Round {round_number} · {tokens} tokens · {elapsed:.1f}s",
                    details,
                    "muted",
                )
                self._active_items.discard(item)
        elif event.kind == "assistant_text":
            text = str(data.get("text", "")).strip()
            if text:
                self._insert_timeline("Note", "Assistant", self._one_line(text), text, "muted")
        elif event.kind == "tool_start":
            call_id = str(data.get("call_id", ""))
            name = str(data.get("name", "tool"))
            arguments = data.get("arguments", {})
            details = json.dumps(arguments, ensure_ascii=False, indent=2)
            item = self._insert_timeline(
                "Running", name, self._argument_summary(arguments), details, "running"
            )
            self._tool_items[call_id] = item
            self._active_items.add(item)
            if name == "run_command":
                self._start_terminal_command(call_id, arguments)
            elif name in {"edit_file", "write_file"} and isinstance(arguments, dict):
                path = arguments.get("path")
                if isinstance(path, str) and path:
                    self._file_tool_paths[call_id] = path
        elif event.kind == "tool_output":
            call_id = str(data.get("call_id", ""))
            name = str(data.get("name", ""))
            stream = str(data.get("stream", ""))
            text = str(data.get("text", ""))
            if name == "run_command":
                self._append_terminal_output(call_id, stream, text)
        elif event.kind == "tool_end":
            call_id = str(data.get("call_id", ""))
            name = str(data.get("name", "tool"))
            output = str(data.get("output", ""))
            terminal_result = data.get("terminal_result")
            cancelled = (
                name == "run_command"
                and isinstance(terminal_result, dict)
                and terminal_result.get("cancelled") is True
            )
            if isinstance(data.get("output_ok"), bool):
                ok = bool(data["output_ok"])
                summary = str(data.get("output_summary", "Completed"))
            else:
                ok, summary = summarize_tool_output(output)
            state = "Stopped" if cancelled else ("Succeeded" if ok else "Failed")
            tone = "warning" if cancelled else ("success" if ok else "error")
            if cancelled:
                summary = "Cancelled by user"
            item = self._tool_items.get(call_id)
            details = output
            if item:
                self._active_items.discard(item)
                prior = self._entry_details.get(item, "")
                details = f"Arguments\n{prior}\n\nResult\n{output}"
                self._update_timeline(
                    item,
                    state,
                    name,
                    summary,
                    details,
                    tone,
                )
            else:
                self._insert_timeline(
                    state,
                    name,
                    summary,
                    details,
                    tone,
                )
            if name == "run_command":
                self._finish_terminal_command(call_id, data)
            target_path = self._file_tool_paths.pop(call_id, None)
            if ok and name in {"edit_file", "write_file"}:
                self._request_files_refresh(target_path)
            elif name == "run_command":
                self._request_files_refresh(delay_ms=250)
        elif event.kind == "context_compacted":
            removed = int(data.get("removed", 0))
            self._insert_timeline(
                "Info", "Context", f"Removed {removed} old messages", json.dumps(data), "muted"
            )
        elif event.kind == "completion_repair":
            self._insert_timeline(
                "Warning",
                "Protocol",
                "Requested an explicit completion status",
                json.dumps(data),
                "warning",
            )
        elif event.kind == "memory_committed":
            count = int(data.get("count", 0))
            self._memory_commit_count += count
            self.inspector.setTabText(
                self.inspector.indexOf(self.memory_text),
                f"Memory ({self._memory_commit_count})",
            )
            self._insert_timeline(
                "Saved",
                "Memory",
                f"Committed {count} verified entries",
                json.dumps(data, indent=2),
                "success",
            )
        elif event.kind == "memory_error":
            error = str(data.get("error", "Memory update failed"))
            self._insert_timeline("Failed", "Memory", self._one_line(error), error, "error")
        elif event.kind == "loop_stopped":
            reason = str(data.get("reason", "Agent loop stopped"))
            self._insert_timeline("Stopped", "Agent", self._one_line(reason), reason, "error")
        elif event.kind == "run_cancelled":
            reason = str(data.get("reason", "Stopped by user"))
            self._insert_timeline("Stopped", "Agent", self._one_line(reason), reason, "warning")

    def _finish_result(self, result: AgentResult) -> None:
        if result.cancelled:
            state, tag, tone = "Stopped", "warning", "warning"
        elif result.success:
            state, tag, tone = "Completed", "success", "success"
        else:
            state, tag, tone = "Blocked", "error", "error"
        stats = (
            f"requests={result.rounds}  ·  tools={result.tool_calls}  ·  "
            f"tokens={result.input_tokens}+{result.output_tokens}  ·  "
            f"elapsed={result.elapsed_ms / 1000:.1f}s"
        )
        details = f"{result.answer}\n\n{stats}"
        self._settle_active_items(
            "Stopped" if result.cancelled else "Failed",
            "warning" if result.cancelled else "error",
        )
        self._settle_terminal_commands("stopped" if result.cancelled else "interrupted")
        self._finalize_task_item(state, tag, details)
        self._insert_timeline(state, "Result", self._one_line(result.answer), details, tag)
        self._set_status(state, tone)
        self._set_stats(stats)
        self._has_session = True
        self._set_running(False)
        self._refresh_memory()
        self._request_files_refresh(delay_ms=0)
        self._close_after_terminal_event()

    def _finish_error(self, error: str) -> None:
        safe_error = error.strip() or "The Agent stopped with an unknown error."
        self._settle_active_items("Failed", "error")
        self._settle_terminal_commands("interrupted")
        self._finalize_task_item("Error", "error", safe_error)
        self._insert_timeline("Error", "Runtime", self._one_line(safe_error), safe_error, "error")
        self._set_status("Runtime error", "error")
        self._set_stats(self._one_line(safe_error))
        self._has_session = True
        self._set_running(False)
        self._refresh_memory()
        self._request_files_refresh(delay_ms=0)
        self._close_after_terminal_event()

    def _show_local_error(self, error: str) -> None:
        safe_error = error.strip() or "Invalid configuration"
        self._insert_timeline(
            "Error", "Configuration", self._one_line(safe_error), safe_error, "error"
        )
        self._set_status("Configuration error", "error")
        self._set_stats(self._one_line(safe_error))

    def _refresh_memory(self) -> None:
        try:
            expected_workspace = Path(self.workspace_entry.text()).expanduser().resolve()
            overview = self.worker.memory_overview(expected_workspace=expected_workspace)
        except (OSError, TypeError, ValueError) as exc:
            overview = f"Memory could not be loaded: {exc}"
        self._set_memory_text(overview)

    def _reset_files(self, workspace: Path) -> None:
        try:
            resolved = workspace.expanduser().resolve(strict=False)
        except (OSError, ValueError):
            resolved = workspace
        self._file_refresh_timer.stop()
        self._file_filter_timer.stop()
        self._file_generation += 1
        self._file_preview_generation += 1
        self._file_selection_revision += 1
        self._clear_queue(self._file_preview_requests)
        self._clear_queue(self._file_preview_queue)
        self._files_workspace = resolved
        self._file_index = None
        self._file_nodes.clear()
        self._file_entries.clear()
        self._file_items.clear()
        self._file_tool_paths.clear()
        self._file_expanded_paths = {""}
        self._selected_file_path = None
        self._pending_file_selection = None
        self._pending_file_selection_revision = None
        previous = self.file_search_entry.blockSignals(True)
        self.file_search_entry.clear()
        self.file_search_entry.blockSignals(previous)
        self.file_tree.clear()
        self.file_count_label.setText("Loading")
        self.inspector.setTabText(self.inspector.indexOf(self.files_tab), "Files")
        self._set_file_preview_message("Select a file", "Select a file to preview.")
        self._start_file_refresh(resolved, None)

    def _request_files_refresh(
        self,
        relative_path: str | None = None,
        *,
        delay_ms: int = 160,
    ) -> None:
        if relative_path:
            self._pending_file_selection = relative_path.replace("\\", "/")
            self._pending_file_selection_revision = self._file_selection_revision
        if delay_ms <= 0:
            self._file_refresh_timer.stop()
            self._refresh_files()
        else:
            self._file_refresh_timer.start(delay_ms)

    def _refresh_files(self, _checked: bool = False) -> None:
        self._file_refresh_timer.stop()
        selection = self._pending_file_selection or self._selected_file_path
        selection_revision = self._pending_file_selection_revision
        self._pending_file_selection = None
        self._pending_file_selection_revision = None
        self._start_file_refresh(
            self._files_workspace,
            selection,
            selection_revision=selection_revision,
        )

    def _start_file_refresh(
        self,
        workspace: Path,
        selection: str | None,
        *,
        selection_revision: int | None = None,
    ) -> None:
        if self._closed or self._file_workers_stopped:
            return
        self._file_generation += 1
        generation = self._file_generation
        self.file_refresh_button.setEnabled(False)
        self.file_count_label.setText("Loading")
        self._offer_latest(
            self._file_index_requests,
            (
                generation,
                workspace,
                selection,
                self._file_selection_revision
                if selection_revision is None
                else selection_revision,
            ),
        )

    def _file_index_worker_loop(self) -> None:
        while True:
            request = self._file_index_requests.get()
            if request is None:
                return
            generation, workspace, selection, selection_revision = request
            try:
                index = scan_workspace(workspace)
            except Exception:
                index = WorkspaceIndex(
                    workspace,
                    (),
                    error="Workspace files could not be loaded.",
                )
            if self._closed:
                continue
            self._offer_latest(
                self._file_index_queue,
                (generation, workspace, index, selection, selection_revision),
            )

    @staticmethod
    def _clear_queue(target: queue.Queue[Any]) -> None:
        while True:
            try:
                target.get_nowait()
            except queue.Empty:
                return

    @classmethod
    def _offer_latest(cls, target: queue.Queue[Any], value: Any) -> None:
        cls._clear_queue(target)
        try:
            target.put_nowait(value)
        except queue.Full:
            pass

    def _stop_file_workers(self) -> None:
        if self._file_workers_stopped:
            return
        self._file_workers_stopped = True
        self._offer_latest(self._file_index_requests, None)
        self._offer_latest(self._file_preview_requests, None)

    def _drain_file_queues(self) -> None:
        while True:
            try:
                (
                    generation,
                    workspace,
                    index,
                    selection,
                    selection_revision,
                ) = self._file_index_queue.get_nowait()
            except queue.Empty:
                break
            if (
                generation != self._file_generation
                or workspace != self._files_workspace
                or self._closed
            ):
                continue
            self.file_refresh_button.setEnabled(True)
            latest_selection = (
                selection
                if selection_revision == self._file_selection_revision
                else self._selected_file_path
            )
            self._apply_file_index(index, latest_selection)

        while True:
            try:
                generation, workspace, preview = self._file_preview_queue.get_nowait()
            except queue.Empty:
                break
            if (
                generation != self._file_preview_generation
                or workspace != self._files_workspace
                or preview.relative_path != self._selected_file_path
                or self._closed
            ):
                continue
            self._apply_file_preview(preview)

    def _apply_file_index(
        self,
        index: WorkspaceIndex,
        selection: str | None,
    ) -> None:
        self._file_preview_generation += 1
        self._clear_queue(self._file_preview_requests)
        self._clear_queue(self._file_preview_queue)
        self._file_index = index
        self._file_entries = {entry.relative_path: entry for entry in index.files}
        self._file_nodes = self._build_file_nodes(index.files)
        total = len(index.files)
        suffix = "+" if index.truncated else ""
        self.file_count_label.setText(f"{total}{suffix} files")
        self.inspector.setTabText(
            self.inspector.indexOf(self.files_tab),
            f"Files ({total}{suffix})" if total else "Files",
        )
        if index.error:
            self._selected_file_path = None
            self._render_file_message(index.error)
            self._set_file_preview_message("Files unavailable", index.error)
            return
        self._render_file_tree(selection=selection)

    def _build_file_nodes(
        self,
        entries: tuple[WorkspaceFile, ...],
    ) -> dict[str, tuple[_FileTreeNode, ...]]:
        definitions: dict[str, dict[str, tuple[str, bool, str, bool]]] = {}
        directory_statuses: dict[str, set[str]] = {}
        for entry in entries:
            parts = PurePosixPath(entry.relative_path).parts
            parent = ""
            for index, name in enumerate(parts):
                current = "/".join(parts[: index + 1])
                is_directory = index < len(parts) - 1
                definitions.setdefault(parent, {})[current] = (
                    name,
                    is_directory,
                    "" if is_directory else entry.git_status,
                    False if is_directory else entry.is_link,
                )
                if is_directory:
                    definitions.setdefault(current, {})
                    if entry.git_status:
                        directory_statuses.setdefault(current, set()).add(
                            entry.git_status
                        )
                parent = current

        nodes: dict[str, tuple[_FileTreeNode, ...]] = {}
        for parent, children in definitions.items():
            values = []
            for relative_path, (name, is_directory, status, is_link) in children.items():
                if is_directory:
                    status = self._aggregate_file_status(
                        directory_statuses.get(relative_path, set())
                    )
                values.append(
                    _FileTreeNode(
                        relative_path,
                        name,
                        is_directory,
                        status,
                        is_link,
                    )
                )
            nodes[parent] = tuple(
                sorted(
                    values,
                    key=lambda node: (not node.is_directory, node.name.casefold()),
                )
            )
        return nodes

    @staticmethod
    def _aggregate_file_status(statuses: set[str]) -> str:
        joined = "".join(statuses)
        for marker in ("U", "D", "A", "R", "C", "M", "T", "?"):
            if marker in joined:
                return marker
        return ""

    @staticmethod
    def _display_file_status(status: str) -> str:
        if status == "??":
            return "?"
        compact = status.replace(" ", "")
        return compact[:2]

    def _render_file_tree(self, *, selection: str | None = None) -> None:
        if self.file_search_entry.text().strip():
            self._apply_file_filter(selection=selection)
            return
        scroll_value = self.file_tree.verticalScrollBar().value()
        old_blocked = self.file_tree.blockSignals(True)
        try:
            self.file_tree.clear()
            self.file_tree.setRootIsDecorated(True)
            self._file_items.clear()
            if not self._file_index or not self._file_index.files:
                self._selected_file_path = None
                self._add_file_message_item("No visible files")
                self._set_file_preview_message(
                    "Select a file",
                    "Select a file to preview.",
                )
                return
            root_name = self._files_workspace.name or str(self._files_workspace)
            root_node = _FileTreeNode("", root_name, True)
            root_item = self._make_file_item(root_node)
            root_item.setToolTip(
                0,
                self._file_display_text(str(self._files_workspace), 500),
            )
            self.file_tree.addTopLevelItem(root_item)
            self._populate_file_item(root_item)
            root_item.setExpanded(True)
            target = selection or self._selected_file_path
            expanded = set(self._file_expanded_paths)
            if target:
                parts = PurePosixPath(target).parts[:-1]
                expanded.update("/".join(parts[: index + 1]) for index in range(len(parts)))
            for path in sorted(expanded, key=lambda value: (value.count("/"), value)):
                item = self._file_items.get(path)
                if item is None:
                    continue
                self._populate_file_item(item)
                item.setExpanded(True)
            if target:
                self._materialize_file_path(target)
                selected_item = self._file_items.get(target)
                if selected_item is not None:
                    self.file_tree.setCurrentItem(selected_item)
        finally:
            self.file_tree.blockSignals(old_blocked)
        self.file_tree.verticalScrollBar().setValue(
            min(scroll_value, self.file_tree.verticalScrollBar().maximum())
        )
        if self.file_tree.currentItem() is not None:
            self._on_file_tree_select()
        else:
            self._selected_file_path = None
            self._file_preview_generation += 1
            self._set_file_preview_message("Select a file", "Select a file to preview.")

    def _render_file_message(self, message: str) -> None:
        old_blocked = self.file_tree.blockSignals(True)
        try:
            self.file_tree.clear()
            self._file_items.clear()
            self._add_file_message_item(message)
        finally:
            self.file_tree.blockSignals(old_blocked)

    def _add_file_message_item(self, message: str) -> None:
        item = QTreeWidgetItem([self._one_line(message, 160), ""])
        item.setData(0, self.FILE_KIND_ROLE, "message")
        item.setForeground(0, QBrush(QColor(self.COLORS["muted"])))
        self.file_tree.addTopLevelItem(item)

    def _make_file_item(self, node: _FileTreeNode) -> QTreeWidgetItem:
        label = self._file_display_text(node.name, 160)
        status = self._display_file_status(node.git_status)
        item = QTreeWidgetItem([label, status])
        kind = "directory" if node.is_directory else ("link" if node.is_link else "file")
        item.setData(0, self.FILE_PATH_ROLE, node.relative_path)
        item.setData(0, self.FILE_KIND_ROLE, kind)
        item.setData(0, self.FILE_LOADED_ROLE, False)
        item.setToolTip(
            0,
            self._file_display_text(node.relative_path or label, 500),
        )
        if node.is_directory:
            icon = QStyle.StandardPixmap.SP_DirIcon
        elif node.is_link:
            icon = getattr(
                QStyle.StandardPixmap,
                "SP_FileLinkIcon",
                QStyle.StandardPixmap.SP_FileIcon,
            )
        else:
            icon = QStyle.StandardPixmap.SP_FileIcon
        item.setIcon(0, self.style().standardIcon(icon))
        self._apply_file_status_style(item, status)
        self._file_items[node.relative_path] = item
        if node.is_directory and self._file_nodes.get(node.relative_path):
            placeholder = QTreeWidgetItem(["", ""])
            placeholder.setData(0, self.FILE_KIND_ROLE, "placeholder")
            item.addChild(placeholder)
        return item

    def _apply_file_status_style(self, item: QTreeWidgetItem, status: str) -> None:
        if not status:
            return
        if "U" in status or "D" in status:
            color = self.COLORS["danger"]
        elif "A" in status:
            color = self.COLORS["success"]
        elif "?" in status:
            color = self.COLORS["running"]
        else:
            color = self.COLORS["warning"]
        item.setForeground(1, QBrush(QColor(color)))
        font = item.font(1)
        font.setWeight(QFont.Weight.DemiBold)
        item.setFont(1, font)

    def _populate_file_item(self, item: QTreeWidgetItem) -> None:
        if item.data(0, self.FILE_KIND_ROLE) != "directory":
            return
        if bool(item.data(0, self.FILE_LOADED_ROLE)):
            return
        relative_path = str(item.data(0, self.FILE_PATH_ROLE) or "")
        item.takeChildren()
        for node in self._file_nodes.get(relative_path, ()):
            item.addChild(self._make_file_item(node))
        item.setData(0, self.FILE_LOADED_ROLE, True)

    def _materialize_file_path(self, relative_path: str) -> None:
        parts = PurePosixPath(relative_path).parts
        parent = ""
        for index in range(len(parts)):
            parent_item = self._file_items.get(parent)
            if parent_item is None:
                return
            self._populate_file_item(parent_item)
            if index < len(parts) - 1:
                parent_item.setExpanded(True)
            parent = "/".join(parts[: index + 1])

    def _on_file_item_expanded(self, item: QTreeWidgetItem) -> None:
        if item.data(0, self.FILE_KIND_ROLE) != "directory":
            return
        path = str(item.data(0, self.FILE_PATH_ROLE) or "")
        self._file_expanded_paths.add(path)
        self._populate_file_item(item)

    def _on_file_item_collapsed(self, item: QTreeWidgetItem) -> None:
        if item.data(0, self.FILE_KIND_ROLE) != "directory":
            return
        path = str(item.data(0, self.FILE_PATH_ROLE) or "")
        if path:
            self._file_expanded_paths.discard(path)

    def _schedule_file_filter(self, _text: str) -> None:
        self._file_filter_timer.start()

    def _apply_file_filter(self, *, selection: str | None = None) -> None:
        if self._file_index is None:
            return
        query = self.file_search_entry.text().strip().casefold()
        if not query:
            self.file_count_label.setText(
                f"{len(self._file_index.files)}{'+' if self._file_index.truncated else ''} files"
            )
            self._render_file_tree(selection=selection)
            return
        matches = [
            entry
            for entry in self._file_index.files
            if query in entry.relative_path.casefold()
        ]
        limited = matches[: self.MAX_FILE_SEARCH_RESULTS]
        old_blocked = self.file_tree.blockSignals(True)
        try:
            self.file_tree.clear()
            self.file_tree.setRootIsDecorated(False)
            self._file_items.clear()
            for entry in limited:
                node = _FileTreeNode(
                    entry.relative_path,
                    entry.relative_path,
                    False,
                    entry.git_status,
                    entry.is_link,
                )
                self.file_tree.addTopLevelItem(self._make_file_item(node))
            if not limited:
                self._add_file_message_item("No matching files")
            target = selection or self._selected_file_path
            if target and target in self._file_items:
                self.file_tree.setCurrentItem(self._file_items[target])
        finally:
            self.file_tree.blockSignals(old_blocked)
        suffix = "+" if len(matches) > len(limited) else ""
        self.file_count_label.setText(f"{len(limited)}{suffix} matches")
        if self.file_tree.currentItem() is not None:
            self._on_file_tree_select()
        else:
            self._selected_file_path = None
            self._file_preview_generation += 1
            self._set_file_preview_message("Select a file", "Select a file to preview.")

    def _on_file_tree_select(self) -> None:
        selected = self.file_tree.selectedItems()
        if not selected:
            return
        self._file_selection_revision += 1
        item = selected[0]
        kind = str(item.data(0, self.FILE_KIND_ROLE) or "")
        relative_path = str(item.data(0, self.FILE_PATH_ROLE) or "")
        if kind == "directory":
            self._selected_file_path = None
            self._file_preview_generation += 1
            label = relative_path or (self._files_workspace.name or str(self._files_workspace))
            self._set_file_preview_message(label, "Select a file to preview.", "Folder")
            return
        if kind not in {"file", "link"} or not relative_path:
            return
        self._selected_file_path = relative_path
        if kind == "link":
            self._set_file_preview_message(
                relative_path,
                "Links are not previewed.",
                "Link",
            )
            return
        self._start_file_preview(relative_path)

    def _start_file_preview(self, relative_path: str) -> None:
        if self._closed or self._file_workers_stopped:
            return
        self._file_preview_generation += 1
        generation = self._file_preview_generation
        workspace = self._files_workspace
        self._set_file_preview_message(relative_path, "Loading preview.", "Loading")
        self._offer_latest(
            self._file_preview_requests,
            (generation, workspace, relative_path),
        )

    def _file_preview_worker_loop(self) -> None:
        while True:
            request = self._file_preview_requests.get()
            if request is None:
                return
            generation, workspace, relative_path = request
            try:
                preview = preview_workspace_file(workspace, relative_path)
            except Exception:
                preview = WorkspaceFilePreview(relative_path, "unreadable")
            if self._closed:
                continue
            self._offer_latest(
                self._file_preview_queue,
                (generation, workspace, preview),
            )

    def _apply_file_preview(self, preview: WorkspaceFilePreview) -> None:
        if preview.status == "text":
            self.file_preview_path.setText(
                self._file_display_text(preview.relative_path, 220)
            )
            self.file_preview_path.setToolTip(
                self._file_display_text(preview.relative_path, 500)
            )
            meta = f"{self._format_file_size(preview.size_bytes)} | {preview.line_count} lines"
            if preview.truncated:
                meta += " | limited"
            self.file_preview_meta.setText(meta)
            rendered = self._number_file_preview(preview.text, preview.line_count)
            if preview.truncated:
                rendered = f"{rendered}\n\n[preview truncated]".strip()
            self._replace_text(self.file_preview_text, rendered or "[empty file]")
            return
        messages = {
            "binary": "Binary or non-UTF-8 file. Preview is unavailable.",
            "too_large": "File is too large for automatic preview.",
            "sensitive": "Preview is hidden for this sensitive path.",
            "outside": "Preview path is outside the workspace.",
            "missing": "File no longer exists.",
            "directory": "Select a file to preview.",
            "link": "Links are not previewed.",
            "unreadable": "File could not be read.",
        }
        meta = self._format_file_size(preview.size_bytes) if preview.size_bytes else ""
        self._set_file_preview_message(
            preview.relative_path,
            messages.get(preview.status, "Preview is unavailable."),
            meta,
        )

    def _set_file_preview_message(
        self,
        path: str,
        message: str,
        meta: str = "",
    ) -> None:
        safe_path = self._file_display_text(path, 220)
        self.file_preview_path.setText(safe_path)
        self.file_preview_path.setToolTip(self._file_display_text(path, 500))
        self.file_preview_meta.setText(meta)
        self._replace_text(self.file_preview_text, message)

    @classmethod
    def _file_display_text(cls, value: str, limit: int) -> str:
        safe_value = redact_secrets(value)
        safe_value = cls.CONTROL_CHAR_RE.sub("", safe_value)
        safe_value = cls.BIDI_CONTROL_RE.sub("", safe_value)
        return cls._one_line(safe_value, limit)

    @staticmethod
    def _format_file_size(size: int) -> str:
        if size < 1_024:
            return f"{size} B"
        if size < 1_024 * 1_024:
            return f"{size / 1_024:.1f} KB"
        return f"{size / (1_024 * 1_024):.1f} MB"

    @staticmethod
    def _number_file_preview(text: str, total_lines: int) -> str:
        if not text:
            return ""
        lines = text.split("\n")
        width = len(str(max(total_lines, len(lines), 1)))
        return "\n".join(
            f"{number:>{width}} | {line}"
            for number, line in enumerate(lines, start=1)
        )

    def _insert_timeline(
        self, state: str, action: str, summary: str, details: str, tag: str
    ) -> str:
        current = self.timeline.currentItem()
        follow_latest = current is None or self.timeline.indexOfTopLevelItem(current) == (
            self.timeline.topLevelItemCount() - 1
        )
        self._item_sequence += 1
        item_id = f"item-{self._item_sequence}"
        item = QTreeWidgetItem([state, action, self._one_line(summary)])
        item.setData(0, Qt.ItemDataRole.UserRole, item_id)
        item.setToolTip(2, self._one_line(summary, 500))
        self._apply_item_style(item, tag)
        self.timeline.addTopLevelItem(item)
        self._timeline_items[item_id] = item
        self._entry_details[item_id] = details
        self._trim_timeline()
        if follow_latest:
            self.timeline.setCurrentItem(item)
            self.timeline.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)
            self._set_details_text(details)
        self.timeline_count_label.setText(f"{self.timeline.topLevelItemCount()} events")
        return item_id

    def _update_timeline(
        self,
        item_id: str,
        state: str,
        action: str,
        summary: str,
        details: str,
        tag: str,
    ) -> None:
        item = self._timeline_items.get(item_id)
        if item is None:
            return
        item.setText(0, state)
        item.setText(1, action)
        item.setText(2, self._one_line(summary))
        item.setToolTip(2, self._one_line(summary, 500))
        self._apply_item_style(item, tag)
        self._entry_details[item_id] = details
        if self.timeline.currentItem() is item:
            self._set_details_text(details)
            self.timeline.scrollToItem(item, QAbstractItemView.ScrollHint.EnsureVisible)

    def _apply_item_style(self, item: QTreeWidgetItem, tag: str) -> None:
        background, foreground, icon_type = self.ROW_STYLES.get(tag, self.ROW_STYLES["muted"])
        for column in range(3):
            item.setBackground(column, QBrush(QColor(background)))
            item.setForeground(
                column,
                QBrush(QColor(foreground if column == 0 else self.COLORS["text"])),
            )
        state_font = item.font(0)
        state_font.setWeight(QFont.Weight.DemiBold)
        item.setFont(0, state_font)
        item.setIcon(0, self.style().standardIcon(icon_type))

    def _on_timeline_select(self) -> None:
        selected = self.timeline.selectedItems()
        if not selected:
            return
        item_id = str(selected[0].data(0, Qt.ItemDataRole.UserRole) or "")
        self._set_details_text(self._entry_details.get(item_id, ""))

    def _append_changes(self, path: str, diff: str) -> None:
        current = self.changes_text.toPlainText().strip()
        if current == "No file changes captured.":
            current = ""
        addition = f"File: {path}\n{diff.rstrip()}"
        combined = f"{current}\n\n{addition}".strip()
        if len(combined) > self.MAX_CHANGES_CHARS:
            combined = "[Earlier changes omitted]\n\n" + combined[-self.MAX_CHANGES_CHARS :]
        self._set_changes_text(combined)
        self._change_count += 1
        self.inspector.setTabText(
            self.inspector.indexOf(self.changes_text), f"Changes ({self._change_count})"
        )

    def _reset_terminal(self, workspace: Path) -> None:
        self.terminal_text.clear()
        self._terminal_commands.clear()
        self._terminal_command_count = 0
        self._terminal_auto_opened = False
        self._terminal_workspace = workspace
        self.inspector.setTabText(self.inspector.indexOf(self.terminal_text), "Terminal")

    def _start_terminal_command(self, call_id: str, arguments: object) -> None:
        if not call_id or not isinstance(arguments, dict):
            return
        command = str(arguments.get("command", "")).strip()
        if not command:
            return
        if call_id in self._terminal_commands:
            self._append_terminal("[interrupted] duplicate command identifier\n\n")
        cwd = str(arguments.get("cwd", ".") or ".")
        if self.terminal_text.document().characterCount() > 1:
            self._append_terminal("\n")
        prompt = f"PS {cwd}> " if os.name == "nt" else f"$ {cwd}> "
        rendered, _ = self._format_terminal_lines("command", f"{prompt}{command}\n", True)
        self._append_terminal(rendered)
        self._terminal_commands[call_id] = _TerminalCommandState()
        self._terminal_command_count += 1
        self.inspector.setTabText(
            self.inspector.indexOf(self.terminal_text),
            f"Terminal ({self._terminal_command_count})",
        )
        if not self._terminal_auto_opened:
            self.inspector.setCurrentWidget(self.terminal_text)
            self._terminal_auto_opened = True

    def _append_terminal_output(self, call_id: str, stream: str, text: str) -> None:
        state = self._terminal_commands.get(call_id)
        if state is None or stream not in {"stdout", "stderr"}:
            return
        if not text:
            return
        if stream in state.discarding:
            newline = text.find("\n")
            if newline < 0:
                return
            state.discarding.discard(stream)
            text = text[newline + 1 :]
        buffered = state.pending[stream] + text
        state.pending[stream] = ""
        while buffered:
            newline = buffered.find("\n")
            if newline < 0:
                if len(buffered) > self.MAX_TERMINAL_CHUNK_CHARS:
                    self._render_terminal_output_piece(
                        state,
                        stream,
                        "[output line omitted: exceeds display limit]\n",
                    )
                    state.discarding.add(stream)
                else:
                    state.pending[stream] = buffered
                return
            line = buffered[: newline + 1]
            buffered = buffered[newline + 1 :]
            if len(line) > self.MAX_TERMINAL_CHUNK_CHARS:
                line = "[output line omitted: exceeds display limit]\n"
            self._render_terminal_output_piece(state, stream, line)

    def _render_terminal_output_piece(
        self, state: _TerminalCommandState, stream: str, text: str
    ) -> None:
        switched_mid_line = not state.line_start and state.active_source != stream
        rendered, line_start = self._format_terminal_lines(
            stream, text, True if switched_mid_line else state.line_start
        )
        if not rendered:
            return
        if switched_mid_line:
            rendered = "\n" + rendered
        state.streamed.add(stream)
        state.line_start = line_start
        state.active_source = None if line_start else stream
        self._append_terminal(rendered)

    def _finish_terminal_command(self, call_id: str, data: dict[str, Any]) -> None:
        state = self._terminal_commands.get(call_id)
        if state is None:
            return
        self._flush_terminal_output(state)
        self._terminal_commands.pop(call_id, None)
        result = data.get("terminal_result")
        if isinstance(result, dict):
            stdout = str(result.get("stdout", ""))
            stderr = str(result.get("stderr", ""))
            if stdout and "stdout" not in state.streamed:
                self._append_terminal_output_fallback("stdout", stdout)
            if stderr and "stderr" not in state.streamed:
                self._append_terminal_output_fallback("stderr", stderr)
            if result.get("truncated") and not state.streamed and not stdout and not stderr:
                self._append_terminal("[earlier output omitted]\n")
        current = self.terminal_text.toPlainText()
        if current and not current.endswith("\n"):
            self._append_terminal("\n")
        if isinstance(result, dict) and result.get("parsed") and result.get("cancelled"):
            self._append_terminal("[stopped]\n\n")
        elif isinstance(result, dict) and result.get("parsed") and result.get("ok"):
            exit_code = result.get("exit_code")
            label = str(exit_code) if type(exit_code) is int else "unknown"
            self._append_terminal(f"[exit {label}]\n\n")
        else:
            error = (
                str(result.get("error", "Command result unavailable"))
                if isinstance(result, dict)
                else "Command result unavailable"
            )
            rendered, _ = self._format_terminal_lines("error", error, True)
            if not rendered:
                rendered = "[error] Command failed"
            self._append_terminal(rendered.rstrip("\n") + "\n\n")

    def _append_terminal_output_fallback(self, stream: str, text: str) -> None:
        rendered, _ = self._format_terminal_lines(stream, text, True)
        current = self.terminal_text.toPlainText()
        if rendered and current and not current.endswith("\n"):
            rendered = "\n" + rendered
        self._append_terminal(rendered)

    def _flush_terminal_output(self, state: _TerminalCommandState) -> None:
        for stream in ("stdout", "stderr"):
            value = state.pending[stream]
            state.pending[stream] = ""
            if value:
                self._render_terminal_output_piece(state, stream, value)
        state.discarding.clear()

    @classmethod
    def _format_terminal_lines(
        cls, source: str, value: str, at_line_start: bool
    ) -> tuple[str, bool]:
        cleaned = cls._clean_terminal_text(value)
        if not cleaned:
            return "", at_line_start
        rendered: list[str] = []
        for line in cleaned.splitlines(keepends=True):
            if at_line_start:
                rendered.append(f"[{source}] ")
            rendered.append(line)
            at_line_start = line.endswith("\n")
        return "".join(rendered), at_line_start

    def _settle_terminal_commands(self, state: str) -> None:
        if not self._terminal_commands:
            return
        for command_state in tuple(self._terminal_commands.values()):
            self._flush_terminal_output(command_state)
            current = self.terminal_text.toPlainText()
            if current and not current.endswith("\n"):
                self._append_terminal("\n")
            self._append_terminal(f"[{state}]\n\n")
        self._terminal_commands.clear()

    def _append_terminal(self, value: str) -> None:
        safe_value = self._clean_terminal_text(value)
        if not safe_value:
            return
        scroll = self.terminal_text.verticalScrollBar()
        follow_latest = scroll.value() >= scroll.maximum() - 2
        previous_scroll_value = scroll.value()
        cursor = QTextCursor(self.terminal_text.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(safe_value)
        if self.terminal_text.document().characterCount() > self.MAX_TERMINAL_CHARS:
            marker = "[earlier output omitted]\n"
            current = self.terminal_text.toPlainText()
            retained = current[-self.TERMINAL_TRIM_CHARS :]
            first_newline = retained.find("\n")
            if first_newline >= 0:
                retained = retained[first_newline + 1 :]
            trimmed = marker + retained
            self.terminal_text.setPlainText(trimmed)
        if follow_latest:
            scroll.setValue(scroll.maximum())
        else:
            scroll.setValue(min(previous_scroll_value, scroll.maximum()))

    @classmethod
    def _clean_terminal_text(cls, value: str) -> str:
        safe_value = redact_secrets(value)
        safe_value = cls.ANSI_ESCAPE_RE.sub("", safe_value)
        safe_value = safe_value.replace("\r\n", "\n").replace("\r", "\n")
        safe_value = re.sub(r"[\x85\u2028\u2029]", "\n", safe_value)
        safe_value = cls.CONTROL_CHAR_RE.sub("", safe_value)
        safe_value = cls.BIDI_CONTROL_RE.sub("", safe_value)
        safe_value = redact_secrets(safe_value)
        if len(safe_value) <= cls.MAX_TERMINAL_CHUNK_CHARS:
            return safe_value
        marker = "\n[earlier output omitted]\n"
        head = cls.MAX_TERMINAL_CHUNK_CHARS * 2 // 3
        tail = cls.MAX_TERMINAL_CHUNK_CHARS - head - len(marker)
        return safe_value[:head] + marker + safe_value[-tail:]

    def _set_details_text(self, value: str) -> None:
        self._replace_text(self.details_text, value)

    def _set_changes_text(self, value: str) -> None:
        self._replace_text(self.changes_text, value)

    def _set_memory_text(self, value: str) -> None:
        self._replace_text(self.memory_text, value)

    @staticmethod
    def _replace_text(widget: QPlainTextEdit, value: str) -> None:
        widget.setPlainText(value)
        widget.verticalScrollBar().setValue(0)

    @staticmethod
    def _one_line(value: str, limit: int = 180) -> str:
        normalized = " ".join(value.split())
        return normalized if len(normalized) <= limit else normalized[: limit - 3] + "..."

    def _argument_summary(self, arguments: object) -> str:
        if not isinstance(arguments, dict):
            return self._one_line(str(arguments))
        for key in ("path", "command", "query"):
            value = arguments.get(key)
            if value:
                return self._one_line(str(value))
        return self._one_line(json.dumps(arguments, ensure_ascii=False))

    def _settle_active_items(self, state: str, tag: str) -> None:
        for item_id in tuple(self._active_items):
            item = self._timeline_items.get(item_id)
            if item is None:
                continue
            action = item.text(1) or "Operation"
            summary = item.text(2) or "Interrupted"
            details = self._entry_details.get(item_id, summary)
            self._update_timeline(item_id, state, action, summary, details, tag)
        self._active_items.clear()

    def _finalize_task_item(self, state: str, tag: str, result_details: str) -> None:
        if self._task_item and self._task_item in self._timeline_items:
            details = f"Task\n{self._current_task_text}\n\nResult\n{result_details}"
            self._update_timeline(
                self._task_item, state, "Task", self._current_task_text, details, tag
            )

    def _trim_timeline(self) -> None:
        while self.timeline.topLevelItemCount() > self.MAX_TIMELINE_ITEMS:
            removable_id: str | None = None
            removable_index: int | None = None
            for index in range(self.timeline.topLevelItemCount()):
                item = self.timeline.topLevelItem(index)
                candidate = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
                if candidate != self._task_item and candidate not in self._active_items:
                    removable_id = candidate
                    removable_index = index
                    break
            if removable_index is None:
                removable_index = 0
                item = self.timeline.topLevelItem(removable_index)
                removable_id = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
            self.timeline.takeTopLevelItem(removable_index)
            self._timeline_items.pop(removable_id, None)
            self._entry_details.pop(removable_id, None)
            self._active_items.discard(removable_id)
            self._tool_items = {
                key: value for key, value in self._tool_items.items() if value != removable_id
            }
            self._round_items = {
                key: value for key, value in self._round_items.items() if value != removable_id
            }
            if removable_id == self._task_item:
                self._task_item = None

    def _on_run_shortcut(self) -> None:
        self._start_task()

    def _on_stop_shortcut(self) -> None:
        if self.worker.is_running:
            self._stop_task()

    def _on_close(self) -> None:
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self._closed:
            self._stop_file_workers()
            event.accept()
            return
        if self.worker.is_running:
            answer = QMessageBox.question(
                self,
                "TinyForge",
                "A task is still running. Request a stop and close the window?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            if not self.worker.cancel():
                self._closed = True
                self._drain_timer.stop()
                self._stop_file_workers()
                event.accept()
                return
            self._closing = True
            self._set_status("Stopping", "warning")
            self.status_label.setToolTip("Stopping after the current operation")
            self.stop_button.setEnabled(False)
            event.ignore()
            return
        self._closed = True
        self._drain_timer.stop()
        self._stop_file_workers()
        event.accept()

    def _close_after_terminal_event(self) -> None:
        if self._closing:
            QTimer.singleShot(0, self.close)

    def _destroy_window(self) -> None:
        if self._closed:
            return
        self._drain_timer.stop()
        self._file_refresh_timer.stop()
        self._file_filter_timer.stop()
        self._file_generation += 1
        self._file_preview_generation += 1
        self._closed = True
        self._stop_file_workers()
        self.close()
        self.deleteLater()


def _show_window(
    application: QApplication,
    window: TinyForgeApp,
    *,
    secondary_screen: bool,
) -> None:
    """Show an interactive window on a secondary display without initial activation."""
    if not secondary_screen:
        window.show()
        return

    primary = application.primaryScreen()
    if primary is None:
        window.show()
        return
    target = next((screen for screen in application.screens() if screen is not primary), primary)
    if target is primary:
        window.show()
        return

    area = target.availableGeometry()
    width = min(1_020, max(960, area.width() - 24))
    height = min(640, max(600, area.height() - 48))
    window.setMinimumSize(960, 600)
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

    # Select the display before first show so mixed-DPI systems never flash the
    # window on the primary monitor while Qt recreates its native handle.
    window.winId()
    handle = window.windowHandle()
    if handle is not None:
        handle.setScreen(target)
    window.resize(width, height)
    window.move(
        area.x() + max(0, (area.width() - width) // 2),
        area.y() + max(0, (area.height() - height) // 2),
    )
    window.show()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tinyforge-gui", description="TinyForge desktop frontend")
    parser.add_argument("-w", "--workspace", default=".", help="Initial workspace directory")
    parser.add_argument(
        "--secondary-screen",
        action="store_true",
        help="Open on the first non-primary display without initially taking focus",
    )
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"TinyForge {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("TinyForge")
    application.setApplicationVersion(__version__)
    application.setStyle("Fusion")
    application.setFont(QFont("Segoe UI", 10))
    window = TinyForgeApp(args.workspace)
    _show_window(application, window, secondary_screen=args.secondary_screen)
    if args.smoke_test:
        application.processEvents()
        window._destroy_window()
        application.processEvents()
        print("TinyForge PySide6 GUI smoke test passed.")
        return 0
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
