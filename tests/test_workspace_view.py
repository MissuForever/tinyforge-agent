from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tinyforge.workspace_view import (
    _is_link_or_junction,
    _parse_git_status,
    preview_workspace_file,
    scan_workspace,
)


class WorkspaceViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_git_index_filters_paths_and_preserves_status(self) -> None:
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        (self.root / "new file.py").write_text("value = 1\n", encoding="utf-8")
        (self.root / ".env").write_text("TOKEN=hidden\n", encoding="utf-8")
        (self.root / ".secrets").mkdir()
        (self.root / ".secrets" / "notes.txt").write_text("hidden\n", encoding="utf-8")
        with patch(
            "tinyforge.workspace_view._git_workspace_files",
            return_value=(
                [
                    "src/app.py",
                    "new file.py",
                    ".env",
                    ".demo/video.mp4",
                    ".secrets/notes.txt",
                    ".envrc.d/notes.txt",
                    "../escape.py",
                ],
                {"src/app.py": " M", "new file.py": "??"},
            ),
        ):
            index = scan_workspace(self.root)

        self.assertTrue(index.git_available)
        self.assertEqual(
            [(entry.relative_path, entry.git_status) for entry in index.files],
            [("new file.py", "??"), ("src/app.py", " M")],
        )

    def test_fallback_scan_is_bounded_and_does_not_follow_directory_links(self) -> None:
        (self.root / "src").mkdir()
        (self.root / "src" / "main.py").write_text("value = 1\n", encoding="utf-8")
        (self.root / ".env").write_text("PASSWORD=hidden\n", encoding="utf-8")
        (self.root / "id_ecdsa").write_text("hidden key\n", encoding="utf-8")
        (self.root / "id_ed25519_sk").write_text("hidden key\n", encoding="utf-8")
        (self.root / ".envrc").write_text("export TOKEN=hidden\n", encoding="utf-8")
        (self.root / ".demo").mkdir()
        (self.root / ".demo" / "video.mp4").write_bytes(b"video")
        (self.root / ".secrets").mkdir()
        (self.root / ".secrets" / "notes.txt").write_text("hidden", encoding="utf-8")
        (self.root / ".envrc.d").mkdir()
        (self.root / ".envrc.d" / "notes.txt").write_text("hidden", encoding="utf-8")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "package.js").write_text("hidden", encoding="utf-8")

        external = self.root.parent / f"{self.root.name}-external"
        external.mkdir(exist_ok=True)
        (external / "outside.py").write_text("outside = True\n", encoding="utf-8")
        link_created = False
        try:
            (self.root / "external-link").symlink_to(external, target_is_directory=True)
            link_created = True
        except OSError:
            pass
        try:
            with patch("tinyforge.workspace_view._git_workspace_files", return_value=None):
                index = scan_workspace(self.root, max_entries=20)
        finally:
            (external / "outside.py").unlink(missing_ok=True)
            external.rmdir()

        paths = [entry.relative_path for entry in index.files]
        self.assertIn("src/main.py", paths)
        self.assertNotIn(".env", paths)
        self.assertNotIn(".envrc", paths)
        self.assertNotIn("id_ecdsa", paths)
        self.assertNotIn("id_ed25519_sk", paths)
        self.assertNotIn(".demo/video.mp4", paths)
        self.assertNotIn(".secrets/notes.txt", paths)
        self.assertNotIn(".envrc.d/notes.txt", paths)
        self.assertNotIn("node_modules/package.js", paths)
        self.assertFalse(any(path.endswith("outside.py") for path in paths))
        if link_created:
            link = next(entry for entry in index.files if entry.relative_path == "external-link")
            self.assertTrue(link.is_link)

    def test_git_index_honors_entry_limit(self) -> None:
        with patch(
            "tinyforge.workspace_view._git_workspace_files",
            return_value=([f"file-{index}.py" for index in range(5)], {}),
        ):
            index = scan_workspace(self.root, max_entries=2)
        self.assertEqual(len(index.files), 2)
        self.assertTrue(index.truncated)

    def test_git_failure_never_falls_back_across_ignore_rules(self) -> None:
        (self.root / ".git").mkdir()
        (self.root / ".gitignore").write_text("private-data/\n", encoding="utf-8")
        (self.root / "private-data").mkdir()
        (self.root / "private-data" / "customer.txt").write_text(
            "VISIBLE_PRIVATE_CONTENT\n",
            encoding="utf-8",
        )

        with patch("tinyforge.workspace_view._git_workspace_files", return_value=None):
            index = scan_workspace(self.root)

        self.assertTrue(index.git_available)
        self.assertTrue(index.error)
        self.assertEqual(index.files, ())

    def test_fallback_counts_sensitive_entries_toward_scan_limit(self) -> None:
        for index in range(10):
            (self.root / f".env.{index}").write_text("hidden\n", encoding="utf-8")
        with patch("tinyforge.workspace_view._git_workspace_files", return_value=None), patch(
            "tinyforge.workspace_view.MAX_WORKSPACE_SCAN_ENTRIES",
            3,
        ):
            index = scan_workspace(self.root)
        self.assertTrue(index.truncated)
        self.assertEqual(index.files, ())

    def test_legacy_windows_reparse_attribute_is_treated_as_a_link(self) -> None:
        class LegacyPath:
            @staticmethod
            def is_symlink() -> bool:
                return False

            @staticmethod
            def lstat() -> SimpleNamespace:
                return SimpleNamespace(st_file_attributes=0x400)

        self.assertTrue(_is_link_or_junction(LegacyPath()))  # type: ignore[arg-type]

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_real_windows_junction_is_a_non_previewable_leaf(self) -> None:
        external = self.root.parent / f"{self.root.name}-junction-target"
        external.mkdir()
        (external / "outside.py").write_text("outside = True\n", encoding="utf-8")
        junction = self.root / "external-junction"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(external)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if created.returncode != 0:
            (external / "outside.py").unlink()
            external.rmdir()
            self.skipTest("Could not create a Windows junction")
        try:
            with patch("tinyforge.workspace_view._git_workspace_files", return_value=None):
                index = scan_workspace(self.root)
            entries = {entry.relative_path: entry for entry in index.files}
            self.assertIn("external-junction", entries)
            self.assertTrue(entries["external-junction"].is_link)
            self.assertNotIn("external-junction/outside.py", entries)
            self.assertEqual(
                preview_workspace_file(self.root, "external-junction").status,
                "link",
            )
        finally:
            if os.path.lexists(junction):
                os.rmdir(junction)
            (external / "outside.py").unlink(missing_ok=True)
            external.rmdir()

    @unittest.skipIf(shutil.which("git") is None, "Git is not installed")
    def test_real_git_index_respects_ignore_and_reports_status(self) -> None:
        subprocess.run(
            ["git", "init", "--quiet"],
            cwd=self.root,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (self.root / ".gitignore").write_text(".env\nignored/\n", encoding="utf-8")
        (self.root / "tracked.py").write_text("value = 1\n", encoding="utf-8")
        (self.root / "removed").mkdir()
        (self.root / "removed" / "gone.py").write_text("gone = False\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", ".gitignore", "tracked.py", "removed/gone.py"],
            cwd=self.root,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (self.root / "tracked.py").write_text("value = 2\n", encoding="utf-8")
        (self.root / "removed" / "gone.py").unlink()
        (self.root / "removed").rmdir()
        (self.root / "untracked.py").write_text("value = 3\n", encoding="utf-8")
        (self.root / ".env").write_text("PASSWORD=hidden\n", encoding="utf-8")
        (self.root / ".envrc").write_text("export TOKEN=hidden\n", encoding="utf-8")
        (self.root / "ignored").mkdir()
        (self.root / "ignored" / "output.txt").write_text("hidden\n", encoding="utf-8")

        index = scan_workspace(self.root)
        entries = {entry.relative_path: entry.git_status for entry in index.files}

        self.assertTrue(index.git_available)
        self.assertIn("A", entries["tracked.py"])
        self.assertEqual(entries["untracked.py"], "??")
        self.assertIn("D", entries["removed/gone.py"])
        self.assertNotIn(".env", entries)
        self.assertNotIn("ignored/output.txt", entries)

    @unittest.skipIf(shutil.which("git") is None, "Git is not installed")
    def test_git_output_limit_fails_closed(self) -> None:
        subprocess.run(
            ["git", "init", "--quiet"],
            cwd=self.root,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (self.root / "tracked-long-name.py").write_text("value = 1\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "tracked-long-name.py"],
            cwd=self.root,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        with patch("tinyforge.workspace_view.MAX_GIT_OUTPUT_BYTES", 4):
            index = scan_workspace(self.root)

        self.assertTrue(index.git_available)
        self.assertTrue(index.error)
        self.assertEqual(index.files, ())

    def test_parse_git_status_handles_spaces_unicode_and_renames(self) -> None:
        raw = (
            b" M src/app.py\0"
            b"?? new file.py\0"
            + "A  \u4e2d\u6587.py\0".encode()
            + b"R  renamed.py\0old.py\0"
        )
        self.assertEqual(
            _parse_git_status(raw),
            {
                "src/app.py": " M",
                "new file.py": "??",
                "\u4e2d\u6587.py": "A ",
                "renamed.py": "R ",
            },
        )

    def test_preview_redacts_secrets_and_cleans_untrusted_controls(self) -> None:
        secret = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
        path = self.root / "app.py"
        path.write_bytes(
            f"token = '{secret}'\r\nleft\u202eright\n".encode("utf-8")
        )

        preview = preview_workspace_file(self.root, "app.py")

        self.assertEqual(preview.status, "text")
        self.assertEqual(preview.line_count, 3)
        self.assertNotIn(secret, preview.text)
        self.assertNotIn("\u202e", preview.text)
        self.assertIn("[REDACTED", preview.text)

    def test_preview_rejects_sensitive_outside_link_binary_and_large_files(self) -> None:
        (self.root / ".env").write_text("PASSWORD=hidden\n", encoding="utf-8")
        (self.root / "binary.dat").write_bytes(b"x" * 9_000 + b"\0")
        (self.root / "large.txt").write_text("x" * 20, encoding="utf-8")
        (self.root / "invalid.txt").write_bytes(b"\xff\xfe")
        (self.root / "putty.ppk").write_text(
            "PuTTY-User-Key-File-3: ssh-ed25519\nPrivate-Lines: 1\nDUMMY\n",
            encoding="utf-8",
        )
        (self.root / "ordinary.txt").write_text(
            "-----BEGIN OPENSSH PRIVATE KEY-----\nDUMMY_PRIVATE_MATERIAL\n"
            "-----END OPENSSH PRIVATE KEY-----\n",
            encoding="utf-8",
        )

        self.assertEqual(preview_workspace_file(self.root, ".env").status, "sensitive")
        self.assertEqual(preview_workspace_file(self.root, ".envrc").status, "sensitive")
        self.assertEqual(preview_workspace_file(self.root, "id_ecdsa").status, "sensitive")
        self.assertEqual(
            preview_workspace_file(self.root, "id_ed25519_sk").status,
            "sensitive",
        )
        self.assertEqual(
            preview_workspace_file(self.root, ".secrets/notes.txt").status,
            "sensitive",
        )
        self.assertEqual(
            preview_workspace_file(self.root, ".envrc.d/notes.txt").status,
            "sensitive",
        )
        self.assertEqual(preview_workspace_file(self.root, "../outside").status, "outside")
        self.assertEqual(preview_workspace_file(self.root, "bad\x00path").status, "outside")
        self.assertEqual(preview_workspace_file(self.root, "binary.dat").status, "binary")
        self.assertEqual(
            preview_workspace_file(self.root, "large.txt", max_bytes=16).status,
            "too_large",
        )
        self.assertEqual(preview_workspace_file(self.root, "invalid.txt").status, "binary")
        self.assertEqual(preview_workspace_file(self.root, "putty.ppk").status, "sensitive")
        self.assertEqual(
            preview_workspace_file(self.root, "ordinary.txt").status,
            "sensitive",
        )
        self.assertEqual(preview_workspace_file(self.root, "missing.py").status, "missing")

        external = self.root.parent / f"{self.root.name}-target.txt"
        external.write_text("outside\n", encoding="utf-8")
        try:
            link = self.root / "linked.txt"
            try:
                link.symlink_to(external)
            except OSError:
                return
            self.assertEqual(preview_workspace_file(self.root, "linked.txt").status, "link")
        finally:
            external.unlink(missing_ok=True)

    def test_preview_rejects_absolute_paths(self) -> None:
        absolute = str((self.root / "file.py").resolve())
        self.assertEqual(preview_workspace_file(self.root, absolute).status, "outside")
        self.assertEqual(
            preview_workspace_file(self.root, r"C:\outside\file.py").status,
            "outside",
        )
        self.assertEqual(
            preview_workspace_file(self.root, r"C:relative-drive.py").status,
            "outside",
        )


if __name__ == "__main__":
    unittest.main()
