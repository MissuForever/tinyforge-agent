"""Record a reproducible, real TinyForge GUI repair session on Windows."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from ctypes import wintypes

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QAbstractItemView, QApplication


ROOT = Path(__file__).resolve().parents[1]
DEMO_ROOT = (ROOT / ".demo").resolve()
DEFAULT_OUTPUT = DEMO_ROOT / "gui-video-run1"
FFMPEG = (
    ROOT
    / ".demo"
    / "video-tools"
    / "imageio_ffmpeg"
    / "binaries"
    / "ffmpeg-win-x86_64-v7.1.exe"
)
WINDOW_TITLE = "TinyForge 0.3.0 - Live GUI Demo"
WINDOW_WIDTH = 1_020
WINDOW_HEIGHT = 720
TEST_COMMAND = "python -m unittest discover -s tests -t . -v"
TASK = (
    "先使用 list_skills 检索 verified bugfix 并加载匹配的 Skill。"
    "然后阅读 README.md、pricing.py 和完整测试。先使用 run_command 运行 "
    f"{TEST_COMMAND}，记录当前失败；"
    "只修改 pricing.py 修复 order_total，不得修改测试；再次运行同一条完整测试命令验证。"
    "测试通过后，使用 stage_memory 保存带验证证据的可复用 SOP。"
)


def _inside_demo(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == DEMO_ROOT or DEMO_ROOT not in resolved.parents:
        raise ValueError(f"output must be a child of {DEMO_ROOT}")
    return resolved


def _run(
    command: list[str], *, cwd: Path, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def _snapshot(workspace: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if (
            path.is_file()
            and "__pycache__" not in path.parts
            and ".git" not in path.parts
        ):
            snapshot[path.relative_to(workspace).as_posix()] = path.read_text(
                encoding="utf-8", errors="replace"
            )
    return snapshot


def _timeline_rows(app: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for index in range(app.timeline.topLevelItemCount()):
        item = app.timeline.topLevelItem(index)
        item_id = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
        rows.append(
            {
                "state": item.text(0),
                "action": item.text(1),
                "summary": item.text(2),
                "details": app._entry_details.get(item_id, ""),
            }
        )
    return rows


def _test_evidence(rows: list[dict[str, str]]) -> dict[str, list[int]]:
    failed: list[int] = []
    passed: list[int] = []
    for index, row in enumerate(rows):
        if row["action"] != "run_command" or TEST_COMMAND not in row["details"]:
            continue
        details = row["details"]
        if (
            row["state"] == "Failed"
            and "Ran 4 tests" in details
            and "FAILED (failures=3)" in details
        ):
            failed.append(index)
        if (
            row["state"] == "Succeeded"
            and "Ran 4 tests" in details
            and "OK" in details
            and "FAILED" not in details
            and "ERROR" not in details
            and "skipped=" not in details
        ):
            passed.append(index)
    return {"failed": failed, "passed": passed}


def _safe_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{host}{path}"


def _position_recording_window(
    application: QApplication,
    app: Any,
    *,
    primary_fullscreen: bool = False,
) -> Any:
    """Place the recording window in the requested, explicitly verified mode."""
    primary = application.primaryScreen()
    if primary is None:
        raise RuntimeError("Qt did not report a primary screen")
    target = (
        primary
        if primary_fullscreen
        else next(
            (screen for screen in application.screens() if screen is not primary),
            primary,
        )
    )
    area = target.geometry() if primary_fullscreen else target.availableGeometry()
    width = min(WINDOW_WIDTH, max(960, area.width() - 24))
    height = min(WINDOW_HEIGHT, max(600, area.height() - 48))

    app.setMinimumSize(960, 600)
    app.setAttribute(
        Qt.WidgetAttribute.WA_ShowWithoutActivating,
        not primary_fullscreen,
    )
    app.setWindowFlag(
        Qt.WindowType.WindowDoesNotAcceptFocus,
        not primary_fullscreen,
    )
    app.setWindowFlag(
        Qt.WindowType.WindowStaysOnTopHint,
        primary_fullscreen or target is not primary,
    )

    # Bind the native window to the target display before its first show so it
    # never flashes on the primary display during a mixed-DPI screen transfer.
    app.winId()
    handle = app.windowHandle()
    if handle is None:
        raise RuntimeError("Qt did not create a native recording window")
    handle.setScreen(target)
    if primary_fullscreen:
        app.setGeometry(area)
        app.showFullScreen()
    else:
        app.resize(width, height)
        app.move(
            area.x() + max(0, (area.width() - width) // 2),
            area.y() + max(0, (area.height() - height) // 2),
        )
        app.show()
    application.processEvents()
    if primary_fullscreen:
        app.raise_()
        app.activateWindow()
        application.processEvents()
        if not app.isFullScreen():
            raise RuntimeError("The primary recording window did not enter full-screen mode")
        if not app.windowFlags() & Qt.WindowType.WindowStaysOnTopHint:
            raise RuntimeError("The primary recording window is not configured as topmost")
    if app.windowHandle().screen() is not target:
        raise RuntimeError(f"Unable to place the recording window on {target.name()}")
    return target


def _native_window_region(app: Any) -> dict[str, int]:
    """Return the visible Win32 window bounds in physical desktop pixels."""
    if sys.platform != "win32":
        raise RuntimeError("GUI recording currently requires Windows")

    class Rect(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    hwnd = wintypes.HWND(int(app.winId()))
    rect = Rect()
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
    get_frame = dwmapi.DwmGetWindowAttribute
    get_frame.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]
    get_frame.restype = ctypes.c_long
    if get_frame(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        get_rect = user32.GetWindowRect
        get_rect.argtypes = [wintypes.HWND, ctypes.POINTER(Rect)]
        get_rect.restype = wintypes.BOOL
        if not get_rect(hwnd, ctypes.byref(rect)):
            raise ctypes.WinError(ctypes.get_last_error())

    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise RuntimeError(f"Invalid recording window bounds: {width}x{height}")
    return {"x": rect.left, "y": rect.top, "width": width, "height": height}


class DemoController:
    """Drive public GUI widgets and capture only redacted UI-facing state."""

    def __init__(
        self,
        application: QApplication,
        app: Any,
        *,
        output: Path,
        timeout: int,
    ) -> None:
        self.application = application
        self.app = app
        self.output = output
        self.timeout = timeout
        self.ffmpeg: subprocess.Popen[str] | None = None
        self.ffmpeg_log: Any = None
        self.record_started = 0.0
        self.run_started = 0.0
        self.result: Any = None
        self.events: list[dict[str, Any]] = []
        self.markers: list[dict[str, Any]] = []
        self.failure: str | None = None
        self.capture_region: dict[str, int] = {}
        self._showcase_started = False
        self._original_handle = app._handle_envelope
        app._handle_envelope = self._handle_envelope

    @staticmethod
    def _after(delay_ms: int, callback: Callable[..., None], *args: Any) -> None:
        QTimer.singleShot(delay_ms, lambda: callback(*args))

    def elapsed(self) -> float:
        if not self.record_started:
            return 0.0
        return round(time.monotonic() - self.record_started, 3)

    def mark(self, name: str, **data: Any) -> None:
        self.markers.append({"name": name, "t": self.elapsed(), **data})
        print(f"[demo] {name} at {self.elapsed():.1f}s", flush=True)

    def _handle_envelope(self, envelope: Any) -> None:
        payload = envelope.payload
        entry: dict[str, Any] = {"t": self.elapsed(), "kind": envelope.kind}
        if envelope.kind == "event":
            entry["event"] = payload.kind
            entry["data"] = payload.data
        elif envelope.kind == "file_diff":
            entry["data"] = payload
        elif envelope.kind == "result":
            entry["data"] = asdict(payload)
        else:
            entry["data"] = str(payload)
        self.events.append(entry)
        self._original_handle(envelope)
        if envelope.kind == "result":
            self.result = payload
            self.mark("agent_terminal", success=bool(payload.success))
            self._schedule_showcase()
        elif envelope.kind == "error":
            self.failure = str(payload)
            self.mark("agent_error")
            self._after(4_000, self.finish)

    def start_capture(self) -> None:
        raw_video = self.output / "gui-raw.mp4"
        self.capture_region = _native_window_region(self.app)
        reference = self.output / "capture-reference.png"
        if not self.app.grab().save(str(reference)):
            self.failure = f"Unable to save Qt capture reference: {reference}"
            self._after(0, self.application.quit)
            return
        command = [
            str(FFMPEG),
            "-hide_banner",
            "-y",
            "-loglevel",
            "info",
            "-f",
            "gdigrab",
            "-framerate",
            "30",
            "-draw_mouse",
            "0",
            "-rtbufsize",
            "512M",
            "-offset_x",
            str(self.capture_region["x"]),
            "-offset_y",
            str(self.capture_region["y"]),
            "-video_size",
            f"{self.capture_region['width']}x{self.capture_region['height']}",
            "-i",
            "desktop",
            "-vf",
            (
                "scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=white,format=yuv420p"
            ),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "17",
            "-movflags",
            "+faststart",
            str(raw_video),
        ]
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.ffmpeg_log = (self.output / "ffmpeg-record.log").open(
                "w", encoding="utf-8", newline=""
            )
            self.ffmpeg = subprocess.Popen(
                command,
                cwd=ROOT,
                stdin=subprocess.PIPE,
                stdout=self.ffmpeg_log,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=creation_flags,
            )
        except OSError as exc:
            if self.ffmpeg_log is not None:
                self.ffmpeg_log.close()
                self.ffmpeg_log = None
            self.failure = f"Unable to start FFmpeg: {exc}"
            self._after(0, self.application.quit)
            return
        self.record_started = time.monotonic()
        self.mark("recording_started")
        self._after(2_000, self._check_capture)

    def _check_capture(self) -> None:
        if self.ffmpeg is None or self.ffmpeg.poll() is not None:
            code = None if self.ffmpeg is None else self.ffmpeg.returncode
            self.failure = f"FFmpeg window capture failed with exit code {code}"
            self._after(0, self.application.quit)
            return
        self.mark("task_typing_started")
        self._type_task(0)

    def _type_task(self, offset: int) -> None:
        if offset >= len(TASK):
            self.mark("task_ready")
            self._after(1_200, self._run_task)
            return
        end = min(len(TASK), offset + 2)
        self.app.task_input.insertPlainText(TASK[offset:end])
        self.app.task_input.ensureCursorVisible()
        self._after(42, self._type_task, end)

    def _run_task(self) -> None:
        self.mark("task_submitted")
        self.run_started = time.monotonic()
        self.app._start_task()
        status = self.app.status_label.text()
        if status != "Running":
            self.failure = f"GUI did not start the task: {status}"
            self._after(4_000, self.finish)
            return
        self._after(1_000, self._check_timeout)

    def _check_timeout(self) -> None:
        if not self.app.worker.is_running:
            return
        if time.monotonic() - self.run_started > self.timeout:
            self.failure = f"Agent exceeded the {self.timeout}s recording timeout"
            self.mark("timeout")
            self.app._stop_task()
            self._after(1_000, self._wait_for_cancel, time.monotonic() + 180)
            return
        self._after(1_000, self._check_timeout)

    def _wait_for_cancel(self, deadline: float) -> None:
        if not self.app.worker.is_running:
            if not self._showcase_started:
                self._after(3_000, self.finish)
            return
        if time.monotonic() >= deadline:
            self.failure = f"{self.failure}; worker did not stop after cancellation"
            self.finish()
            return
        self._after(1_000, self._wait_for_cancel, deadline)

    def _schedule_showcase(self) -> None:
        if self._showcase_started:
            return
        self._showcase_started = True
        stages: list[tuple[str, Callable[[], None], int]] = [
            ("result_live", self._show_result, 4_000),
            ("baseline_evidence", self._show_failed_test, 5_500),
            ("code_changes", self._show_changes, 6_500),
            ("workspace_files", self._show_files, 6_500),
            ("verification_evidence", self._show_passed_test, 5_500),
            ("persistent_memory", self._show_memory, 6_500),
            ("skill_receipt", self._show_skills, 6_500),
            ("final_result", self._show_result, 5_000),
        ]

        def advance(index: int) -> None:
            if index >= len(stages):
                self.finish()
                return
            name, action, duration = stages[index]
            action()
            if name == "workspace_files":
                self._wait_for_files_showcase(
                    deadline=time.monotonic() + 8.0,
                    on_ready=lambda: self._after(duration, advance, index + 1),
                )
                return
            self.mark(name)
            self._after(duration, advance, index + 1)

        self._after(2_000, advance, 0)

    def _select_timeline(self, *, state: str | None, action: str, last: bool) -> bool:
        items = [
            self.app.timeline.topLevelItem(index)
            for index in range(self.app.timeline.topLevelItemCount())
        ]
        if last:
            items.reverse()
        for item in items:
            if item.text(1) != action or (state is not None and item.text(0) != state):
                continue
            self.app.timeline.setCurrentItem(item)
            self.app.timeline.scrollToItem(
                item, QAbstractItemView.ScrollHint.PositionAtCenter
            )
            self.app._on_timeline_select()
            self.app.inspector.setCurrentIndex(0)
            self.app.details_text.verticalScrollBar().setValue(0)
            return True
        return False

    def _show_failed_test(self) -> None:
        self._select_test_evidence(success=False)
        self._focus_terminal_output("[stderr] FAILED (failures=3)", last=False)

    def _show_passed_test(self) -> None:
        self.app.main_splitter.setSizes([250, 430, 288])
        self._select_test_evidence(success=True)
        self._focus_terminal_output("[stderr] OK", last=True)

    def _focus_terminal_output(self, needle: str, *, last: bool) -> bool:
        text = self.app.terminal_text.toPlainText()
        position = text.rfind(needle) if last else text.find(needle)
        if position < 0:
            return False
        cursor = self.app.terminal_text.textCursor()
        cursor.setPosition(position)
        self.app.terminal_text.setTextCursor(cursor)
        self.app.terminal_text.centerCursor()
        return True

    def _select_test_evidence(self, *, success: bool) -> bool:
        state = "Succeeded" if success else "Failed"
        items = [
            self.app.timeline.topLevelItem(index)
            for index in range(self.app.timeline.topLevelItemCount())
        ]
        if success:
            items.reverse()
        for item in items:
            item_id = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
            details = self.app._entry_details.get(item_id, "")
            expected_output = "OK" if success else "FAILED (failures=3)"
            if (
                item.text(0) == state
                and item.text(1) == "run_command"
                and TEST_COMMAND in details
                and "Ran 4 tests" in details
                and expected_output in details
            ):
                self.app.timeline.setCurrentItem(item)
                self.app.timeline.scrollToItem(
                    item, QAbstractItemView.ScrollHint.PositionAtCenter
                )
                self.app._on_timeline_select()
                self.app.inspector.setCurrentIndex(0)
                self.app.details_text.verticalScrollBar().setValue(0)
                return True
        return False

    def _show_changes(self) -> None:
        self.app.inspector.setCurrentIndex(1)
        self.app.changes_text.verticalScrollBar().setValue(0)

    def _show_files(self) -> None:
        self.app.main_splitter.setSizes([310, 370, 288])
        self.app.files_splitter.setSizes([125, 165])
        self.app.file_search_entry.clear()
        self.app._request_files_refresh("pricing.py", delay_ms=0)

    def _wait_for_files_showcase(
        self,
        *,
        deadline: float,
        on_ready: Callable[[], None],
    ) -> None:
        entry = self.app._file_entries.get("pricing.py")
        item = self.app._file_items.get("pricing.py")
        if item is not None and self.app._selected_file_path != "pricing.py":
            self.app.file_tree.setCurrentItem(item)
        preview_ready = (
            self.app.file_preview_meta.text() != "Loading"
            and "order_total" in self.app.file_preview_text.toPlainText()
        )
        if (
            self.app.file_refresh_button.isEnabled()
            and entry is not None
            and item is not None
            and self.app._selected_file_path == "pricing.py"
            and preview_ready
            and "M" in entry.git_status
        ):
            self.app.file_tree.scrollToItem(
                item,
                QAbstractItemView.ScrollHint.PositionAtCenter,
            )
            self.mark(
                "workspace_files",
                path="pricing.py",
                git_status=entry.git_status,
            )
            on_ready()
            return
        if time.monotonic() >= deadline:
            self.failure = "Files showcase did not become ready before its deadline"
            self.mark("workspace_files_error")
            self._after(1_000, self.finish)
            return
        self._after(
            100,
            lambda: self._wait_for_files_showcase(
                deadline=deadline,
                on_ready=on_ready,
            ),
        )

    def _show_memory(self) -> None:
        self.app.inspector.setCurrentIndex(2)
        scrollbar = self.app.memory_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _show_skills(self) -> None:
        for index in range(self.app.inspector.count()):
            if self.app.inspector.tabText(index) == "Skills":
                self.app.inspector.setCurrentIndex(index)
                break
        for index in range(self.app.skill_tree.topLevelItemCount()):
            item = self.app.skill_tree.topLevelItem(index)
            if item.text(0) != "Loaded":
                continue
            item.setExpanded(True)
            self.app.skill_tree.setCurrentItem(item)
            self.app.skill_tree.scrollToItem(
                item,
                QAbstractItemView.ScrollHint.PositionAtCenter,
            )
            break

    def _show_result(self) -> None:
        self._select_timeline(state=None, action="Result", last=True)

    def finish(self) -> None:
        self.mark("recording_finished")
        self.application.quit()

    def stop_capture(self) -> int | None:
        if self.ffmpeg is None:
            return None
        if self.ffmpeg.poll() is None and self.ffmpeg.stdin is not None:
            try:
                self.ffmpeg.stdin.write("q\n")
                self.ffmpeg.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        try:
            code = self.ffmpeg.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.ffmpeg.terminate()
            code = self.ffmpeg.wait(timeout=10)
        if self.ffmpeg_log is not None:
            self.ffmpeg_log.close()
        return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT.relative_to(ROOT)))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument(
        "--primary-fullscreen",
        action="store_true",
        help="Record a topmost full-screen window on the primary display",
    )
    args = parser.parse_args()

    output = _inside_demo(ROOT / args.output)
    if output.exists():
        if not args.force:
            parser.error(f"output already exists: {output}; pass --force to replace it")
        shutil.rmtree(output)
    output.mkdir(parents=True)
    workspace = output / "workspace"

    prepare = _run(
        [
            sys.executable,
            str(ROOT / "scripts" / "prepare_demo.py"),
            "--target",
            str(workspace.relative_to(ROOT)),
            "--force",
        ],
        cwd=ROOT,
    )
    if prepare.returncode:
        print(prepare.stdout, file=sys.stderr)
        return prepare.returncode

    skill_source = ROOT / ".tinyforge" / "skills" / "verified-bugfix"
    skill_target = workspace / ".tinyforge" / "skills" / "verified-bugfix"
    if not (skill_source / "SKILL.md").is_file():
        print(f"Demo Skill not found: {skill_source}", file=sys.stderr)
        return 2
    skill_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(skill_source, skill_target)

    git_commands = [
        ["git", "init", "--quiet"],
        [
            "git",
            "add",
            "README.md",
            "pricing.py",
            "tests",
            ".tinyforge/skills/verified-bugfix",
        ],
        [
            "git",
            "-c",
            "user.name=TinyForge Demo",
            "-c",
            "user.email=tinyforge-demo@localhost",
            "commit",
            "--quiet",
            "-m",
            "demo baseline",
        ],
    ]
    for command in git_commands:
        initialized = _run(command, cwd=workspace)
        if initialized.returncode:
            print(initialized.stdout, file=sys.stderr)
            print("Unable to create the isolated demo Git baseline.", file=sys.stderr)
            return initialized.returncode

    before = _snapshot(workspace)
    baseline = _run(
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-t",
            ".",
            "-v",
        ],
        cwd=workspace,
    )
    (output / "baseline.txt").write_text(baseline.stdout, encoding="utf-8")
    baseline_expected = (
        baseline.returncode != 0
        and "Ran 4 tests" in baseline.stdout
        and "FAILED (failures=3)" in baseline.stdout
        and "ERROR" not in baseline.stdout
    )
    if not baseline_expected:
        print("Baseline was not the expected four-test failure; refusing to record.", file=sys.stderr)
        return 2

    if not FFMPEG.is_file():
        print(f"FFmpeg not found: {FFMPEG}", file=sys.stderr)
        return 2

    os.environ.update(
        {
            "TINYFORGE_STATE_DIR": str(output / "state"),
            "TINYFORGE_ARCHIVE_SESSIONS": "0",
            "TINYFORGE_SKILLS_ENABLED": "1",
            "TINYFORGE_MAX_ROUNDS": "20",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )

    from tinyforge import __version__
    from tinyforge.config import Config
    from tinyforge.gui import TinyForgeApp

    config = Config.from_env(workspace)
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("TinyForge")
    application.setApplicationVersion(__version__)
    application.setStyle("Fusion")
    application.setFont(QFont("Segoe UI", 10))
    app = TinyForgeApp(workspace)
    app.setWindowTitle(WINDOW_TITLE)
    target_screen = _position_recording_window(
        application,
        app,
        primary_fullscreen=args.primary_fullscreen,
    )

    controller = DemoController(application, app, output=output, timeout=args.timeout)
    QTimer.singleShot(900, controller.start_capture)
    try:
        application.exec()
    finally:
        ffmpeg_code = controller.stop_capture()

    if app.worker.is_running:
        wait_deadline = time.monotonic() + 180
        while app.worker.is_running and time.monotonic() < wait_deadline:
            application.processEvents()
            time.sleep(0.05)
        if app.worker.is_running:
            controller.failure = (
                f"{controller.failure + '; ' if controller.failure else ''}"
                "worker remained active after the GUI closed"
            )

    timeline = _timeline_rows(app)
    changes_text = app.changes_text.toPlainText().strip()
    memory_text = app.memory_text.toPlainText().strip()
    status = app.status_label.text()
    stats = app.stats_label.text()
    change_count = app._change_count
    memory_commit_count = app._memory_commit_count
    terminal_text = app.terminal_text.toPlainText()
    command_showcase = {
        "visible": app.terminal_panel.isVisibleTo(app),
        "height": app.terminal_panel.height(),
        "count": app._terminal_command_count,
        "has_failed_baseline": "[stderr] FAILED (failures=3)" in terminal_text,
        "has_successful_verification": (
            "[stderr] OK" in terminal_text and "[exit 0]" in terminal_text
        ),
    }
    pricing_entry = app._file_entries.get("pricing.py")
    files_showcase = {
        "indexed": pricing_entry is not None,
        "selected": app._selected_file_path == "pricing.py",
        "preview_ready": "order_total" in app.file_preview_text.toPlainText(),
        "git_status": pricing_entry.git_status if pricing_entry is not None else "",
    }
    loaded_skills = [dict(item) for item in app._loaded_skills]
    skill_showcase = {
        "enabled": app._skill_enabled,
        "search_count": len(app._skill_searches),
        "loaded": [str(item.get("id", "")) for item in loaded_skills],
        "receipts_complete": bool(loaded_skills)
        and all(
            len(str(item.get("sha256", ""))) == 64
            and len(str(item.get("resource_manifest_sha256", ""))) == 64
            for item in loaded_skills
        ),
    }
    app._destroy_window()
    application.processEvents()

    verification = _run(
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-t",
            ".",
            "-v",
        ],
        cwd=workspace,
    )
    (output / "verification.txt").write_text(verification.stdout, encoding="utf-8")
    after = _snapshot(workspace)
    changed_files = sorted(
        key for key in set(before) | set(after) if before.get(key) != after.get(key)
    )
    memory_files = sorted(
        str(path.relative_to(output)).replace("\\", "/")
        for path in (output / "state").rglob("*.json")
    ) if (output / "state").exists() else []
    gui_test_evidence = _test_evidence(timeline)
    gui_tests_verified = (
        bool(gui_test_evidence["failed"])
        and bool(gui_test_evidence["passed"])
        and gui_test_evidence["failed"][0] < gui_test_evidence["passed"][-1]
    )
    verification_expected = (
        verification.returncode == 0
        and "Ran 4 tests" in verification.stdout
        and "OK" in verification.stdout
        and "FAILED" not in verification.stdout
        and "ERROR" not in verification.stdout
        and "skipped=" not in verification.stdout
    )
    files_marker_recorded = any(
        marker.get("name") == "workspace_files" for marker in controller.markers
    )
    skill_marker_recorded = any(
        marker.get("name") == "skill_receipt" for marker in controller.markers
    )

    result_data = {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "window_title": WINDOW_TITLE,
        "task": TASK,
        "configuration": {
            "base_url": _safe_endpoint(config.base_url),
            "model": config.model,
            "wire_api": config.wire_api,
            "reasoning_effort": config.reasoning_effort,
            "store_responses": config.store_responses,
            "memory_enabled": config.memory_enabled,
            "skills_enabled": config.skills_enabled,
        },
        "status": status,
        "stats": stats,
        "agent_result": asdict(controller.result) if controller.result is not None else None,
        "failure": controller.failure,
        "baseline": {
            "exit_code": baseline.returncode,
            "ran_four_tests": "Ran 4 tests" in baseline.stdout,
            "expected_three_failures": baseline_expected,
        },
        "verification": {
            "exit_code": verification.returncode,
            "ran_four_tests": "Ran 4 tests" in verification.stdout,
            "all_passed": verification_expected,
        },
        "gui_test_evidence": gui_test_evidence,
        "command_showcase": command_showcase,
        "files_showcase": files_showcase,
        "files_marker_recorded": files_marker_recorded,
        "skill_showcase": skill_showcase,
        "skill_marker_recorded": skill_marker_recorded,
        "changed_files": changed_files,
        "change_count": change_count,
        "memory_commit_count": memory_commit_count,
        "memory_files": memory_files,
        "ffmpeg_exit_code": ffmpeg_code,
        "capture_region": controller.capture_region,
        "capture_screen": target_screen.name(),
        "capture_mode": (
            "primary_fullscreen_topmost"
            if args.primary_fullscreen
            else "secondary_compact"
        ),
        "recording_duration": controller.elapsed(),
        "markers": controller.markers,
        "events": controller.events,
        "timeline": timeline,
        "changes": changes_text,
        "memory": memory_text,
    }
    (output / "result.json").write_text(
        json.dumps(result_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    accepted = (
        status == "Completed"
        and controller.result is not None
        and controller.result.success
        and controller.failure is None
        and baseline_expected
        and verification_expected
        and gui_tests_verified
        and command_showcase["visible"]
        and command_showcase["height"] >= 120
        and command_showcase["count"] >= 2
        and command_showcase["has_failed_baseline"]
        and command_showcase["has_successful_verification"]
        and files_showcase["indexed"]
        and files_showcase["selected"]
        and files_showcase["preview_ready"]
        and "M" in files_showcase["git_status"]
        and files_marker_recorded
        and skill_showcase["enabled"]
        and skill_showcase["search_count"] >= 1
        and "workspace:verified-bugfix" in skill_showcase["loaded"]
        and skill_showcase["receipts_complete"]
        and skill_marker_recorded
        and changed_files == ["pricing.py"]
        and change_count >= 1
        and memory_commit_count >= 1
        and ffmpeg_code == 0
        and (output / "gui-raw.mp4").is_file()
    )
    print(
        json.dumps(
            {
                "accepted": accepted,
                "status": status,
                "changed_files": changed_files,
                "command_showcase": command_showcase,
                "files_showcase": files_showcase,
                "skill_showcase": skill_showcase,
                "memory_commits": memory_commit_count,
                "raw_video": str(output / "gui-raw.mp4"),
                "result": str(output / "result.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
