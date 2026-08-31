from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tinyforge.config import Config, ConfigError, load_env_file


class ConfigTests(unittest.TestCase):
    def test_load_env_file_does_not_override_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / ".env"
            env_file.write_text("ONE=file\nTWO='quoted value'\n", encoding="utf-8")
            with patch.dict(os.environ, {"ONE": "environment"}, clear=True):
                load_env_file(env_file)
                self.assertEqual(os.environ["ONE"], "environment")
                self.assertEqual(os.environ["TWO"], "quoted value")

    def test_missing_api_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {}, clear=True), patch(
                "tinyforge.config.read_env_file", return_value={}
            ):
                with self.assertRaises(ConfigError):
                    Config.from_env(temp)

    def test_workspace_env_files_do_not_pollute_each_other_or_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launch = root / "launcher"
            first = root / "first"
            second = root / "second"
            launch.mkdir()
            first.mkdir()
            second.mkdir()
            (first / ".env").write_text(
                "TINYFORGE_API_KEY=first-key\nTINYFORGE_MODEL=first-model\n",
                encoding="utf-8",
            )
            (second / ".env").write_text(
                "TINYFORGE_API_KEY=second-key\nTINYFORGE_MODEL=second-model\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True), patch(
                "tinyforge.config.Path.cwd", return_value=launch
            ):
                first_config = Config.from_env(first)
                second_config = Config.from_env(second)
                self.assertNotIn("TINYFORGE_API_KEY", os.environ)
                self.assertNotIn("TINYFORGE_MODEL", os.environ)

            self.assertEqual(first_config.api_key, "first-key")
            self.assertEqual(first_config.model, "first-model")
            self.assertEqual(second_config.api_key, "second-key")
            self.assertEqual(second_config.model, "second-model")

    def test_environment_and_overrides_are_combined(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            environment = {
                "TINYFORGE_API_KEY": "secret",
                "TINYFORGE_MODEL": "env-model",
                "TINYFORGE_MAX_ROUNDS": "12",
                "TINYFORGE_WIRE_API": "responses",
                "TINYFORGE_REASONING_EFFORT": "xhigh",
                "TINYFORGE_STORE_RESPONSES": "false",
                "TINYFORGE_STATE_DIR": temp,
                "TINYFORGE_MEMORY_ENABLED": "true",
                "TINYFORGE_ARCHIVE_SESSIONS": "false",
                "TINYFORGE_MAX_CONTEXT_TOKENS": "24000",
            }
            with patch.dict(os.environ, environment, clear=True):
                config = Config.from_env(temp, model="cli-model")
            self.assertEqual(config.api_key, "secret")
            self.assertEqual(config.model, "cli-model")
            self.assertEqual(config.max_rounds, 12)
            self.assertEqual(config.wire_api, "responses")
            self.assertEqual(config.reasoning_effort, "xhigh")
            self.assertFalse(config.store_responses)
            self.assertEqual(config.state_dir, Path(temp).resolve())
            self.assertTrue(config.memory_enabled)
            self.assertFalse(config.archive_sessions)
            self.assertEqual(config.max_context_tokens, 24_000)

    def test_invalid_wire_api_is_rejected(self) -> None:
        environment = {
            "TINYFORGE_API_KEY": "secret",
            "TINYFORGE_WIRE_API": "unknown",
        }
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(ConfigError):
                    Config.from_env(temp)


if __name__ == "__main__":
    unittest.main()
