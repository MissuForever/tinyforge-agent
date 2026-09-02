from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tinyforge.memory import WorkingMemory
from tinyforge.tools import CompositeTools, WorkspaceTools


class WorkspaceToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.tools = WorkspaceTools(self.root, command_timeout=5, max_output=10_000)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def execute(self, name: str, **arguments: object) -> dict[str, object]:
        return json.loads(self.tools.execute(name, json.dumps(arguments)))

    @staticmethod
    def _python_command(script: str) -> str:
        executable = (
            f'& "{sys.executable}"' if os.name == "nt" else shlex.quote(sys.executable)
        )
        return f"{executable} {shlex.quote(script)}"

    def test_write_read_edit_and_search(self) -> None:
        written = self.execute("write_file", path="src/main.py", content="x = 1\nprint(x)\n")
        self.assertTrue(written["ok"])

        read = self.execute("read_file", path="src/main.py")
        self.assertIn("1 | x = 1", read["result"]["content"])

        edited = self.execute(
            "edit_file", path="src/main.py", old_text="x = 1", new_text="x = 42"
        )
        self.assertTrue(edited["ok"])
        self.assertEqual((self.root / "src/main.py").read_text(encoding="utf-8"), "x = 42\nprint(x)\n")

        searched = self.execute("search_files", query="42", file_glob="*.py")
        self.assertEqual(searched["result"]["matches"][0]["path"], "src/main.py")

    def test_edit_requires_unique_match(self) -> None:
        (self.root / "values.txt").write_text("same\nsame\n", encoding="utf-8")
        result = self.execute(
            "edit_file", path="values.txt", old_text="same", new_text="changed"
        )
        self.assertFalse(result["ok"])
        self.assertIn("occurs 2 times", result["error"])

    def test_path_escape_is_rejected(self) -> None:
        result = self.execute("read_file", path="../outside.txt")
        self.assertFalse(result["ok"])
        self.assertIn("escapes the workspace", result["error"])

    def test_command_runs_in_workspace(self) -> None:
        executable = (
            f'& "{sys.executable}"' if os.name == "nt" else shlex.quote(sys.executable)
        )
        result = self.execute("run_command", command=f'{executable} -c "print(6 * 7)"')
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["exit_code"], 0)
        self.assertIn("42", result["result"]["stdout"])

    def test_command_does_not_inherit_agent_provider_credentials(self) -> None:
        (self.root / "inspect_environment.py").write_text(
            "import os\n"
            "print(os.environ.get('OPENAI_API_KEY', 'missing'))\n"
            "print(os.environ.get('TINYFORGE_API_KEY', 'missing'))\n"
            "print(os.environ.get('TINYFORGE_TEST_PASSTHROUGH', 'missing'))\n",
            encoding="utf-8",
        )
        environment = {
            "OPENAI_API_KEY": "openai-agent-secret",
            "TINYFORGE_API_KEY": "tinyforge-agent-secret",
            "TINYFORGE_TEST_PASSTHROUGH": "visible",
        }

        with patch.dict(os.environ, environment, clear=False):
            result = self.execute(
                "run_command",
                command=self._python_command("inspect_environment.py"),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["stdout"].splitlines(), ["missing", "missing", "visible"])

    @unittest.skipUnless(os.name == "nt", "Windows Job Objects are Windows-only")
    def test_windows_command_joins_job_before_suspended_process_resumes(self) -> None:
        jobs: list[bool] = []
        resumes: list[bool] = []
        create_job = WorkspaceTools._create_windows_job
        resume_process = WorkspaceTools._resume_windows_process

        def tracked_job(process):
            job = create_job(process)
            jobs.append(job is not None)
            return job

        def tracked_resume(process):
            resumed = resume_process(process)
            resumes.append(resumed)
            return resumed

        with patch.object(
            WorkspaceTools, "_create_windows_job", side_effect=tracked_job
        ), patch.object(
            WorkspaceTools, "_resume_windows_process", side_effect=tracked_resume
        ):
            result = self.execute("run_command", command="Write-Output ready")

        self.assertTrue(result["ok"])
        self.assertEqual(jobs, [True])
        self.assertEqual(resumes, [True])

    @unittest.skipUnless(os.name == "nt", "Windows Job Objects are Windows-only")
    def test_windows_command_fails_closed_when_job_assignment_is_unavailable(self) -> None:
        with patch.object(WorkspaceTools, "_create_windows_job", return_value=None), patch.object(
            WorkspaceTools, "_resume_windows_process"
        ) as resume:
            result = self.execute("run_command", command="Write-Output must-not-run")

        self.assertFalse(result["ok"])
        self.assertIn("Windows Job Object", result["error"])
        resume.assert_not_called()

    def test_command_streams_stdout_stderr_and_keeps_nonzero_result(self) -> None:
        (self.root / "stream_command.py").write_text(
            "import sys\n"
            "print('stdout-one', flush=True)\n"
            "print('stderr-one', file=sys.stderr, flush=True)\n"
            "print('stdout-two', flush=True)\n"
            "print('stderr-two', file=sys.stderr, flush=True)\n"
            "raise SystemExit(7)\n",
            encoding="utf-8",
        )
        progress: list[tuple[str, str]] = []

        raw = self.tools.execute_with_progress(
            "run_command",
            json.dumps({"command": self._python_command("stream_command.py")}),
            lambda stream, text: progress.append((stream, text)),
        )
        payload = json.loads(raw)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["exit_code"], 1 if os.name == "nt" else 7)
        self.assertEqual(
            "".join(text for stream, text in progress if stream == "stdout"),
            payload["result"]["stdout"],
        )
        self.assertEqual(
            "".join(text for stream, text in progress if stream == "stderr"),
            payload["result"]["stderr"],
        )
        self.assertIn("stdout-one", payload["result"]["stdout"])
        self.assertIn("stderr-two", payload["result"]["stderr"])

    def test_command_streams_flushed_text_before_a_newline_or_process_exit(self) -> None:
        gate = self.root / "continue.flag"
        (self.root / "no_newline_command.py").write_text(
            "import sys\n"
            "import time\n"
            "from pathlib import Path\n"
            "sys.stdout.write('ready')\n"
            "sys.stdout.flush()\n"
            "while not Path('continue.flag').exists():\n"
            "    time.sleep(0.01)\n"
            "sys.stdout.write('done')\n"
            "sys.stdout.flush()\n",
            encoding="utf-8",
        )
        first_output = threading.Event()
        progress: list[tuple[str, str]] = []
        result: list[str] = []
        errors: list[BaseException] = []

        def execute() -> None:
            try:
                result.append(
                    self.tools.execute_with_progress(
                        "run_command",
                        json.dumps(
                            {
                                "command": self._python_command("no_newline_command.py"),
                                "timeout": 10,
                            }
                        ),
                        lambda stream, text: (
                            progress.append((stream, text)),
                            first_output.set(),
                        ),
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=execute, daemon=True)
        worker.start()
        arrived_before_exit = first_output.wait(5)
        was_running = worker.is_alive()
        gate.write_text("continue", encoding="utf-8")
        worker.join(5)

        self.assertTrue(arrived_before_exit)
        self.assertTrue(was_running)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(result), 1)
        payload = json.loads(result[0])
        self.assertEqual(payload["result"]["stdout"], "readydone")
        self.assertEqual("".join(text for _, text in progress), "readydone")

    def test_progress_callback_failure_does_not_break_command(self) -> None:
        (self.root / "callback_command.py").write_text(
            "print('first', flush=True)\nprint('second', flush=True)\n",
            encoding="utf-8",
        )
        callbacks: list[tuple[str, str]] = []

        def failing_callback(stream: str, text: str) -> None:
            callbacks.append((stream, text))
            raise RuntimeError("the UI stopped accepting progress")

        raw = self.tools.execute_with_progress(
            "run_command",
            json.dumps({"command": self._python_command("callback_command.py")}),
            failing_callback,
        )
        payload = json.loads(raw)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["exit_code"], 0)
        self.assertEqual(payload["result"]["stdout"], "first\nsecond\n")
        self.assertTrue(callbacks)
        self.assertEqual("".join(text for _, text in callbacks), "first\nsecond\n")

    def test_command_timeout_streams_partial_output_and_closes_readers(self) -> None:
        (self.root / "timeout_command.py").write_text(
            "import sys\n"
            "import time\n"
            "print('partial-stdout', flush=True)\n"
            "print('partial-stderr', file=sys.stderr, flush=True)\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        progress: list[tuple[str, str]] = []
        readers_before = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("tinyforge-command-")
        }

        raw = self.tools.execute_with_progress(
            "run_command",
            json.dumps(
                {"command": self._python_command("timeout_command.py"), "timeout": 1}
            ),
            lambda stream, text: progress.append((stream, text)),
        )
        payload = json.loads(raw)

        self.assertFalse(payload["ok"])
        self.assertIn("timed out after 1s", payload["error"])
        self.assertIn("partial-stdout", payload["error"])
        self.assertIn("partial-stderr", payload["error"])
        self.assertEqual(
            "".join(text for stream, text in progress if stream == "stdout"),
            "partial-stdout\n",
        )
        self.assertEqual(
            "".join(text for stream, text in progress if stream == "stderr"),
            "partial-stderr\n",
        )
        live_readers = [
            thread.name
            for thread in threading.enumerate()
            if (
                thread.name.startswith("tinyforge-command-")
                and thread.ident not in readers_before
                and thread.is_alive()
            )
        ]
        self.assertEqual(live_readers, [])

    def test_command_cancellation_terminates_descendants_and_closes_readers(self) -> None:
        descendant_finished = self.root / "descendant-finished.flag"
        (self.root / "descendant.py").write_text(
            "import time\n"
            "from pathlib import Path\n"
            "time.sleep(0.75)\n"
            "Path('descendant-finished.flag').write_text('alive', encoding='utf-8')\n",
            encoding="utf-8",
        )
        (self.root / "cancel_tree.py").write_text(
            "import subprocess\n"
            "import sys\n"
            "import time\n"
            "subprocess.Popen([sys.executable, 'descendant.py'])\n"
            "print('tree-ready', flush=True)\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        cancel_event = threading.Event()
        started = threading.Event()
        results: list[str] = []
        errors: list[BaseException] = []
        readers_before = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("tinyforge-command-")
        }

        def execute() -> None:
            try:
                results.append(
                    self.tools.execute_with_progress(
                        "run_command",
                        json.dumps(
                            {
                                "command": self._python_command("cancel_tree.py"),
                                "timeout": 20,
                            }
                        ),
                        lambda stream, text: started.set()
                        if "tree-ready" in text
                        else None,
                        cancel_event=cancel_event,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=execute, daemon=True)
        worker.start()
        try:
            self.assertTrue(started.wait(5))
        finally:
            cancel_event.set()
        cancelled_at = time.monotonic()
        worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - cancelled_at, 5)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        payload = json.loads(results[0])
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["cancelled"])
        self.assertIn("cancelled by user", payload["error"])
        time.sleep(1)
        self.assertFalse(descendant_finished.exists())
        live_readers = [
            thread.name
            for thread in threading.enumerate()
            if (
                thread.name.startswith("tinyforge-command-")
                and thread.ident not in readers_before
                and thread.is_alive()
            )
        ]
        self.assertEqual(live_readers, [])

    def test_command_cleans_descendant_after_parent_exits(self) -> None:
        descendant_finished = self.root / "background-finished.flag"
        (self.root / "background_descendant.py").write_text(
            "import time\n"
            "from pathlib import Path\n"
            "time.sleep(0.75)\n"
            "Path('background-finished.flag').write_text('alive', encoding='utf-8')\n",
            encoding="utf-8",
        )
        (self.root / "exiting_parent.py").write_text(
            "import subprocess\n"
            "import sys\n"
            "subprocess.Popen([sys.executable, 'background_descendant.py'])\n"
            "print('parent-finished', flush=True)\n",
            encoding="utf-8",
        )

        raw = self.tools.execute_with_progress(
            "run_command",
            json.dumps(
                {
                    "command": self._python_command("exiting_parent.py"),
                    "timeout": 10,
                }
            ),
            lambda stream, text: None,
        )
        payload = json.loads(raw)

        self.assertTrue(payload["ok"])
        self.assertIn("parent-finished", payload["result"]["stdout"])
        time.sleep(1)
        self.assertFalse(descendant_finished.exists())

    @unittest.skipIf(os.name == "nt", "setsid is POSIX-only")
    def test_command_closes_readers_when_descendant_escapes_process_group(self) -> None:
        detached_pid = self.root / "detached.pid"
        (self.root / "detached_descendant.py").write_text(
            "import os\n"
            "import time\n"
            "from pathlib import Path\n"
            "Path('detached.pid').write_text(str(os.getpid()), encoding='utf-8')\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        (self.root / "spawn_detached.py").write_text(
            "import subprocess\n"
            "import sys\n"
            "subprocess.Popen(\n"
            "    [sys.executable, 'detached_descendant.py'],\n"
            "    start_new_session=True,\n"
            ")\n"
            "print('parent-finished', flush=True)\n",
            encoding="utf-8",
        )
        readers_before = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("tinyforge-command-")
        }

        try:
            started_at = time.monotonic()
            raw = self.tools.execute_with_progress(
                "run_command",
                json.dumps(
                    {"command": self._python_command("spawn_detached.py"), "timeout": 10}
                ),
                lambda stream, text: None,
            )
            elapsed = time.monotonic() - started_at
            payload = json.loads(raw)
        finally:
            if detached_pid.exists():
                try:
                    os.kill(int(detached_pid.read_text(encoding="utf-8")), 9)
                except (OSError, ValueError):
                    pass

        self.assertTrue(payload["ok"])
        self.assertIn("parent-finished", payload["result"]["stdout"])
        self.assertLess(elapsed, 5)
        live_readers = [
            thread.name
            for thread in threading.enumerate()
            if (
                thread.name.startswith("tinyforge-command-")
                and thread.ident not in readers_before
                and thread.is_alive()
            )
        ]
        self.assertEqual(live_readers, [])

    def test_composite_tools_forward_progress_and_fall_back_for_legacy_provider(self) -> None:
        def tool_definitions(name: str) -> list[dict[str, object]]:
            return [{"type": "function", "function": {"name": name, "parameters": {}}}]

        class StreamingProvider:
            definitions = tool_definitions("stream")

            def execute(self, name: str, arguments: str) -> str:
                raise AssertionError("progress execution should be preferred")

            def execute_with_progress(
                self, name, arguments, on_progress, *, cancel_event=None
            ):
                received_cancel_events.append(cancel_event)
                on_progress("stdout", "live")
                return json.dumps({"ok": True, "result": name})

        class LegacyProvider:
            definitions = tool_definitions("legacy")

            def execute(self, name: str, arguments: str) -> str:
                return json.dumps({"ok": True, "result": arguments})

        composite = CompositeTools(StreamingProvider(), LegacyProvider())
        progress: list[tuple[str, str]] = []
        received_cancel_events: list[threading.Event | None] = []
        cancel_event = threading.Event()
        streamed = composite.execute_with_progress(
            "stream",
            "{}",
            lambda stream, text: progress.append((stream, text)),
            cancel_event=cancel_event,
        )
        legacy = composite.execute_with_progress("legacy", '{"value": 1}', progress.append)
        unknown = composite.execute_with_progress("missing", "{}", progress.append)

        self.assertEqual(progress, [("stdout", "live")])
        self.assertEqual(received_cancel_events, [cancel_event])
        self.assertEqual(json.loads(streamed)["result"], "stream")
        self.assertEqual(json.loads(legacy)["result"], '{"value": 1}')
        self.assertFalse(json.loads(unknown)["ok"])

    def test_dangerous_command_is_blocked(self) -> None:
        result = self.execute("run_command", command="git reset --hard HEAD")
        self.assertFalse(result["ok"])
        self.assertIn("blocked by safety policy", result["error"])

    def test_privilege_escalation_tokens_are_blocked_as_commands(self) -> None:
        commands = (
            "sudo apt-get update",
            "/usr/bin/sudo -u root whoami",
            "su - root",
            "su --login root",
            "Write-Output ready; sudo whoami",
            'powershell -Command "sudo whoami"',
        )

        for command in commands:
            with self.subTest(command=command):
                result = self.execute("run_command", command=command)
                self.assertFalse(result["ok"])
                self.assertIn("privilege escalation", result["error"])

    def test_system_critical_mutations_are_blocked_without_execution(self) -> None:
        commands = (
            "rm -f /etc/passwd",
            "unlink /usr/bin/python",
            r"Remove-Item -LiteralPath C:\Windows\System32\kernel32.dll",
            r"Set-Content -Path $env:SystemRoot\System32\drivers\etc\hosts -Value blocked",
            "Write-Output blocked > /etc/tinyforge-policy-test",
        )

        for command in commands:
            with self.subTest(command=command):
                result = self.execute("run_command", command=command)
                self.assertFalse(result["ok"])
                self.assertIn("system-critical path modification", result["error"])

    def test_dangerous_words_in_output_text_do_not_trigger_policy(self) -> None:
        commands = (
            'echo "sudo rm -rf /"',
            'Write-Output "su --login root"',
            r"Write-Output 'Remove-Item C:\Windows -Recurse'",
            'git commit -m "git reset --hard is not being executed"',
            'python -c "print(\'sudo rm -f /etc/passwd\')"',
        )

        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(WorkspaceTools._danger_reason(command))

    def test_allow_dangerous_bypasses_command_policy_check(self) -> None:
        script = self.root / "allowed.py"
        script.write_text("print('allowed')\n", encoding="utf-8")
        permissive = WorkspaceTools(
            self.root,
            command_timeout=5,
            max_output=10_000,
            allow_dangerous=True,
        )

        with patch.object(
            WorkspaceTools,
            "_danger_reason",
            side_effect=AssertionError("explicit opt-in must bypass the default policy"),
        ) as policy:
            payload = json.loads(
                permissive.execute(
                    "run_command",
                    json.dumps({"command": self._python_command(script.name)}),
                )
            )

        policy.assert_not_called()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["exit_code"], 0)
        self.assertIn("allowed", payload["result"]["stdout"])

    def test_invalid_json_does_not_raise(self) -> None:
        result = json.loads(self.tools.execute("read_file", "{not-json"))
        self.assertFalse(result["ok"])
        self.assertIn("not valid JSON", result["error"])

    def test_large_result_remains_valid_json(self) -> None:
        tools = WorkspaceTools(self.root, max_output=1_000)
        (self.root / "large.txt").write_text("abcdefghij\n" * 500, encoding="utf-8")
        raw = tools.execute("read_file", json.dumps({"path": "large.txt"}))
        payload = json.loads(raw)
        self.assertLessEqual(len(raw), 1_000)
        self.assertTrue(payload["result"]["truncated"])
        self.assertEqual(payload["result"]["path"], "large.txt")
        self.assertEqual(payload["result"]["total_lines"], 500)

    def test_large_failed_command_keeps_exit_code_and_is_not_evidence(self) -> None:
        tools = WorkspaceTools(self.root, max_output=1_000)
        executable = (
            f'& "{sys.executable}"' if os.name == "nt" else shlex.quote(sys.executable)
        )
        (self.root / "large_failure.py").write_text(
            "import sys\nprint('x' * 5000)\nsys.exit(7)\n", encoding="utf-8"
        )
        command = f"{executable} large_failure.py"

        raw = tools.execute("run_command", json.dumps({"command": command}))
        payload = json.loads(raw)

        self.assertLessEqual(len(raw), 1_000)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["result"]["truncated"])
        self.assertEqual(payload["result"]["command"], command)
        self.assertEqual(payload["result"]["cwd"], ".")
        self.assertNotEqual(payload["result"]["exit_code"], 0)

        memory = WorkingMemory()
        memory.start("Run a failing command with large output")
        memory.record_tool("run_command", raw)
        self.assertEqual(memory.evidence, {})

    def test_truncated_command_cannot_become_verification_evidence(self) -> None:
        tools = WorkspaceTools(self.root, max_output=1_000)
        command = "pytest " + "case " * 100 + "--fix"
        raw = tools._serialize_payload(
            {
                "ok": True,
                "result": {
                    "command": command,
                    "cwd": ".",
                    "exit_code": 0,
                    "stdout": "x" * 5_000,
                    "stderr": "",
                },
                "elapsed_ms": 1,
            }
        )
        payload = json.loads(raw)
        self.assertTrue(payload["result"]["command_truncated"])

        memory = WorkingMemory()
        memory.start("Do not trust truncated command metadata")
        memory.record_tool("run_command", raw)
        self.assertFalse(memory.evidence["e1"].verifies_code)


if __name__ == "__main__":
    unittest.main()
