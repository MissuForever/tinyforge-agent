from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tinyforge.skills as skills_module
from tinyforge.agent import Agent, AgentEvent
from tinyforge.config import Config
from tinyforge.memory import WorkingMemory
from tinyforge.model import AssistantReply, ToolCall
from tinyforge.runtime import build_agent
from tinyforge.skills import (
    MAX_RESOURCE_BYTES,
    MAX_RESOURCE_OUTPUT_CHARS,
    MAX_RESOURCE_PATH_CHARS,
    MAX_SKILL_BYTES,
    MAX_TOOL_OUTPUT_CHARS,
    SkillCatalog,
    SkillError,
    SkillRuntime,
)
from tinyforge.tools import CompositeTools


def write_skill(
    root: Path,
    name: str,
    *,
    description: str = "Use for a verified change.",
    body: str = "Inspect the relevant files, make a focused change, and verify it.",
) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return directory


class _FinalModel:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, object]], list[dict[str, object]]]] = []

    def complete(self, messages, tools):
        self.calls.append((messages, tools))
        return AssistantReply("TASK_COMPLETE: done", ())


class _LoadSkillModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return AssistantReply(
                "",
                (ToolCall("skill-call", "load_skill", '{"skill_id":"workspace:verify-change"}'),),
            )
        return AssistantReply("TASK_COMPLETE: loaded", ())


class _SkillFailureModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return AssistantReply("", (ToolCall("list-call", "list_skills", "{}"),))
        if self.calls == 2:
            return AssistantReply(
                "",
                (ToolCall("load-call", "load_skill", '{"skill_id":"workspace:verify-change"}'),),
            )
        if self.calls == 3:
            return AssistantReply("", (ToolCall("failure-call", "fail_check", "{}"),))
        return AssistantReply("TASK_BLOCKED: the verification failed", ())


class _SameBatchSkillFailureModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return AssistantReply(
                "",
                (
                    ToolCall(
                        "batch-load",
                        "load_skill",
                        '{"skill_id":"workspace:verify-change"}',
                    ),
                    ToolCall("batch-failure", "fail_check", "{}"),
                ),
            )
        return AssistantReply("TASK_BLOCKED: the batched verification failed", ())


class _CompressedContextSkillFailureModel:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def complete(self, messages, tools):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            return AssistantReply(
                "",
                (
                    ToolCall(
                        "compressed-load",
                        "load_skill",
                        '{"skill_id":"workspace:verify-change"}',
                    ),
                ),
            )
        if len(self.calls) == 2:
            return AssistantReply(
                "", (ToolCall("context-fill", "large_observation", "{}"),)
            )
        if len(self.calls) == 3:
            return AssistantReply(
                "", (ToolCall("compressed-failure", "fail_check", "{}"),)
            )
        return AssistantReply("TASK_BLOCKED: the later verification failed", ())


class _FailingToolProvider:
    @property
    def definitions(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "fail_check",
                    "description": "Return one structured failure for a test.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "large_observation",
                    "description": "Return a large successful observation for a test.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

    def execute(self, name, arguments):
        if name == "large_observation":
            return json.dumps({"ok": True, "result": {"text": "x" * 6_000}})
        return json.dumps({"ok": False, "error": "focused check failed"})


class SkillCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.user = self.root / "user-skills"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    @property
    def workspace_skills(self) -> Path:
        return self.workspace / ".tinyforge" / "skills"

    def test_progressive_disclosure_and_resource_paging(self) -> None:
        write_skill(
            self.user,
            "user-check",
            description="Use for user checks.",
            body="User-only instructions.",
        )
        workspace_skill = write_skill(
            self.workspace_skills,
            "verify-change",
            description="Use for changes that need a repeated verification command.",
            body="Read references/python.md only for Python projects.",
        )
        references = workspace_skill / "references"
        references.mkdir()
        (references / "python.md").write_text(
            "# Python\nRun the focused test.\nRun the full suite.\n",
            encoding="utf-8",
        )
        catalog = SkillCatalog(self.workspace, self.user)
        runtime = SkillRuntime(catalog, enabled=True)

        listed_raw = runtime.execute("list_skills", "{}")
        listed = json.loads(listed_raw)
        self.assertTrue(listed["ok"])
        self.assertEqual(
            [item["id"] for item in listed["result"]["skills"]],
            ["user:user-check", "workspace:verify-change"],
        )
        self.assertNotIn("User-only instructions", listed_raw)
        self.assertNotIn(str(self.root), listed_raw)
        self.assertTrue(listed["result"]["untrusted"])

        blocked = json.loads(
            runtime.execute(
                "read_skill_resource",
                json.dumps(
                    {"skill_id": "workspace:verify-change", "path": "references/python.md"}
                ),
            )
        )
        self.assertFalse(blocked["ok"])
        self.assertIn("load_skill", blocked["error"])

        loaded_raw = runtime.execute(
            "load_skill", '{"skill_id":"workspace:verify-change"}'
        )
        loaded = json.loads(loaded_raw)
        self.assertTrue(loaded["ok"])
        self.assertIn("Read references/python.md", loaded["result"]["instructions"])
        self.assertEqual(loaded["result"]["skill"]["scope"], "workspace")
        self.assertEqual(
            loaded["result"]["resources"],
            [{"path": "references/python.md", "readable": True}],
        )
        self.assertNotIn(str(self.root), loaded_raw)

        resource = json.loads(
            runtime.execute(
                "read_skill_resource",
                json.dumps(
                    {
                        "skill_id": "workspace:verify-change",
                        "path": "references/python.md",
                        "start_line": 2,
                        "end_line": 3,
                    }
                ),
            )
        )
        self.assertTrue(resource["ok"])
        self.assertEqual(
            resource["result"]["content"],
            "Run the focused test.\nRun the full suite.",
        )
        self.assertIn("[loaded] workspace:verify-change", runtime.overview())

    def test_task_query_ranks_bounded_candidates_and_empty_query_browses(self) -> None:
        write_skill(
            self.workspace_skills,
            "python-tests",
            description="Use for Python unittest failures and regression checks.",
        )
        write_skill(
            self.workspace_skills,
            "css-layout",
            description="Use for CSS layout and responsive styling work.",
        )
        runtime = SkillRuntime(SkillCatalog(self.workspace, self.user), enabled=True)
        runtime.start_task("Repair the Python unittest failure")

        ranked = json.loads(runtime.execute("list_skills", '{"max_results":1}'))
        explicit = json.loads(runtime.execute("list_skills", '{"query":"CSS"}'))
        browse = json.loads(runtime.execute("list_skills", '{"query":""}'))

        self.assertEqual(ranked["result"]["skills"][0]["id"], "workspace:python-tests")
        self.assertGreater(ranked["result"]["skills"][0]["relevance"], 0)
        self.assertEqual(ranked["result"]["retrieval"]["query_source"], "task")
        self.assertEqual(explicit["result"]["skills"][0]["id"], "workspace:css-layout")
        self.assertEqual(
            [item["id"] for item in browse["result"]["skills"]],
            ["workspace:css-layout", "workspace:python-tests"],
        )

    def test_loaded_resource_snapshot_rejects_changes_and_new_files(self) -> None:
        skill_dir = write_skill(self.workspace_skills, "stable-resources")
        references = skill_dir / "references"
        references.mkdir()
        notes = references / "notes.md"
        notes.write_text("original guidance", encoding="utf-8")
        runtime = SkillRuntime(SkillCatalog(self.workspace, self.user), enabled=True)

        loaded = json.loads(
            runtime.execute("load_skill", '{"skill_id":"workspace:stable-resources"}')
        )
        self.assertEqual(
            len(loaded["result"]["skill"]["resource_manifest_sha256"]), 64
        )

        notes.write_text("changed guidance with a different size", encoding="utf-8")
        changed = json.loads(
            runtime.execute(
                "read_skill_resource",
                '{"skill_id":"workspace:stable-resources","path":"references/notes.md"}',
            )
        )
        (references / "late.md").write_text("late", encoding="utf-8")
        added = json.loads(
            runtime.execute(
                "read_skill_resource",
                '{"skill_id":"workspace:stable-resources","path":"references/late.md"}',
            )
        )

        self.assertFalse(changed["ok"])
        self.assertIn("changed after", changed["error"])
        self.assertFalse(added["ok"])
        self.assertIn("not present", added["error"])

    def test_resource_snapshot_detects_content_change_with_same_stat_signature(self) -> None:
        skill_dir = write_skill(self.workspace_skills, "content-digest")
        references = skill_dir / "references"
        references.mkdir()
        notes = references / "notes.md"
        notes.write_text("AAAA", encoding="utf-8")
        runtime = SkillRuntime(SkillCatalog(self.workspace, self.user), enabled=True)

        loaded = json.loads(
            runtime.execute("load_skill", '{"skill_id":"workspace:content-digest"}')
        )
        original_stat = notes.stat()
        notes.write_text("BBBB", encoding="utf-8")
        os.utime(
            notes,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        replaced_stat = notes.stat()
        self.assertEqual(
            (
                original_stat.st_dev,
                original_stat.st_ino,
                original_stat.st_size,
                original_stat.st_mtime_ns,
            ),
            (
                replaced_stat.st_dev,
                replaced_stat.st_ino,
                replaced_stat.st_size,
                replaced_stat.st_mtime_ns,
            ),
        )

        changed = json.loads(
            runtime.execute(
                "read_skill_resource",
                '{"skill_id":"workspace:content-digest","path":"references/notes.md"}',
            )
        )
        replacement_runtime = SkillRuntime(
            SkillCatalog(self.workspace, self.user), enabled=True
        )
        replacement = json.loads(
            replacement_runtime.execute(
                "load_skill", '{"skill_id":"workspace:content-digest"}'
            )
        )

        self.assertFalse(changed["ok"])
        self.assertIn("content changed", changed["error"])
        self.assertNotEqual(
            loaded["result"]["skill"]["resource_manifest_sha256"],
            replacement["result"]["skill"]["resource_manifest_sha256"],
        )

    def test_fault_report_uses_only_skills_active_at_the_failed_step(self) -> None:
        write_skill(self.workspace_skills, "first-skill")
        write_skill(self.workspace_skills, "late-skill")
        runtime = SkillRuntime(SkillCatalog(self.workspace, self.user), enabled=True)
        runtime.start_task("Run a focused verification")

        first_output = runtime.execute(
            "load_skill", '{"skill_id":"workspace:first-skill"}'
        )
        runtime.record_tool("load-first", "load_skill", "{}", first_output)
        runtime.sync_context({"load-first"})
        runtime.record_tool(
            "failed-check",
            "run_command",
            "{}",
            json.dumps({"ok": True, "result": {"exit_code": 7}}),
        )
        late_output = runtime.execute("load_skill", '{"skill_id":"workspace:late-skill"}')
        runtime.record_tool("load-late", "load_skill", "{}", late_output)

        report = runtime.finish_task(success=False)

        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report["localized_step"], 2)
        self.assertEqual(report["call_id"], "failed-check")
        self.assertEqual(
            [item["id"] for item in report["active_skill_candidates"]],
            ["workspace:first-skill"],
        )
        self.assertEqual(report["attribution_status"], "unresolved")
        self.assertEqual(report["qualification_status"], "not_run")
        self.assertFalse(report["skill_mutation_applied"])
        self.assertIn("no Skill was changed", runtime.overview())

        self.assertIsNone(runtime.finish_task(success=True))
        self.assertIsNone(runtime.last_fault_report)

    def test_disabled_skills_do_not_capture_or_report_task_trajectory(self) -> None:
        runtime = SkillRuntime(SkillCatalog(self.workspace, self.user), enabled=False)
        runtime.start_task("A failing task with Skills disabled")
        runtime.record_tool(
            "failed-check",
            "run_command",
            "{}",
            json.dumps({"ok": True, "result": {"exit_code": 2}}),
        )

        self.assertEqual(runtime.trace, [])
        self.assertIsNone(runtime.finish_task(success=False))
        self.assertIsNone(runtime.last_fault_report)

    def test_discovery_reads_only_frontmatter_and_loads_body_lazily(self) -> None:
        marker = "BODY_IS_LOADED_ONLY_ON_DEMAND"
        write_skill(
            self.workspace_skills,
            "lazy-body",
            description="Use to verify lazy Skill loading.",
            body=marker,
        )

        with patch(
            "tinyforge.skills._read_bounded",
            side_effect=AssertionError("discovery attempted a full SKILL.md read"),
        ) as full_read:
            catalog = SkillCatalog(self.workspace, self.user)

        full_read.assert_not_called()
        skill = catalog.resolve("workspace:lazy-body")
        self.assertFalse(hasattr(skill, "body"))
        runtime = SkillRuntime(catalog, enabled=True)
        listed = runtime.execute("list_skills", "{}")
        self.assertNotIn(marker, listed)

        loaded = json.loads(
            runtime.execute("load_skill", '{"skill_id":"workspace:lazy-body"}')
        )
        self.assertTrue(loaded["ok"])
        self.assertEqual(loaded["result"]["instructions"], marker)
        self.assertEqual(len(loaded["result"]["skill"]["sha256"]), 64)

    def test_skill_file_change_after_discovery_fails_closed(self) -> None:
        skill_dir = write_skill(self.workspace_skills, "frozen-skill", body="original")
        runtime = SkillRuntime(SkillCatalog(self.workspace, self.user), enabled=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            skill_file.read_text(encoding="utf-8") + "changed after discovery\n",
            encoding="utf-8",
        )

        loaded = json.loads(
            runtime.execute("load_skill", '{"skill_id":"workspace:frozen-skill"}')
        )

        self.assertFalse(loaded["ok"])
        self.assertIn("changed", loaded["error"])
        self.assertNotIn("workspace:frozen-skill", runtime.loaded)

    def test_same_name_uses_stable_scope_ids_and_rejects_ambiguous_alias(self) -> None:
        write_skill(self.user, "same-name")
        write_skill(self.workspace_skills, "same-name")
        runtime = SkillRuntime(SkillCatalog(self.workspace, self.user), enabled=True)

        ambiguous = json.loads(runtime.execute("load_skill", '{"skill_id":"same-name"}'))
        explicit = json.loads(
            runtime.execute("load_skill", '{"skill_id":"workspace:same-name"}')
        )

        self.assertFalse(ambiguous["ok"])
        self.assertIn("ambiguous", ambiguous["error"])
        self.assertTrue(explicit["ok"])
        self.assertEqual(explicit["result"]["skill"]["id"], "workspace:same-name")

    def test_invalid_frontmatter_and_oversized_skill_are_isolated(self) -> None:
        write_skill(self.workspace_skills, "valid-skill")
        mismatch = write_skill(self.workspace_skills, "directory-name")
        (mismatch / "SKILL.md").write_text(
            "---\nname: different-name\ndescription: mismatch\n---\nbody\n",
            encoding="utf-8",
        )
        duplicate = self.workspace_skills / "duplicate-key"
        duplicate.mkdir()
        (duplicate / "SKILL.md").write_text(
            "---\nname: duplicate-key\nname: duplicate-key\ndescription: duplicate\n---\nbody\n",
            encoding="utf-8",
        )
        unknown = self.workspace_skills / "unknown-key"
        unknown.mkdir()
        (unknown / "SKILL.md").write_text(
            "---\nname: unknown-key\ndescription: unknown\nversion: 1\n---\nbody\n",
            encoding="utf-8",
        )
        oversized = self.workspace_skills / "oversized"
        oversized.mkdir()
        oversized_header = (
            b"---\nname: oversized\ndescription: valid metadata with a large body\n---\n"
        )
        (oversized / "SKILL.md").write_bytes(
            oversized_header + b"x" * (MAX_SKILL_BYTES + 1 - len(oversized_header))
        )

        catalog = SkillCatalog(self.workspace, self.user)

        self.assertEqual(list(catalog.skills), ["workspace:valid-skill"])
        self.assertEqual(len(catalog.issues), 4)
        self.assertTrue(all(issue.code == "invalid_skill" for issue in catalog.issues))

    def test_discovery_and_resource_listing_have_total_scan_budgets(self) -> None:
        for index in range(5):
            (self.user / f"invalid-{index}").mkdir(parents=True)
        write_skill(self.workspace_skills, "would-be-valid")

        with patch("tinyforge.skills.MAX_SKILL_SCAN_ENTRIES", 3):
            catalog = SkillCatalog(self.workspace, self.user)

        self.assertEqual(catalog.skills, {})
        self.assertIn("scan_limit", {issue.code for issue in catalog.issues})

        skill_dir = write_skill(self.workspace_skills, "bounded-resources")
        references = skill_dir / "references"
        references.mkdir()
        for index in range(5):
            (references / f"item-{index}.md").write_text("text", encoding="utf-8")
        catalog = SkillCatalog(self.workspace, self.root / "empty-user")
        skill = catalog.resolve("workspace:bounded-resources")
        with patch("tinyforge.skills.MAX_RESOURCE_SCAN_ENTRIES", 2):
            resources = catalog.resources(skill)
        self.assertLessEqual(len(resources), 2)

    def test_resource_paths_and_oversized_resources_fail_closed(self) -> None:
        skill_dir = write_skill(self.workspace_skills, "safe-resource")
        references = skill_dir / "references"
        assets = skill_dir / "assets"
        references.mkdir()
        assets.mkdir()
        (references / "large.md").write_bytes(b"x" * (MAX_RESOURCE_BYTES + 1))
        (assets / "notes.txt").write_text("not readable through the tool", encoding="utf-8")
        runtime = SkillRuntime(SkillCatalog(self.workspace, self.user), enabled=True)
        runtime.execute("load_skill", '{"skill_id":"workspace:safe-resource"}')

        unsafe_paths = (
            "../outside.md",
            "/absolute.md",
            "C:/secret.txt",
            "references\\secret.md",
            "references/file.txt:stream",
            "references/NUL.txt",
            "assets/notes.txt",
        )
        for unsafe in unsafe_paths:
            with self.subTest(path=unsafe):
                payload = json.loads(
                    runtime.execute(
                        "read_skill_resource",
                        json.dumps(
                            {"skill_id": "workspace:safe-resource", "path": unsafe}
                        ),
                    )
                )
                self.assertFalse(payload["ok"])

        oversized = json.loads(
            runtime.execute(
                "read_skill_resource",
                '{"skill_id":"workspace:safe-resource","path":"references/large.md"}',
            )
        )
        self.assertFalse(oversized["ok"])

        too_long = "references/" + "a" * MAX_RESOURCE_PATH_CHARS
        long_path = json.loads(
            runtime.execute(
                "read_skill_resource",
                json.dumps(
                    {"skill_id": "workspace:safe-resource", "path": too_long}
                ),
            )
        )
        self.assertFalse(long_path["ok"])
        self.assertIn("too long", long_path["error"])

    def test_empty_resource_and_output_truncation_boundaries(self) -> None:
        skill_dir = write_skill(self.workspace_skills, "resource-boundaries")
        references = skill_dir / "references"
        references.mkdir()
        (references / "empty.txt").write_bytes(b"")
        (references / "exact.txt").write_text(
            "x" * MAX_RESOURCE_OUTPUT_CHARS,
            encoding="utf-8",
        )
        (references / "over.txt").write_text(
            "x" * (MAX_RESOURCE_OUTPUT_CHARS + 1),
            encoding="utf-8",
        )
        (references / "escaped.txt").write_text(
            '"' * (MAX_RESOURCE_OUTPUT_CHARS + 1),
            encoding="utf-8",
        )
        catalog = SkillCatalog(self.workspace, self.user)
        skill = catalog.resolve("workspace:resource-boundaries")

        empty = catalog.read_resource(skill, "references/empty.txt")
        exact = catalog.read_resource(skill, "references/exact.txt")
        over = catalog.read_resource(skill, "references/over.txt")

        self.assertEqual((empty["start_line"], empty["end_line"]), (0, 0))
        self.assertEqual(empty["content"], "")
        self.assertFalse(empty["truncated"])
        self.assertEqual(len(exact["content"]), MAX_RESOURCE_OUTPUT_CHARS)
        self.assertFalse(exact["truncated"])
        self.assertEqual(len(over["content"]), MAX_RESOURCE_OUTPUT_CHARS)
        self.assertTrue(over["truncated"])

        past_end = catalog.read_resource(
            skill,
            "references/exact.txt",
            start_line=MAX_RESOURCE_OUTPUT_CHARS + 1,
        )
        self.assertEqual((past_end["start_line"], past_end["end_line"]), (0, 0))
        self.assertEqual(past_end["content"], "")
        self.assertFalse(past_end["truncated"])

        runtime = SkillRuntime(catalog, enabled=True)
        runtime.execute("load_skill", '{"skill_id":"workspace:resource-boundaries"}')
        escaped = runtime.execute(
            "read_skill_resource",
            '{"skill_id":"workspace:resource-boundaries","path":"references/escaped.txt"}',
        )
        escaped_payload = json.loads(escaped)
        self.assertTrue(escaped_payload["ok"])
        self.assertTrue(escaped_payload["result"]["truncated"])
        self.assertLessEqual(len(escaped), MAX_TOOL_OUTPUT_CHARS)

    def test_workspace_root_and_intermediate_reparse_points_are_rejected(self) -> None:
        write_skill(self.workspace_skills, "unsafe-chain")
        original = skills_module._is_link_or_reparse
        targets = (self.workspace, self.workspace / ".tinyforge")

        for target in targets:
            with self.subTest(target=target.name):
                def reports_reparse(path: Path, *, expected: Path = target) -> bool:
                    return Path(path) == expected or original(Path(path))

                with patch(
                    "tinyforge.skills._is_link_or_reparse",
                    side_effect=reports_reparse,
                ):
                    catalog = SkillCatalog(self.workspace, self.user)
                self.assertNotIn("workspace:unsafe-chain", catalog.skills)
                self.assertIn("unsafe_root", {issue.code for issue in catalog.issues})

    def test_skill_directory_is_revalidated_after_load_read(self) -> None:
        skill_dir = write_skill(self.workspace_skills, "changing-skill")
        catalog = SkillCatalog(self.workspace, self.user)
        runtime = SkillRuntime(catalog, enabled=True)
        original_read = skills_module._read_bounded
        original_link_check = skills_module._is_link_or_reparse
        read_completed = False

        def read_then_change(*args, **kwargs):
            nonlocal read_completed
            raw = original_read(*args, **kwargs)
            read_completed = True
            return raw

        def reports_late_reparse(path: Path) -> bool:
            candidate = Path(path)
            if candidate == skill_dir and read_completed:
                return True
            return original_link_check(candidate)

        with patch(
            "tinyforge.skills._read_bounded",
            side_effect=read_then_change,
        ), patch(
            "tinyforge.skills._is_link_or_reparse",
            side_effect=reports_late_reparse,
        ):
            loaded = json.loads(
                runtime.execute("load_skill", '{"skill_id":"workspace:changing-skill"}')
            )

        self.assertFalse(loaded["ok"])
        self.assertIn("changed", loaded["error"])
        self.assertNotIn("workspace:changing-skill", runtime.loaded)

    def test_resource_listing_skips_unsafe_names_including_assets(self) -> None:
        skill_dir = write_skill(self.workspace_skills, "safe-listing")
        references = skill_dir / "references"
        assets = skill_dir / "assets"
        references.mkdir()
        assets.mkdir()
        (references / "safe.md").write_text("safe", encoding="utf-8")
        (assets / "safe.dat").write_bytes(b"asset")
        unsafe_reference = references / "hidden\u202e.md"
        unsafe_asset = assets / "hidden\u2066.dat"
        unsafe_reference.write_text("unsafe", encoding="utf-8")
        unsafe_asset.write_bytes(b"unsafe")
        catalog = SkillCatalog(self.workspace, self.user)
        skill = catalog.resolve("workspace:safe-listing")

        resources = catalog.resources(skill)
        paths = {item["path"] for item in resources}

        self.assertIn("references/safe.md", paths)
        self.assertIn("assets/safe.dat", paths)
        self.assertNotIn(unsafe_reference.relative_to(skill_dir).as_posix(), paths)
        self.assertNotIn(unsafe_asset.relative_to(skill_dir).as_posix(), paths)

    def test_assets_cannot_exhaust_budget_before_readable_resources(self) -> None:
        skill_dir = write_skill(self.workspace_skills, "resource-priority")
        references = skill_dir / "references"
        scripts = skill_dir / "scripts"
        assets = skill_dir / "assets"
        references.mkdir()
        scripts.mkdir()
        assets.mkdir()
        (references / "guide.md").write_text("guide", encoding="utf-8")
        (scripts / "check.py").write_text("print('ok')", encoding="utf-8")
        for index in range(3):
            (assets / f"asset-{index}.bin").write_bytes(b"asset")
        catalog = SkillCatalog(self.workspace, self.user)
        skill = catalog.resolve("workspace:resource-priority")

        with patch("tinyforge.skills.MAX_RESOURCES", 2), patch(
            "tinyforge.skills.MAX_RESOURCE_SCAN_ENTRIES", 2
        ):
            resources = catalog.resources(skill)

        self.assertEqual(
            [item["path"] for item in resources],
            ["references/guide.md", "scripts/check.py"],
        )
        self.assertTrue(all(item["readable"] for item in resources))

    def test_resource_is_revalidated_immediately_before_read(self) -> None:
        skill_dir = write_skill(self.workspace_skills, "changing-resource")
        references = skill_dir / "references"
        references.mkdir()
        resource = references / "notes.md"
        resource.write_text("trusted snapshot", encoding="utf-8")
        catalog = SkillCatalog(self.workspace, self.user)
        skill = catalog.resolve("workspace:changing-resource")
        original = skills_module._is_link_or_reparse
        resource_checks = 0

        def changes_to_reparse(path: Path) -> bool:
            nonlocal resource_checks
            candidate = Path(path)
            if candidate == resource:
                resource_checks += 1
                return resource_checks >= 3
            return original(candidate)

        with patch(
            "tinyforge.skills._is_link_or_reparse",
            side_effect=changes_to_reparse,
        ), patch(
            "tinyforge.skills._read_bounded",
            side_effect=AssertionError("unsafe resource reached the read operation"),
        ) as bounded_read:
            with self.assertRaises(SkillError):
                catalog.read_resource(skill, "references/notes.md")
        bounded_read.assert_not_called()

    def test_linked_skill_or_resource_is_not_read(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        outside_skill = write_skill(outside, "linked-skill")
        self.workspace_skills.mkdir(parents=True)
        linked_dir = self.workspace_skills / "linked-skill"
        try:
            os.symlink(outside_skill, linked_dir, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"Directory symlinks are unavailable: {exc}")

        catalog = SkillCatalog(self.workspace, self.user)
        self.assertNotIn("workspace:linked-skill", catalog.skills)

    def test_secret_text_is_redacted_and_tool_output_is_bounded(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        write_skill(
            self.workspace_skills,
            "redacted-skill",
            description=f"Use with {secret} only.",
            body=f"Authorization: Bearer {secret}\n" + ("instruction " * 1_500),
        )
        runtime = SkillRuntime(SkillCatalog(self.workspace, self.user), enabled=True)

        listed = runtime.execute("list_skills", "{}")
        loaded = runtime.execute(
            "load_skill", '{"skill_id":"workspace:redacted-skill"}'
        )

        self.assertNotIn(secret, listed)
        self.assertNotIn(secret, loaded)
        self.assertIn("REDACTED", loaded)
        self.assertLessEqual(len(loaded), MAX_TOOL_OUTPUT_CHARS)


class SkillIntegrationTests(unittest.TestCase):
    def test_workspace_env_cannot_enable_or_redirect_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            attacker = Path(temp) / "attacker"
            workspace.mkdir()
            (workspace / ".env").write_text(
                f"TINYFORGE_API_KEY=file-key\n"
                f"TINYFORGE_SKILLS_ENABLED=true\n"
                f"TINYFORGE_SKILLS_DIR={attacker}\n",
                encoding="utf-8",
            )
            safe_user = Path(temp) / "safe-user"
            environment = {
                "TINYFORGE_API_KEY": "process-key",
                "TINYFORGE_SKILLS_DIR": str(safe_user),
            }
            with patch.dict(os.environ, environment, clear=True):
                config = Config.from_env(workspace)

            self.assertFalse(config.skills_enabled)
            self.assertEqual(config.user_skills_dir, safe_user.resolve())

    def test_runtime_exposes_skill_tools_only_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            user = Path(temp) / "user"
            workspace.mkdir()
            base = dict(
                api_key="key",
                base_url="https://example.test/v1",
                model="test-model",
                workspace=workspace,
                state_dir=Path(temp) / "state",
                user_skills_dir=user,
                memory_enabled=False,
            )
            disabled = build_agent(Config(**base, skills_enabled=False))
            enabled = build_agent(Config(**base, skills_enabled=True))

        disabled_names = {
            item["function"]["name"] for item in disabled.tools.definitions
        }
        enabled_names = {item["function"]["name"] for item in enabled.tools.definitions}
        self.assertNotIn("list_skills", disabled_names)
        self.assertTrue(
            {"list_skills", "load_skill", "read_skill_resource"} <= enabled_names
        )

    def test_skill_content_is_not_injected_into_system_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            unique = "UNTRUSTED_DESCRIPTION_123"
            write_skill(
                workspace / ".tinyforge" / "skills",
                "hidden-content",
                description=unique,
                body="UNTRUSTED_BODY_456",
            )
            runtime = SkillRuntime(SkillCatalog(workspace, Path(temp) / "user"), enabled=True)
            model = _FinalModel()
            agent = Agent(
                model=model,
                tools=runtime,
                workspace=workspace,
                skills=runtime,
                skills_enabled=True,
            )
            result = agent.run("Inspect the project")

        system_prompt = str(model.calls[0][0][0]["content"])
        self.assertTrue(result.success)
        self.assertNotIn(unique, system_prompt)
        self.assertNotIn("UNTRUSTED_BODY_456", system_prompt)
        self.assertIn("untrusted local guidance", system_prompt)

    def test_successful_load_emits_event_and_is_not_memory_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            write_skill(workspace / ".tinyforge" / "skills", "verify-change")
            runtime = SkillRuntime(SkillCatalog(workspace, Path(temp) / "user"), enabled=True)
            events: list[AgentEvent] = []
            agent = Agent(
                model=_LoadSkillModel(),
                tools=runtime,
                workspace=workspace,
                skills=runtime,
                skills_enabled=True,
                on_event=events.append,
            )
            result = agent.run("Use the relevant skill")

        loaded = [event for event in events if event.kind == "skill_loaded"]
        self.assertTrue(result.success)
        self.assertEqual(loaded[0].data["id"], "workspace:verify-change")

        memory = WorkingMemory()
        memory.start("Use a Skill")
        memory.record_tool(
            "load_skill",
            json.dumps({"ok": True, "result": {"skill": {"id": "x"}}}),
        )
        self.assertEqual(memory.evidence, {})

    def test_failed_agent_run_emits_read_only_skill_fault_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            write_skill(workspace / ".tinyforge" / "skills", "verify-change")
            runtime = SkillRuntime(
                SkillCatalog(workspace, Path(temp) / "user"), enabled=True
            )
            events: list[AgentEvent] = []
            agent = Agent(
                model=_SkillFailureModel(),
                tools=CompositeTools(runtime, _FailingToolProvider()),
                workspace=workspace,
                skills=runtime,
                skills_enabled=True,
                on_event=events.append,
            )

            result = agent.run("Use a Skill and run the focused check")

        reports = [event for event in events if event.kind == "skill_fault_report"]
        self.assertFalse(result.success)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].data["tool"], "fail_check")
        self.assertEqual(
            reports[0].data["active_skill_candidates"][0]["id"],
            "workspace:verify-change",
        )
        self.assertFalse(reports[0].data["skill_mutation_applied"])

    def test_same_batch_load_is_not_attributed_to_a_sibling_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            write_skill(workspace / ".tinyforge" / "skills", "verify-change")
            runtime = SkillRuntime(
                SkillCatalog(workspace, Path(temp) / "user"), enabled=True
            )
            events: list[AgentEvent] = []
            agent = Agent(
                model=_SameBatchSkillFailureModel(),
                tools=CompositeTools(runtime, _FailingToolProvider()),
                workspace=workspace,
                skills=runtime,
                skills_enabled=True,
                on_event=events.append,
            )

            result = agent.run("Load a Skill and run the check in one decision batch")

        reports = [event for event in events if event.kind == "skill_fault_report"]
        self.assertFalse(result.success)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].data["tool"], "fail_check")
        self.assertEqual(reports[0].data["active_skill_candidates"], [])

    def test_context_eviction_removes_skill_from_later_fault_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            write_skill(workspace / ".tinyforge" / "skills", "verify-change")
            runtime = SkillRuntime(
                SkillCatalog(workspace, Path(temp) / "user"), enabled=True
            )
            events: list[AgentEvent] = []
            model = _CompressedContextSkillFailureModel()
            agent = Agent(
                model=model,
                tools=CompositeTools(runtime, _FailingToolProvider()),
                workspace=workspace,
                max_context_chars=2_200,
                skills=runtime,
                skills_enabled=True,
                on_event=events.append,
            )

            result = agent.run("Load guidance, gather context, then run the check")

        reports = [event for event in events if event.kind == "skill_fault_report"]
        third_call_ids = {
            message.get("tool_call_id")
            for message in model.calls[2]
            if message.get("role") == "tool"
        }
        self.assertFalse(result.success)
        self.assertNotIn("compressed-load", third_call_ids)
        self.assertIn("context_compacted", [event.kind for event in events])
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].data["active_skill_candidates"], [])


if __name__ == "__main__":
    unittest.main()
