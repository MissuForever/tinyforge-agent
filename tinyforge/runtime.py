"""Shared runtime assembly for command-line and graphical frontends."""

from __future__ import annotations

from .agent import Agent, EventHandler
from .config import Config
from .memory import MemoryRuntime, MemoryStore
from .model import OpenAICompatibleClient
from .skills import SkillCatalog, SkillRuntime
from .tools import CompositeTools, WorkspaceTools


def build_agent(config: Config, *, on_event: EventHandler | None = None) -> Agent:
    """Build an Agent from a validated immutable configuration snapshot."""
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
    providers = [workspace_tools]
    if config.memory_enabled:
        memory = MemoryRuntime(
            MemoryStore(
                config.state_dir,
                config.workspace,
                archive_sessions=config.archive_sessions,
            )
        )
        providers.append(memory)
    skills = SkillRuntime(
        SkillCatalog(config.workspace, config.user_skills_dir),
        enabled=config.skills_enabled,
    )
    if config.skills_enabled:
        providers.append(skills)
    tools = providers[0] if len(providers) == 1 else CompositeTools(*providers)
    return Agent(
        model=model,
        tools=tools,
        workspace=config.workspace,
        max_rounds=config.max_rounds,
        max_context_chars=config.max_context_chars,
        max_context_tokens=config.max_context_tokens,
        on_event=on_event,
        memory=memory,
        skills=skills,
        skills_enabled=config.skills_enabled,
    )
