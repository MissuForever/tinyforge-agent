from __future__ import annotations

import asyncio
import base64
import io
import subprocess
import sys
import tempfile
import traceback
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

try:
    from scripts import render_gui_demo_video as render
except ModuleNotFoundError as exc:
    if exc.name != "PIL":
        raise
    render = None  # type: ignore[assignment]


def write_wav(path: Path, *, rate: int = 24_000, frames: int = 240) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"\x00\x00" * frames)


@unittest.skipIf(render is None, "GUI demo rendering dependencies are not installed")
class RenderGuiDemoVideoTests(unittest.TestCase):
    def settings(self, backend: str) -> dict[str, object]:
        assert render is not None
        return {
            "backend": backend,
            "edge_voice": render.DEFAULT_EDGE_VOICE,
            "edge_rate": render.DEFAULT_EDGE_RATE,
            "system_voice": render.DEFAULT_SYSTEM_VOICE,
            "system_rate": render.DEFAULT_SYSTEM_RATE,
            "proxy": None,
        }

    def test_parser_exposes_validated_tts_controls(self) -> None:
        assert render is not None
        parser = render.build_parser()
        defaults = parser.parse_args([])
        self.assertEqual(defaults.tts_backend, "auto")
        self.assertEqual(defaults.edge_voice, "zh-CN-XiaoxiaoNeural")
        self.assertEqual(defaults.edge_rate, "+5%")
        self.assertEqual(defaults.system_rate, 3)
        self.assertFalse(defaults.no_tts_proxy)

        selected = parser.parse_args(
            [
                "--tts-backend",
                "system",
                "--edge-rate=-10%",
                "--system-rate",
                "0",
            ]
        )
        self.assertEqual(selected.tts_backend, "system")
        self.assertEqual(selected.edge_rate, "-10%")
        self.assertEqual(selected.system_rate, 0)
        with patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["--edge-rate", "fast"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["--system-rate", "11"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["--edge-voice", " "])
            with self.assertRaises(SystemExit):
                parser.parse_args(["--tts-proxy", "http://proxy", "--no-tts-proxy"])

    def test_memory_card_uses_evidence_ids_from_the_recorded_run(self) -> None:
        assert render is not None
        memory = (
            "verified_evidence: e11=edit_file:pricing.py (replacements=1) | "
            "e12=read_file:pricing.py | "
            "e13=run_command:exit=0 for python -m unittest discover"
        )
        self.assertEqual(
            render.memory_evidence_lines(memory),
            (
                "e11  edit_file: pricing.py (replacements=1)",
                "e13  run_command: 4 tests OK, exit=0",
            ),
        )

    def test_auto_uses_edge_for_the_complete_batch(self) -> None:
        assert render is not None
        with tempfile.TemporaryDirectory() as temp:
            edge = Mock(side_effect=lambda _text, path, **_kwargs: write_wav(path))
            system = Mock()
            with patch.object(render, "synthesize_edge", edge), patch.object(
                render, "synthesize_system", system
            ):
                paths, manifest = render.synthesize_narration(
                    ["one", "two", "three"], Path(temp), **self.settings("auto")
                )

            self.assertEqual(edge.call_count, 3)
            system.assert_not_called()
            self.assertTrue(all(path.is_file() for path in paths))
            self.assertEqual(manifest["actual_backend"], "edge")
            self.assertFalse(manifest["fallback"])

    def test_auto_regenerates_every_chunk_after_edge_failure(self) -> None:
        assert render is not None
        attempts = 0

        def edge_side_effect(_text: str, path: Path, **_kwargs: object) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 3:
                raise render.EdgeTTSUnavailableError("network unavailable")
            write_wav(path)

        system = Mock(side_effect=lambda _text, path, **_kwargs: write_wav(path))
        with tempfile.TemporaryDirectory() as temp, patch.object(
            render, "synthesize_edge", side_effect=edge_side_effect
        ), patch.object(render, "synthesize_system", system), patch(
            "sys.stderr", io.StringIO()
        ):
            work = Path(temp)
            paths, manifest = render.synthesize_narration(
                ["one", "two", "three"], work, **self.settings("auto")
            )

            self.assertEqual(system.call_count, 3)
            self.assertTrue(all(path.parent.name == "tts-system" for path in paths))
            self.assertFalse(any((work / "tts-edge").glob("narration-*.wav")))
            self.assertEqual(manifest["actual_backend"], "system")
            self.assertTrue(manifest["fallback"])

    def test_explicit_edge_failure_does_not_fall_back(self) -> None:
        assert render is not None
        system = Mock()
        with tempfile.TemporaryDirectory() as temp, patch.object(
            render,
            "synthesize_edge",
            side_effect=render.EdgeTTSUnavailableError("network unavailable"),
        ), patch.object(render, "synthesize_system", system):
            with self.assertRaisesRegex(RuntimeError, "no fallback was requested") as caught:
                render.synthesize_narration(
                    ["one"], Path(temp), **self.settings("edge")
                )
        self.assertIsNone(caught.exception.__cause__)
        system.assert_not_called()

    def test_edge_error_does_not_expose_proxy_credentials(self) -> None:
        assert render is not None
        secret = "proxy-password-should-not-leak"

        class FailingCommunicate:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            async def save(self, _path: str) -> None:
                raise OSError(f"proxy http://user:{secret}@proxy.invalid failed")

        fake_module = types.SimpleNamespace(Communicate=FailingCommunicate)
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            sys.modules, {"edge_tts": fake_module}
        ):
            with self.assertRaises(render.EdgeTTSUnavailableError) as caught:
                asyncio.run(
                    render._save_edge_audio(
                        "text",
                        Path(temp) / "speech.mp3",
                        voice=render.DEFAULT_EDGE_VOICE,
                        rate=render.DEFAULT_EDGE_RATE,
                        proxy=f"http://user:{secret}@proxy.invalid",
                    )
                )

        formatted = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn(secret, formatted)
        self.assertIsNone(caught.exception.__cause__)

    def test_proxy_detection_and_explicit_disable(self) -> None:
        assert render is not None
        with patch.dict(render.os.environ, {"HTTP_PROXY": "http://http-proxy"}, clear=True), patch.object(
            render, "_proxy_from_windows", return_value="http://windows-proxy"
        ):
            self.assertEqual(render.detect_tts_proxy(), "http://http-proxy")
            self.assertEqual(
                render.resolve_tts_proxy("http://explicit-proxy", False),
                "http://explicit-proxy",
            )
            self.assertIsNone(render.resolve_tts_proxy(None, True))

    def test_explicit_system_backend_never_calls_edge(self) -> None:
        assert render is not None
        edge = Mock()
        system = Mock(side_effect=lambda _text, path, **_kwargs: write_wav(path))
        with tempfile.TemporaryDirectory() as temp, patch.object(
            render, "synthesize_edge", edge
        ), patch.object(render, "synthesize_system", system):
            paths, manifest = render.synthesize_narration(
                ["one", "two"], Path(temp), **self.settings("system")
            )
            self.assertTrue(all(path.is_file() for path in paths))
        edge.assert_not_called()
        self.assertEqual(system.call_count, 2)
        self.assertEqual(manifest["actual_backend"], "system")

    def test_normalize_audio_uses_the_canonical_wav_contract(self) -> None:
        assert render is not None

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            write_wav(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, "")

        with tempfile.TemporaryDirectory() as temp, patch.object(
            render.subprocess, "run", side_effect=fake_run
        ) as run:
            source = Path(temp) / "source.mp3"
            destination = Path(temp) / "final.wav"
            source.write_bytes(b"audio")
            render._normalize_audio(source, destination)
            render._validate_canonical_wav(destination)

        command = run.call_args.args[0]
        self.assertIn("-ac", command)
        self.assertEqual(command[command.index("-ac") + 1], "1")
        self.assertEqual(command[command.index("-ar") + 1], "24000")
        self.assertEqual(command[command.index("-c:a") + 1], "pcm_s16le")

    def test_mixer_rejects_noncanonical_wav(self) -> None:
        assert render is not None
        with tempfile.TemporaryDirectory() as temp:
            good = Path(temp) / "good.wav"
            bad = Path(temp) / "bad.wav"
            output = Path(temp) / "mixed.wav"
            write_wav(good)
            write_wav(bad, rate=22_050)
            render._validate_canonical_wav(good)
            with self.assertRaisesRegex(RuntimeError, "incompatible audio format"):
                render._validate_canonical_wav(bad)
            with self.assertRaisesRegex(RuntimeError, "Incompatible narration WAV"):
                render.write_narration([(0.0, good), (0.1, bad)], 1.0, output)

    def test_canonical_wav_rejects_zero_frames(self) -> None:
        assert render is not None
        with tempfile.TemporaryDirectory() as temp:
            empty = Path(temp) / "empty.wav"
            write_wav(empty, frames=0)
            with self.assertRaisesRegex(RuntimeError, "no audio frames"):
                render._validate_canonical_wav(empty)

    def test_powershell_payload_round_trips_special_text(self) -> None:
        assert render is not None
        text = "中文路径\\含 空格\\旁白's `$value"
        encoded = render._powershell_base64(text)
        self.assertEqual(base64.b64decode(encoded).decode("utf-16le"), text)


if __name__ == "__main__":
    unittest.main()
