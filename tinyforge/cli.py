"""Command-line interface for TinyForge."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .agent import Agent, AgentEvent
from .config import Config, ConfigError
from .memory import MemoryRuntime, MemoryStore, redact_secrets
from .model import ModelError, OpenAICompatibleClient
from .tools import CompositeTools, WorkspaceTools


class Console:
    COLORS = {
        "cyan": "\033[36m",
        "green": "\033[32m",
        "yellow": "\033[33m",
        "red": "\033[31m",
        "dim": "\033[2m",
        "reset": "\033[0m",
    }

    def __init__(self, color: bool = True, verbose: bool = True) -> None:
        self.color = color and sys.stdout.isatty() and os.getenv("NO_COLOR") is None
        self.verbose = verbose

    def paint(self, text: str, color: str) -> str:
        if not self.color:
            return text
        return f"{self.COLORS[color]}{text}{self.COLORS['reset']}"

    def event(self, event: AgentEvent) -> None:
        if event.kind == "model_start":
            print(self.paint(f"\n[model] round {event.data['round']}", "cyan"))
        elif event.kind == "assistant_text" and self.verbose:
            text = str(event.data["text"]).strip()
            if text:
                print(self.paint(f"[note] {text}", "dim"))
        elif event.kind == "tool_start":
            arguments = json.dumps(event.data["arguments"], ensure_ascii=False)
            if len(arguments) > 500:
                arguments = arguments[:500] + "..."
            print(self.paint(f"[tool] {event.data['name']}", "yellow"), arguments)
        elif event.kind == "tool_end":
            ok, summary = self._tool_summary(str(event.data["output"]))
            label = "ok" if ok else "error"
            color = "green" if ok else "red"
            print(self.paint(f"[{label}]", color), summary)
        elif event.kind == "context_compacted":
            print(self.paint(f"[context] removed {event.data['removed']} old messages", "dim"))
        elif event.kind == "loop_stopped":
            print(self.paint(f"[stopped] {event.data['reason']}", "red"))
        elif event.kind == "memory_committed":
            print(
                self.paint(
                    f"[memory] committed {event.data['count']} verified entries", "green"
                )
            )
        elif event.kind == "memory_error":
            print(self.paint(f"[memory error] {event.data['error']}", "red"))

    @staticmethod
    def _tool_summary(output: str) -> tuple[bool, str]:
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            return False, redact_secrets(output[:300])
        if not payload.get("ok"):
            return False, redact_secrets(
                str(payload.get("error", "unknown tool error"))[:500]
            )
        result = payload.get("result", {})
        if isinstance(result, dict):
            if "exit_code" in result:
                stdout = redact_secrets(
                    str(result.get("stdout", "")).strip().replace("\n", " | ")
                )
                return result["exit_code"] == 0, (
                    f"exit={result['exit_code']}" + (f"; {stdout[:350]}" if stdout else "")
                )
            if "path" in result:
                return True, redact_secrets(str(result["path"]))
            if "matches" in result:
                return True, f"{len(result['matches'])} matches"
        return True, "completed"

    def result_stats(self, result: Any) -> None:
        token_text = f"tokens={result.input_tokens}+{result.output_tokens}"
        if result.cached_input_tokens:
            token_text += f" cached={result.cached_input_tokens}"
        print(
            self.paint(
                f"[stats] requests={result.rounds} tools={result.tool_calls} "
                f"{token_text} elapsed={result.elapsed_ms / 1000:.1f}s",
                "dim",
            )
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tinyforge",
        description="A small coding agent with local file and shell tools.",
    )
    parser.add_argument("task", nargs="*", help="Task text. Omit for interactive mode.")
    parser.add_argument("-w", "--workspace", default=".", help="Workspace directory")
    parser.add_argument("--model", help="Override TINYFORGE_MODEL")
    parser.add_argument("--base-url", help="Override TINYFORGE_BASE_URL")
    parser.add_argument(
        "--wire-api",
        choices=("chat_completions", "responses"),
        help="Model protocol (default: TINYFORGE_WIRE_API or chat_completions)",
    )
    parser.add_argument("--reasoning-effort", help="Reasoning effort for the Responses API")
    parser.add_argument("--max-rounds", type=int, help="Maximum model/tool rounds")
    parser.add_argument("--tool-timeout", type=int, help="Default command timeout in seconds")
    parser.add_argument("--state-dir", help="Persistent state directory (default: ~/.tinyforge)")
    parser.add_argument("--no-memory", action="store_true", help="Disable persistent memory")
    parser.add_argument(
        "--no-session-archive",
        action="store_true",
        help="Keep memory but disable L4 session archives",
    )
    parser.add_argument(
        "--allow-dangerous",
        action="store_true",
        help="Allow commands blocked by the default destructive-command policy",
    )
    parser.add_argument("--quiet", action="store_true", help="Hide intermediate assistant notes")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    parser.add_argument("--version", action="version", version=f"TinyForge {__version__}")
    return parser


def create_agent(args: argparse.Namespace, console: Console) -> Agent:
    overrides: dict[str, Any] = {
        "model": args.model,
        "base_url": args.base_url.rstrip("/") if args.base_url else None,
        "wire_api": args.wire_api,
        "reasoning_effort": args.reasoning_effort,
        "max_rounds": args.max_rounds,
        "tool_timeout": args.tool_timeout,
        "allow_dangerous": args.allow_dangerous,
        "state_dir": Path(args.state_dir).expanduser().resolve() if args.state_dir else None,
        "memory_enabled": False if args.no_memory else None,
        "archive_sessions": False if args.no_session_archive else None,
    }
    config = Config.from_env(args.workspace, **overrides)
    model = OpenAICompatibleClient(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        timeout=config.request_timeout,
        wire_api=config.wire_api,
        reasoning_effort=config.reasoning_effort,
        store=config.store_responses,
    )
    workspace_tools = WorkspaceTools(
        config.workspace,
        command_timeout=config.tool_timeout,
        max_output=config.max_tool_output,
        allow_dangerous=config.allow_dangerous,
    )
    memory = None
    if config.memory_enabled:
        memory = MemoryRuntime(
            MemoryStore(
                config.state_dir,
                config.workspace,
                archive_sessions=config.archive_sessions,
            )
        )
        tools = CompositeTools(workspace_tools, memory)
    else:
        tools = workspace_tools
    print(
        console.paint("TinyForge", "cyan"),
        console.paint(
            f"model={config.model} api={config.wire_api} "
            f"memory={'on' if memory else 'off'} workspace={config.workspace}",
            "dim",
        ),
    )
    return Agent(
        model=model,
        tools=tools,
        workspace=config.workspace,
        max_rounds=config.max_rounds,
        max_context_chars=config.max_context_chars,
        max_context_tokens=config.max_context_tokens,
        on_event=console.event,
        memory=memory,
    )


def interactive(agent: Agent, console: Console) -> int:
    print("Enter a programming task. Commands: /new, /memory, /help, /exit")
    continuing = False
    while True:
        try:
            task = input(console.paint("\ntinyforge> ", "cyan")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not task:
            continue
        if task in {"/exit", "/quit"}:
            return 0
        if task == "/new":
            agent.reset()
            continuing = False
            print("Started a new session.")
            continue
        if task == "/help":
            print(
                "/new clears conversation context; /memory shows the working and persistent "
                "memory index; /exit closes TinyForge."
            )
            continue
        if task == "/memory":
            print(agent.memory_overview())
            continue
        result = agent.run(task, continue_session=continuing)
        continuing = True
        color = "green" if result.success else "red"
        print(console.paint("\n[final]", color), result.answer)
        console.result_stats(result)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console(color=not args.no_color, verbose=not args.quiet)
    try:
        agent = create_agent(args, console)
        if args.task:
            result = agent.run(" ".join(args.task))
            color = "green" if result.success else "red"
            print(console.paint("\n[final]", color), result.answer)
            console.result_stats(result)
            return 0 if result.success else 1
        return interactive(agent, console)
    except (ConfigError, ModelError) as exc:
        print(console.paint(f"error: {exc}", "red"), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
