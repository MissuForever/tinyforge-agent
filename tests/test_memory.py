from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tinyforge.memory import (
    MemoryCandidate,
    MemoryRuntime,
    MemoryStore,
    WorkingMemory,
    _is_verification_command,
    redact_secrets,
)


def tool_output(result: dict[str, object], *, ok: bool = True) -> str:
    return json.dumps({"ok": ok, "result": result})


class WorkingMemoryTests(unittest.TestCase):
    def test_redaction_handles_assignment_json_and_bearer_forms(self) -> None:
        secret = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
        samples = (
            f"OPENAI_API_KEY={secret}",
            json.dumps({"password": "plain-password-value"}),
            "Authorization: Bearer abcdefghijklmnop",
            "DATABASE_URL=postgres://user:database-password@host/db",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                redacted = redact_secrets(sample)
                self.assertNotIn(secret, redacted)
                self.assertNotIn("plain-password-value", redacted)
                self.assertNotIn("abcdefghijklmnop", redacted)
                self.assertNotIn("database-password", redacted)
                self.assertIn("[REDACTED", redacted)
                self.assertNotIn("]]", redacted)

    def test_checkpoint_anchor_is_bounded_and_keeps_recent_events(self) -> None:
        memory = WorkingMemory()
        memory.start("Fix the project without changing tests")
        memory.update(
            progress="Located the defect",
            constraints=["Do not change tests"],
            key_facts=["calculator.py subtracts instead of adding"],
            next_step="Patch calculator.py",
        )
        for number in range(30):
            memory.record_tool(
                "read_file",
                tool_output({"path": f"file-{number}.py", "total_lines": number + 1}),
            )
        anchor = memory.render(31, "persistent_memory_index: empty")
        self.assertIn("Do not change tests", anchor)
        self.assertIn("calculator.py subtracts", anchor)
        self.assertNotIn("file-0.py", anchor)
        self.assertIn("file-29.py", anchor)
        self.assertEqual(len(memory.turn_summaries), 20)

    def test_failure_guidance_escalates_to_strategy_change(self) -> None:
        memory = WorkingMemory()
        memory.start("Repair a failing build")
        failed = json.dumps({"ok": False, "error": "file not found"})
        memory.record_tool("read_file", failed)
        self.assertIn("localized correction", memory.recovery_guidance)
        memory.record_tool("read_file", failed)
        self.assertIn("different strategy", memory.recovery_guidance)

    def test_verification_command_classification_is_structural(self) -> None:
        self.assertTrue(_is_verification_command("pytest tests/test_format.py"))
        self.assertTrue(_is_verification_command("py -3 -m unittest discover -v"))
        self.assertTrue(_is_verification_command('python -c "assert 2 > 1"'))
        self.assertTrue(_is_verification_command('python -c "assert 1 < 2"'))
        self.assertFalse(_is_verification_command("echo pytest"))
        self.assertFalse(_is_verification_command("pytest tests --fix"))
        self.assertFalse(_is_verification_command("pytest --version"))
        self.assertFalse(_is_verification_command("python -m pytest --help"))
        self.assertFalse(_is_verification_command("pytest\npython mutate.py"))
        self.assertFalse(_is_verification_command("pytest $(python mutate.py)"))
        self.assertFalse(_is_verification_command("python mutate.py | pytest"))
        self.assertFalse(_is_verification_command("python mutate.py || pytest"))
        self.assertFalse(_is_verification_command("cargo test --no-run"))
        self.assertFalse(_is_verification_command("pytest --co"))
        self.assertFalse(_is_verification_command("tsc -v"))
        self.assertFalse(_is_verification_command("eslint --print-config app.js"))
        self.assertFalse(_is_verification_command("pytest --markers"))
        self.assertFalse(_is_verification_command("pytest --setup-plan"))
        self.assertFalse(_is_verification_command("rspec --dry-run"))
        self.assertFalse(_is_verification_command("ctest -N"))
        self.assertFalse(_is_verification_command("go test -list ."))

    def test_working_anchor_redacts_command_secrets(self) -> None:
        secret = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
        memory = WorkingMemory()
        memory.start(f"Inspect password=plain-secret and {secret}")
        memory.record_tool(
            "run_command",
            tool_output(
                {
                    "command": f"tool --api_key={secret}",
                    "exit_code": 0,
                    "stdout": "",
                }
            ),
        )
        anchor = memory.render(2, "persistent_memory_index: empty")
        self.assertNotIn(secret, anchor)
        self.assertNotIn("plain-secret", anchor)
        self.assertIn("[REDACTED", anchor)


class MemoryRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.state = root / "state"
        self.store = MemoryStore(self.state, self.workspace)
        self.runtime = MemoryRuntime(self.store)
        self.runtime.start_task("Fix calculator")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def execute(self, name: str, **arguments: object) -> dict[str, object]:
        return json.loads(self.runtime.execute(name, json.dumps(arguments)))

    def test_no_execution_no_memory(self) -> None:
        result = self.execute(
            "stage_memory",
            kind="fact",
            title="Project language",
            content="The project uses Python for all runtime modules.",
            evidence_ids=[],
        )
        self.assertFalse(result["ok"])
        self.assertIn("No Execution, No Memory", result["error"])

    def test_sop_requires_successful_verification_after_edit(self) -> None:
        self.runtime.record_tool("edit_file", tool_output({"path": "calculator.py", "replacements": 1}))
        rejected = self.execute(
            "stage_memory",
            kind="sop",
            title="Calculator regression workflow",
            content="Patch calculator.py and run the complete unittest suite before reporting success.",
            keywords=["calculator", "tests"],
            evidence_ids=["e1"],
        )
        self.assertFalse(rejected["ok"])
        self.assertIn("requires successful run_command", rejected["error"])

        self.runtime.record_tool(
            "run_command",
            tool_output({"command": "python -m unittest", "exit_code": 0, "stdout": "OK"}),
        )
        staged = self.execute(
            "stage_memory",
            kind="sop",
            title="Calculator regression workflow",
            content="Patch calculator.py and run the complete unittest suite before reporting success.",
            keywords=["calculator", "tests"],
            evidence_ids=["e2"],
        )
        self.assertTrue(staged["ok"])
        committed = self.runtime.finish(
            success=True,
            task="Fix calculator",
            answer="Tests pass",
            messages=[{"role": "user", "content": "Fix calculator"}],
        )
        self.assertEqual(len(committed), 1)

        new_store = MemoryStore(self.state, self.workspace)
        self.assertIn("Calculator regression workflow", new_store.orientation())
        matches = new_store.search("calculator tests", kind="sop")
        self.assertEqual(matches[0]["title"], "Calculator regression workflow")
        self.assertIn("complete unittest suite", matches[0]["content"])
        self.assertIn("e2: run_command", matches[0]["evidence"][0])

    def test_sop_requires_a_recognized_verification_command(self) -> None:
        self.runtime.record_tool(
            "run_command",
            tool_output({"command": "py --version", "exit_code": 0, "stdout": "Python 3"}),
        )
        staged = self.execute(
            "stage_memory",
            kind="sop",
            title="Python environment check",
            content="Check the Python launcher version before running the project test suite.",
            evidence_ids=["e1"],
        )
        self.assertFalse(staged["ok"])
        self.assertIn("verification evidence", staged["error"])

    def test_later_file_edit_invalidates_staged_sop(self) -> None:
        self.runtime.record_tool(
            "run_command",
            tool_output({"command": "py -m unittest", "exit_code": 0, "stdout": "OK"}),
        )
        staged = self.execute(
            "stage_memory",
            kind="sop",
            title="Verified calculator workflow",
            content="Run the complete calculator unittest suite after changing its implementation.",
            evidence_ids=["e1"],
        )
        self.assertTrue(staged["ok"])
        self.runtime.record_tool(
            "edit_file", tool_output({"path": "calculator.py", "replacements": 1})
        )
        committed = self.runtime.finish(
            success=True,
            task="Fix calculator",
            answer="Done",
            messages=[],
        )
        self.assertEqual(committed, [])
        self.assertEqual(self.store.orientation(), "persistent_memory_index: empty")

    def test_later_file_edit_invalidates_staged_fact(self) -> None:
        self.runtime.record_tool(
            "read_file", tool_output({"path": "config.json", "total_lines": 4})
        )
        staged = self.execute(
            "stage_memory",
            kind="fact",
            title="Configuration mode",
            content="The checked configuration currently enables strict validation mode.",
            evidence_ids=["e1"],
        )
        self.assertTrue(staged["ok"])
        self.runtime.record_tool(
            "edit_file", tool_output({"path": "config.json", "replacements": 1})
        )
        committed = self.runtime.finish(
            success=True,
            task="Inspect configuration",
            answer="Done",
            messages=[],
        )
        self.assertEqual(committed, [])
        self.assertEqual(self.store.orientation(), "persistent_memory_index: empty")

    def test_failed_task_discards_staged_memory_but_archives_redacted_session(self) -> None:
        self.runtime.record_tool("read_file", tool_output({"path": "README.md", "total_lines": 10}))
        staged = self.execute(
            "stage_memory",
            kind="fact",
            title="README format",
            content="The repository documents its commands in a Markdown README file.",
            evidence_ids=["e1"],
        )
        self.assertTrue(staged["ok"])
        self.runtime.finish(
            success=False,
            task="Inspect with " + "sk-" + "abcdefghijklmnopqrstuvwxyz123456",
            answer="Blocked",
            messages=[{"role": "user", "content": "password=super-secret"}],
        )
        self.assertEqual(self.store.orientation(), "persistent_memory_index: empty")
        archives = list((self.store.root / "sessions").glob("*.json"))
        self.assertEqual(len(archives), 1)
        archive = archives[0].read_text(encoding="utf-8")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", archive)
        self.assertNotIn("super-secret", archive)
        self.assertIn("[REDACTED", archive)

    def test_all_memory_metadata_and_nested_tool_calls_are_redacted_and_bounded(self) -> None:
        secret = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
        plain_password = "correct horse battery staple"
        self.store.commit(
            MemoryCandidate(
                kind="fact",
                title=f"Credential {secret}",
                content=f"Reusable content with authorization: Bearer {secret}",
                keywords=(f"password={plain_password}",),
                evidence=(f"e1: run_command - api_key={secret}",),
            )
        )
        self.store.archive(
            task=f"Inspect {secret}",
            answer="Completed",
            success=True,
            messages=[
                {
                    "role": "assistant",
                    "content": json.dumps({"password": plain_password}),
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {
                                        "api_key": secret,
                                        "database_password": plain_password,
                                        "github_token": "ordinary-token-value",
                                        "OPENAI_API_KEY": "ordinary-key-value",
                                        "AWS_ACCESS_KEY_ID": "ordinary-access-key",
                                        "AWS_SECRET_ACCESS_KEY": "ordinary-aws-secret",
                                        "GITHUB_PAT": "ordinary-github-pat",
                                        "SESSION_COOKIE": "ordinary-session-cookie",
                                        "password": plain_password,
                                        "content": (
                                            "DATABASE_URL=postgres://user:database-password@host/db\n"
                                            + "x" * 200_000
                                        ),
                                    }
                                ),
                            },
                        }
                    ],
                }
            ],
        )
        stored = "\n".join(
            path.read_text(encoding="utf-8")
            for path in self.store.root.rglob("*.json")
        )
        self.assertNotIn(secret, stored)
        self.assertNotIn(plain_password, stored)
        self.assertNotIn("ordinary-token-value", stored)
        self.assertNotIn("ordinary-key-value", stored)
        self.assertNotIn("ordinary-access-key", stored)
        self.assertNotIn("ordinary-aws-secret", stored)
        self.assertNotIn("ordinary-github-pat", stored)
        self.assertNotIn("ordinary-session-cookie", stored)
        self.assertNotIn("database-password", stored)
        self.assertIn("[REDACTED", stored)
        archives = list((self.store.root / "sessions").glob("*.json"))
        self.assertEqual(len(archives), 1)
        self.assertLess(archives[0].stat().st_size, 75_000)

    def test_archive_has_a_hard_total_size_and_message_limit(self) -> None:
        messages = [{"role": "tool", "content": ""} for _ in range(10_000)]
        self.store.archive("Inspect", "Done", True, messages)
        archive_path = next((self.store.root / "sessions").glob("*.json"))
        archive = json.loads(archive_path.read_text(encoding="utf-8"))
        self.assertLessEqual(archive_path.stat().st_size, 60_000)
        self.assertLessEqual(len(archive["messages"]), 200)
        self.assertTrue(archive["messages_truncated"])

    def test_archive_replaces_unencodable_unicode(self) -> None:
        self.store.archive(
            "Inspect \ud800",
            "Done \udfff",
            True,
            [{"role": "user", "content": "Value \ud800"}],
        )
        archive_path = next((self.store.root / "sessions").glob("*.json"))
        archive_path.read_text(encoding="utf-8")

    def test_l1_index_is_bounded_and_workspaces_are_isolated(self) -> None:
        for number in range(40):
            self.store.commit(
                MemoryCandidate(
                    kind="fact",
                    title=f"Fact category {number}",
                    content=f"Reusable detail for category {number} that is not injected by default.",
                    keywords=(f"key-{number}",),
                    evidence=("e1: read_file - verified",),
                )
            )
        orientation = self.store.orientation(max_entries=12, max_chars=900)
        self.assertLessEqual(len(orientation), 900)
        self.assertNotIn("Reusable detail", orientation)

        other_workspace = self.workspace.parent / "other"
        other_workspace.mkdir()
        other_store = MemoryStore(self.state, other_workspace)
        self.assertEqual(other_store.orientation(), "persistent_memory_index: empty")

    def test_concurrent_commits_do_not_lose_index_entries(self) -> None:
        def commit(number: int) -> None:
            MemoryStore(self.state, self.workspace).commit(
                MemoryCandidate(
                    kind="fact",
                    title=f"Concurrent fact {number}",
                    content=f"Reusable concurrent detail number {number}.",
                    keywords=("concurrency",),
                    evidence=("e1: read_file - verified",),
                )
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(commit, range(24)))
        index = json.loads(self.store.index_path.read_text(encoding="utf-8"))
        self.assertEqual(len(index["facts"]), 24)


if __name__ == "__main__":
    unittest.main()
