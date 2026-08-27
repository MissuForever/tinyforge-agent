from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from tinyforge.tools import WorkspaceTools


class WorkspaceToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.tools = WorkspaceTools(self.root, command_timeout=5, max_output=10_000)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def execute(self, name: str, **arguments: object) -> dict[str, object]:
        return json.loads(self.tools.execute(name, json.dumps(arguments)))

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

    def test_dangerous_command_is_blocked(self) -> None:
        result = self.execute("run_command", command="git reset --hard HEAD")
        self.assertFalse(result["ok"])
        self.assertIn("blocked by safety policy", result["error"])

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


if __name__ == "__main__":
    unittest.main()
