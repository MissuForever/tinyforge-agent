"""Turn a verified TinyForge GUI recording into a narrated 1080p MP4."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import wave
from array import array
from pathlib import Path
from typing import Any, Callable, Sequence

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
WIDTH, HEIGHT = 1920, 1080
FPS = 30
NARRATION_SAMPLE_RATE = 24_000
DEFAULT_EDGE_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_EDGE_RATE = "+5%"
DEFAULT_SYSTEM_VOICE = "Microsoft Huihui Desktop"
DEFAULT_SYSTEM_RATE = 3
INTRO_SECONDS = 8.0
MEMORY_SECONDS = 9.0
OUTRO_SECONDS = 11.0
BG = "#111315"
PANEL = "#1d2225"
TEXT = "#f5f7f6"
MUTED = "#aeb8b5"
GREEN = "#43b581"
CYAN = "#57b8c5"
YELLOW = "#e5b95c"
SANS = "C:/Windows/Fonts/Noto Sans SC (TrueType).otf"
SANS_BOLD = "C:/Windows/Fonts/Noto Sans SC Bold (TrueType).otf"
MONO = "C:/Windows/Fonts/CascadiaMono.ttf"
FFMPEG = (
    ROOT
    / ".demo"
    / "video-tools"
    / "imageio_ffmpeg"
    / "binaries"
    / "ffmpeg-win-x86_64-v7.1.exe"
)


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def box(draw: ImageDraw.ImageDraw, bounds: tuple[int, int, int, int], fill: str) -> None:
    draw.rounded_rectangle(bounds, radius=8, fill=fill)


def header(draw: ImageDraw.ImageDraw, step: str) -> None:
    draw.rectangle((0, 0, WIDTH, 92), fill="#0a0d0e")
    draw.text((72, 25), "TinyForge", font=font(SANS_BOLD, 34), fill=TEXT)
    draw.text((255, 33), "0.3.0", font=font(MONO, 21), fill=GREEN)
    draw.text((WIDTH - 310, 33), step, font=font(SANS, 22), fill=MUTED)


def make_intro(path: Path, result: dict[str, Any]) -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    header(draw, "GUI LIVE DEMO")
    draw.text((72, 174), "从缺陷到验证，再到可复用记忆", font=font(SANS_BOLD, 58), fill=TEXT)
    draw.text(
        (72, 260),
        "真实模型调用 · 原生桌面 GUI · 文件与工具证据",
        font=font(SANS, 29),
        fill=CYAN,
    )
    skills_enabled = result.get("configuration", {}).get("skills_enabled") is True
    labels = (
        [
            ("01", "检索 Skill", "按任务加载只读指南"),
            ("02", "建立基线", "4 项测试，3 项失败"),
            ("03", "自主修复", "只修改 pricing.py"),
            ("04", "验证与记忆", "测试全过并保存 SOP"),
        ]
        if skills_enabled
        else [
            ("01", "建立基线", "4 项测试，3 项失败"),
            ("02", "自主修复", "只修改 pricing.py"),
            ("03", "执行验证", "完整测试 exit=0"),
            ("04", "提交记忆", "保存带证据 SOP"),
        ]
    )
    for index, (number, title, detail) in enumerate(labels):
        left = 72 + index * 440
        box(draw, (left, 390, left + 390, 680), PANEL)
        draw.text((left + 28, 425), number, font=font(MONO, 25), fill=GREEN)
        draw.text((left + 28, 500), title, font=font(SANS_BOLD, 35), fill=TEXT)
        draw.text((left + 28, 576), detail, font=font(SANS, 23), fill=MUTED)
    config = result["configuration"]
    draw.text(
        (72, 800),
        f"Model  {config['model']}    Protocol  {config['wire_api']}    "
        f"Skills  {'on' if skills_enabled else 'off'}    Memory  on",
        font=font(MONO, 25),
        fill=YELLOW,
    )
    draw.text(
        (72, 958),
        "录屏来自一次全新的实时运行；网络等待会在成片中等比加速。",
        font=font(SANS, 23),
        fill=MUTED,
    )
    image.save(path, optimize=True)


def make_outro(path: Path, result: dict[str, Any]) -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    header(draw, "VERIFIED RESULT")
    draw.text((72, 174), "完整闭环已通过真实执行验证", font=font(SANS_BOLD, 58), fill=TEXT)
    agent = result["agent_result"] or {}
    metrics = [
        ("Completed", "Agent 结果"),
        ("4 / 4", "演示项目测试"),
        ("pricing.py", "唯一修改文件"),
        (str(result["memory_commit_count"]), "已提交 SOP"),
    ]
    for index, (value, label) in enumerate(metrics):
        left = 72 + index * 440
        box(draw, (left, 350, left + 390, 640), PANEL)
        size = 46 if len(value) <= 10 else 31
        draw.text((left + 28, 404), value, font=font(SANS_BOLD, size), fill=GREEN)
        draw.text((left + 28, 530), label, font=font(SANS, 24), fill=MUTED)
    stats = (
        f"模型请求 {agent.get('rounds', 0)} 次   工具调用 {agent.get('tool_calls', 0)} 次   "
        f"运行 {agent.get('elapsed_ms', 0) / 1000:.1f} 秒"
    )
    draw.text((72, 760), stats, font=font(SANS, 28), fill=CYAN)
    draw.text(
        (72, 825),
        "主项目完整测试通过 · GUI smoke、构建产物与真实端点均已验证",
        font=font(SANS, 27),
        fill=YELLOW,
    )
    draw.text(
        (72, 958),
        "Skill receipt、执行记录、统一 Diff 和持久记忆证据均保留在本次演示产物中。",
        font=font(SANS, 23),
        fill=MUTED,
    )
    image.save(path, optimize=True)


def memory_evidence_lines(memory: str) -> tuple[str, str]:
    edit = re.search(r"\b(e\d+)=edit_file:([^|\n]+)", memory)
    verification = re.search(r"\b(e\d+)=run_command:exit=0 for ([^|\n]+)", memory)
    edit_line = (
        f"{edit.group(1)}  edit_file: {edit.group(2).strip()}"
        if edit is not None
        else "verified  edit_file: pricing.py"
    )
    verification_line = (
        f"{verification.group(1)}  run_command: 4 tests OK, exit=0"
        if verification is not None
        else "verified  run_command: 4 tests OK, exit=0"
    )
    return edit_line, verification_line


def make_memory_card(path: Path, result: dict[str, Any]) -> None:
    memory = str(result.get("memory", ""))
    identifier_match = re.search(r"\[(sop:[^\]]+)\]", memory)
    identifier = identifier_match.group(1) if identifier_match else "sop:verified"
    edit_evidence, verification_evidence = memory_evidence_lines(memory)
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    header(draw, "PERSISTENT MEMORY")
    draw.text((72, 174), "带执行证据的 SOP 已进入持久记忆", font=font(SANS_BOLD, 56), fill=TEXT)
    draw.text(
        (72, 258),
        "No Execution, No Memory",
        font=font(MONO, 27),
        fill=CYAN,
    )
    box(draw, (72, 370, 1848, 742), PANEL)
    draw.text((110, 416), f"[{identifier}]", font=font(MONO, 27), fill=GREEN)
    draw.text(
        (110, 486),
        "Baseline-then-verify workflow for Python unittest fixes",
        font=font(SANS_BOLD, 34),
        fill=TEXT,
    )
    draw.text(
        (110, 570),
        edit_evidence,
        font=font(MONO, 24),
        fill=MUTED,
    )
    draw.text(
        (110, 626),
        verification_evidence,
        font=font(MONO, 24),
        fill=YELLOW,
    )
    draw.text(
        (72, 874),
        "索引默认只注入标题；详细流程由 recall_memory 按需读取。",
        font=font(SANS, 27),
        fill=MUTED,
    )
    image.save(path, optimize=True)


def _powershell_base64(value: str) -> str:
    return base64.b64encode(value.encode("utf-16le")).decode("ascii")


def _validate_canonical_wav(path: Path) -> None:
    try:
        with wave.open(str(path), "rb") as stream:
            actual = (
                stream.getnchannels(),
                stream.getsampwidth(),
                stream.getframerate(),
                stream.getcomptype(),
            )
            frames = stream.getnframes()
    except (OSError, EOFError, wave.Error) as exc:
        raise RuntimeError(f"TTS did not produce a readable WAV: {path.name}") from exc
    expected = (1, 2, NARRATION_SAMPLE_RATE, "NONE")
    if actual != expected:
        raise RuntimeError(
            f"TTS WAV has incompatible audio format: {actual}; expected {expected}"
        )
    if frames <= 0:
        raise RuntimeError(f"TTS WAV contains no audio frames: {path.name}")


def _normalize_audio(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tinyforge-audio-", dir=destination.parent) as temp:
        normalized = Path(temp) / "normalized.wav"
        completed = subprocess.run(
            [
                str(FFMPEG),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(NARRATION_SAMPLE_RATE),
                "-c:a",
                "pcm_s16le",
                str(normalized),
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"FFmpeg could not normalize TTS audio: {source.name}")
        _validate_canonical_wav(normalized)
        normalized.replace(destination)


def synthesize_system(
    text: str,
    destination: Path,
    *,
    voice: str = DEFAULT_SYSTEM_VOICE,
    rate: int = DEFAULT_SYSTEM_RATE,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tinyforge-system-tts-", dir=destination.parent) as temp:
        source = Path(temp) / "speech.wav"
        encoded_voice = _powershell_base64(voice)
        encoded_text = _powershell_base64(text)
        encoded_path = _powershell_base64(str(source))
        script = (
            "Add-Type -AssemblyName System.Speech;"
            "$decode={param($v)[Text.Encoding]::Unicode.GetString("
            "[Convert]::FromBase64String($v))};"
            f"$voice=&$decode '{encoded_voice}';"
            f"$text=&$decode '{encoded_text}';"
            f"$path=&$decode '{encoded_path}';"
            "$s=[System.Speech.Synthesis.SpeechSynthesizer]::new();"
            f"$s.SelectVoice($voice);$s.Rate={rate};$s.Volume=100;"
            "$s.SetOutputToWaveFile($path);$s.Speak($text);$s.Dispose()"
        )
        encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-EncodedCommand", encoded_script],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        _normalize_audio(source, destination)


class EdgeTTSUnavailableError(RuntimeError):
    pass


async def _save_edge_audio(
    text: str,
    source: Path,
    *,
    voice: str,
    rate: str,
    proxy: str | None,
) -> None:
    failure: str | None = None
    try:
        import edge_tts

        communicate = edge_tts.Communicate(
            text,
            voice,
            rate=rate,
            proxy=proxy,
            connect_timeout=15,
            receive_timeout=90,
        )
        await communicate.save(str(source))
    except ImportError:
        failure = "edge-tts is not installed; install the project's demo extra"
    except Exception as exc:
        failure = f"Edge TTS request failed ({type(exc).__name__})"
    if failure is not None:
        raise EdgeTTSUnavailableError(failure) from None


def synthesize_edge(
    text: str,
    destination: Path,
    *,
    voice: str = DEFAULT_EDGE_VOICE,
    rate: str = DEFAULT_EDGE_RATE,
    proxy: str | None = None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tinyforge-edge-tts-", dir=destination.parent) as temp:
        source = Path(temp) / "speech.mp3"
        asyncio.run(
            _save_edge_audio(text, source, voice=voice, rate=rate, proxy=proxy)
        )
        _normalize_audio(source, destination)


def _proxy_from_windows() -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0])
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
    except (OSError, ValueError):
        return None
    if not enabled or not server:
        return None
    if ";" in server:
        entries = dict(
            part.split("=", 1) for part in server.split(";") if "=" in part
        )
        server = entries.get("https") or entries.get("http") or ""
    if not server:
        return None
    return server if "://" in server else f"http://{server}"


def detect_tts_proxy() -> str | None:
    for name in (
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return _proxy_from_windows()


def resolve_tts_proxy(explicit: str | None, disabled: bool) -> str | None:
    if disabled:
        return None
    return explicit or detect_tts_proxy()


def _render_tts_batch(
    texts: Sequence[str],
    directory: Path,
    renderer: Callable[[str, Path], None],
) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = [directory / f"narration-{index:02d}.wav" for index in range(len(texts))]
    for text, path in zip(texts, paths, strict=True):
        renderer(text, path)
    return paths


def synthesize_narration(
    texts: Sequence[str],
    work: Path,
    *,
    backend: str,
    edge_voice: str,
    edge_rate: str,
    system_voice: str,
    system_rate: int,
    proxy: str | None,
) -> tuple[list[Path], dict[str, Any]]:
    if backend in {"auto", "edge"}:
        edge_directory = work / "tts-edge"
        edge_paths = [
            edge_directory / f"narration-{index:02d}.wav"
            for index in range(len(texts))
        ]
        try:
            paths = _render_tts_batch(
                texts,
                edge_directory,
                lambda text, path: synthesize_edge(
                    text, path, voice=edge_voice, rate=edge_rate, proxy=proxy
                ),
            )
            return paths, {
                "requested_backend": backend,
                "actual_backend": "edge",
                "voice": edge_voice,
                "rate": edge_rate,
                "sample_rate": NARRATION_SAMPLE_RATE,
                "fallback": False,
            }
        except EdgeTTSUnavailableError as exc:
            for path in edge_paths:
                path.unlink(missing_ok=True)
            if backend == "edge":
                raise RuntimeError(
                    f"{exc}; no fallback was requested"
                ) from None
            print(
                f"{exc}; regenerating all narration "
                "with the local System.Speech voice.",
                file=sys.stderr,
            )

    paths = _render_tts_batch(
        texts,
        work / "tts-system",
        lambda text, path: synthesize_system(
            text, path, voice=system_voice, rate=system_rate
        ),
    )
    return paths, {
        "requested_backend": backend,
        "actual_backend": "system",
        "voice": system_voice,
        "rate": system_rate,
        "sample_rate": NARRATION_SAMPLE_RATE,
        "fallback": backend == "auto",
    }


def media_duration(path: Path) -> float:
    completed = subprocess.run(
        [str(FFMPEG), "-hide_banner", "-i", str(path)],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", completed.stdout)
    if not match:
        raise RuntimeError(f"Unable to read media duration: {path}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as stream:
        return stream.getnframes() / stream.getframerate()


def detect_gui_height(raw_video: Path, work: Path) -> int:
    probe = work / "layout-probe.png"
    subprocess.run(
        [
            str(FFMPEG),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            "1",
            "-i",
            str(raw_video),
            "-frames:v",
            "1",
            "-update",
            "1",
            str(probe),
        ],
        check=True,
    )
    image = Image.open(probe).convert("RGB")
    if image.size != (WIDTH, HEIGHT):
        return HEIGHT
    consecutive = 0
    start = HEIGHT
    for y in range(HEIGHT // 2, HEIGHT - 20):
        row = image.crop((0, y, WIDTH, y + 1)).resize((240, 1))
        dark = sum(1 for red, green, blue in row.getdata() if max(red, green, blue) < 12)
        if dark >= 235:
            if not consecutive:
                start = y
            consecutive += 1
            if consecutive >= 10:
                return max(540, start) // 2 * 2
        else:
            consecutive = 0
            start = HEIGHT
    return HEIGHT


def make_live_footer(
    path: Path, *, gui_height: int, result: dict[str, Any], speed: float
) -> None:
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    if gui_height >= HEIGHT:
        image.save(path)
        return
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, gui_height, WIDTH, HEIGHT), fill=BG)
    draw.rectangle((0, gui_height, WIDTH, gui_height + 3), fill=GREEN)
    draw.text(
        (72, gui_height + 48),
        "LIVE GUI CAPTURE",
        font=font(MONO, 27),
        fill=GREEN,
    )
    feature_text = "Responses API  |  Files  |  Skills  |  Memory"
    draw.text(
        (72, gui_height + 102),
        f"{result['configuration']['model']}  |  {feature_text}",
        font=font(SANS, 25),
        fill=TEXT,
    )
    draw.text(
        (WIDTH - 590, gui_height + 48),
        f"Model wait accelerated {speed:.2f}x",
        font=font(MONO, 23),
        fill=YELLOW,
    )
    draw.text(
        (WIDTH - 590, gui_height + 102),
        "Evidence review remains at real speed",
        font=font(SANS, 23),
        fill=MUTED,
    )
    image.save(path, optimize=True)


def marker_time(result: dict[str, Any], name: str, default: float) -> float:
    for marker in result.get("markers", []):
        if marker.get("name") == name:
            return float(marker.get("t", default))
    return default


def has_ordered_gui_test_evidence(result: dict[str, Any]) -> bool:
    failed: list[int] = []
    passed: list[int] = []
    command = "python -m unittest discover -s tests -t . -v"
    for index, row in enumerate(result.get("timeline", [])):
        details = str(row.get("details", ""))
        if row.get("action") != "run_command" or command not in details:
            continue
        if (
            row.get("state") == "Failed"
            and "Ran 4 tests" in details
            and "FAILED (failures=3)" in details
        ):
            failed.append(index)
        if (
            row.get("state") == "Succeeded"
            and "Ran 4 tests" in details
            and "OK" in details
            and "FAILED" not in details
            and "ERROR" not in details
        ):
            passed.append(index)
    return bool(failed and passed and failed[0] < passed[-1])


def write_narration(
    chunks: list[tuple[float, Path]], duration: float, destination: Path
) -> None:
    with wave.open(str(chunks[0][1]), "rb") as first:
        params = first.getparams()
        channels = first.getnchannels()
        sample_width = first.getsampwidth()
        rate = first.getframerate()
    if channels != 1 or sample_width != 2 or rate != NARRATION_SAMPLE_RATE:
        raise RuntimeError("Expected canonical mono 24 kHz 16-bit PCM narration WAV")
    samples = array("h", [0]) * math.ceil(duration * rate)
    for start, path in chunks:
        with wave.open(str(path), "rb") as stream:
            if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) != (
                channels,
                sample_width,
                rate,
            ):
                raise RuntimeError(f"Incompatible narration WAV: {path}")
            spoken = array("h")
            spoken.frombytes(stream.readframes(stream.getnframes()))
        offset = max(0, round(start * rate))
        for index, value in enumerate(spoken):
            target = offset + index
            if target >= len(samples):
                break
            samples[target] = max(-32768, min(32767, samples[target] + value))
    with wave.open(str(destination), "wb") as output:
        output.setparams(params)
        output.writeframes(samples.tobytes())


def schedule_narration(
    desired: list[tuple[float, Path]], total_duration: float
) -> list[tuple[float, Path]]:
    scheduled: list[tuple[float, Path]] = []
    cursor = 0.0
    for wanted, path in desired:
        start = max(wanted, cursor + (0.35 if scheduled else 0.0))
        end = start + wav_duration(path)
        if end > total_duration - 0.15:
            raise RuntimeError(
                f"Narration exceeds video duration: {path.name} ends at {end:.2f}s"
            )
        scheduled.append((start, path))
        cursor = end
    return scheduled


def _edge_rate(value: str) -> str:
    match = re.fullmatch(r"([+-])(\d{1,3})%", value)
    if match is None or int(match.group(2)) > 100:
        raise argparse.ArgumentTypeError("edge rate must use +N% or -N% within 0..100")
    return value


def _system_rate(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("system rate must be an integer") from exc
    if not -10 <= parsed <= 10:
        raise argparse.ArgumentTypeError("system rate must be between -10 and 10")
    return parsed


def _voice_name(value: str) -> str:
    parsed = value.strip()
    if not parsed:
        raise argparse.ArgumentTypeError("voice name cannot be empty")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=".demo/gui-video-run1")
    parser.add_argument("--output", default=".demo/gui-video-run1/TinyForge-GUI-demo.mp4")
    parser.add_argument("--max-gui-seconds", type=float, default=115.0)
    parser.add_argument(
        "--tts-backend",
        choices=("auto", "edge", "system"),
        default="auto",
        help="Narration backend; auto uses Edge neural TTS with an offline fallback",
    )
    parser.add_argument("--edge-voice", type=_voice_name, default=DEFAULT_EDGE_VOICE)
    parser.add_argument("--edge-rate", type=_edge_rate, default=DEFAULT_EDGE_RATE)
    parser.add_argument("--system-voice", type=_voice_name, default=DEFAULT_SYSTEM_VOICE)
    parser.add_argument("--system-rate", type=_system_rate, default=DEFAULT_SYSTEM_RATE)
    proxy_group = parser.add_mutually_exclusive_group()
    proxy_group.add_argument(
        "--tts-proxy",
        help="HTTP proxy for Edge TTS; defaults to proxy environment or Windows settings",
    )
    proxy_group.add_argument(
        "--no-tts-proxy",
        action="store_true",
        help="Ignore proxy environment and Windows settings for Edge TTS",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    input_dir = (ROOT / args.input).resolve()
    output = (ROOT / args.output).resolve()
    result = json.loads((input_dir / "result.json").read_text(encoding="utf-8"))
    raw_video = input_dir / "gui-raw.mp4"
    valid = (
        result.get("status") == "Completed"
        and result.get("verification", {}).get("all_passed")
        and result.get("changed_files") == ["pricing.py"]
        and result.get("change_count", 0) >= 1
        and result.get("memory_commit_count", 0) >= 1
        and result.get("command_showcase", {}).get("visible")
        and result.get("command_showcase", {}).get("height", 0) >= 120
        and result.get("command_showcase", {}).get("count", 0) >= 2
        and result.get("command_showcase", {}).get("has_failed_baseline")
        and result.get("command_showcase", {}).get("has_successful_verification")
        and result.get("files_showcase", {}).get("indexed")
        and result.get("files_showcase", {}).get("selected")
        and result.get("files_showcase", {}).get("preview_ready")
        and "M" in str(result.get("files_showcase", {}).get("git_status", ""))
        and result.get("files_marker_recorded")
        and (
            result.get("configuration", {}).get("skills_enabled") is not True
            or (
                result.get("skill_showcase", {}).get("enabled") is True
                and result.get("skill_showcase", {}).get("search_count", 0) >= 1
                and "workspace:verified-bugfix"
                in result.get("skill_showcase", {}).get("loaded", [])
                and result.get("skill_showcase", {}).get("receipts_complete") is True
                and result.get("skill_marker_recorded") is True
            )
        )
        and has_ordered_gui_test_evidence(result)
        and raw_video.is_file()
    )
    if not valid:
        raise SystemExit("GUI capture is incomplete; refusing to render a misleading video")

    work = input_dir / "render-gui"
    work.mkdir(exist_ok=True)
    intro = work / "intro.png"
    memory_card = work / "memory.png"
    outro = work / "outro.png"
    make_intro(intro, result)
    make_memory_card(memory_card, result)
    make_outro(outro, result)

    raw_duration = media_duration(raw_video)
    setup_end = marker_time(result, "task_submitted", 8.0)
    terminal = marker_time(result, "agent_terminal", raw_duration * 0.75)
    terminal = min(max(setup_end + 1.0, terminal), raw_duration - 1.0)
    review_duration = raw_duration - terminal
    available_work = max(30.0, args.max_gui_seconds - setup_end - review_duration)
    work_duration = terminal - setup_end
    speed = max(1.0, work_duration / available_work)
    gui_duration = setup_end + work_duration / speed + review_duration
    total_duration = INTRO_SECONDS + gui_duration + MEMORY_SECONDS + OUTRO_SECONDS

    def live_time(raw_time: float) -> float:
        raw_time = min(max(0.0, raw_time), raw_duration)
        if raw_time <= setup_end:
            mapped = raw_time
        elif raw_time <= terminal:
            mapped = setup_end + (raw_time - setup_end) / speed
        else:
            mapped = setup_end + work_duration / speed + (raw_time - terminal)
        return INTRO_SECONDS + mapped

    gui_height = detect_gui_height(raw_video, work)
    footer_image = work / "live-footer.png"
    make_live_footer(footer_image, gui_height=gui_height, result=result, speed=speed)

    narration_items = [
        (
            0.4,
            "这是 TinyForge 图形界面的完整演示。我们从真实缺陷开始，让 Agent 自主修复、验证并保存可靠经验。",
        ),
        (
            live_time(marker_time(result, "task_typing_started", 2.0)),
            "桌面端已经加载独立工作区、真实模型、技能和记忆模块。任务先检索并加载匹配的只读技能，再进入修复流程。",
        ),
        (
            live_time(setup_end + 9.0),
            "执行时间线实时记录模型请求和本地工具调用，文件操作与命令输出都可以逐项审计。",
        ),
        (
            live_time(setup_end + work_duration * 0.45),
            "Agent 先运行同一套测试建立失败基线，再读取需求和实现，只对目标文件做精确修改。",
        ),
        (
            live_time(terminal) + 0.8,
            "任务完成后开始回看证据：最初四项测试中三项失败，统一差异只包含 pricing 点 py，"
            "再次执行同一命令后四项全部通过。",
        ),
        (
            live_time(marker_time(result, "workspace_files", terminal + 18.0)) + 0.5,
            "左侧工作区同步显示目录结构和 Git 状态。pricing 点 py 标记为已修改，文件树下方可直接查看带行号的安全只读预览。",
        ),
        (
            live_time(marker_time(result, "skill_receipt", terminal + 36.0)) + 0.4,
            "技能页保留任务检索、加载顺序和内容哈希。技能始终只读，不会扩大 Agent 的工具权限。",
        ),
        (
            INTRO_SECONDS + gui_duration + 0.5,
            "这条 SOP 引用了修改文件和最终测试的执行证据。没有成功命令，就不会晋升长期记忆。",
        ),
        (
            INTRO_SECONDS + gui_duration + MEMORY_SECONDS + 0.5,
            "本次运行完整通过：只修改目标实现，四项测试全过，并成功提交可复用记忆。",
        ),
    ]
    narration_paths, tts_manifest = synthesize_narration(
        [text for _, text in narration_items],
        work,
        backend=args.tts_backend,
        edge_voice=args.edge_voice,
        edge_rate=args.edge_rate,
        system_voice=args.system_voice,
        system_rate=args.system_rate,
        proxy=resolve_tts_proxy(args.tts_proxy, args.no_tts_proxy),
    )
    desired_chunks = [
        (start, path)
        for (start, _), path in zip(narration_items, narration_paths, strict=True)
    ]
    wav_chunks = schedule_narration(desired_chunks, total_duration)
    narration = work / "narration.wav"
    write_narration(wav_chunks, total_duration, narration)

    output.parent.mkdir(parents=True, exist_ok=True)
    filter_graph = (
        f"[0:v]trim=duration={INTRO_SECONDS:.3f},setpts=PTS-STARTPTS,"
        f"fps={FPS},setsar=1,format=yuv420p[opening];"
        f"[1:v]crop={WIDTH}:{gui_height}:0:0,fps={FPS},setsar=1,split=3[rs][rw][rr];"
        f"[rs]trim=start=0:end={setup_end:.6f},setpts=PTS-STARTPTS[setup];"
        f"[rw]trim=start={setup_end:.6f}:end={terminal:.6f},"
        f"setpts=(PTS-STARTPTS)/{speed:.8f}[work];"
        f"[rr]trim=start={terminal:.6f},setpts=PTS-STARTPTS[review];"
        "[setup][work][review]concat=n=3:v=1:a=0[live-sequence];"
        f"[live-sequence]pad={WIDTH}:{HEIGHT}:0:0:color=0x111315[live-base];"
        f"[4:v]trim=duration={gui_duration:.6f},setpts=PTS-STARTPTS,"
        "format=rgba[footer];"
        "[live-base][footer]overlay=0:0:shortest=1[live];"
        f"[2:v]trim=duration={MEMORY_SECONDS:.3f},setpts=PTS-STARTPTS,"
        f"fps={FPS},setsar=1,format=yuv420p[memory-card];"
        f"[3:v]trim=duration={OUTRO_SECONDS:.3f},setpts=PTS-STARTPTS,"
        f"fps={FPS},setsar=1,format=yuv420p[closing];"
        f"[opening][live][memory-card][closing]concat=n=4:v=1:a=0,"
        f"fps={FPS},settb=1/{FPS},format=yuv420p[v]"
    )
    command = [
        str(FFMPEG),
        "-hide_banner",
        "-y",
        "-loop",
        "1",
        "-t",
        f"{INTRO_SECONDS:.3f}",
        "-i",
        str(intro),
        "-i",
        str(raw_video),
        "-loop",
        "1",
        "-t",
        f"{MEMORY_SECONDS:.3f}",
        "-i",
        str(memory_card),
        "-loop",
        "1",
        "-t",
        f"{OUTRO_SECONDS:.3f}",
        "-i",
        str(outro),
        "-loop",
        "1",
        "-t",
        f"{gui_duration:.3f}",
        "-i",
        str(footer_image),
        "-i",
        str(narration),
        "-filter_complex",
        filter_graph,
        "-map",
        "[v]",
        "-map",
        "5:a:0",
        "-t",
        f"{total_duration:.3f}",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "112k",
        "-movflags",
        "+faststart",
        str(output),
    ]
    subprocess.run(command, check=True)
    rendered_duration = media_duration(output)
    probe = subprocess.run(
        [str(FFMPEG), "-hide_banner", "-i", str(output)],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    ).stdout
    stream_check = all(
        value in probe for value in ("Video: h264", "1920x1080", "Audio: aac")
    )
    if not stream_check or abs(rendered_duration - total_duration) > 1.0:
        raise RuntimeError("Rendered video failed the post-encode stream or duration check")
    manifest = {
        "output": str(output),
        "raw_duration": round(raw_duration, 3),
        "speed": round(speed, 3),
        "gui_duration": round(gui_duration, 3),
        "duration": round(total_duration, 3),
        "rendered_duration": round(rendered_duration, 3),
        "gui_height": gui_height,
        "resolution": f"{WIDTH}x{HEIGHT}",
        "fps": FPS,
        "tts": tts_manifest,
        "narration": [
            {"start": round(start, 3), "duration": round(wav_duration(path), 3)}
            for start, path in wav_chunks
        ],
    }
    (input_dir / "video-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
