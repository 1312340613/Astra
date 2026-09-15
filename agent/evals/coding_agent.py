"""Build the coding agent shared by the source evaluation runners."""

from __future__ import annotations

from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def build_coding_agent(workdir: Path, model_key: str) -> Any:
    """Assemble a minimal coding agent bound to the task workspace."""
    from agent.cli.backend import load_project_env
    from agent.cli.mode_preferences import apply_reasoning_effort
    from agent.cli.model_catalog import configured_model_catalog
    from agent.runtime.llm import LLMClient, LLMConfig
    from agent.runtime.react import ReActAgent
    from agent.runtime.tools.registry import ToolRegistry
    from agent.runtime.tools.files import register_file_tools
    from agent.runtime.tools.code import register_code_tools
    from agent.runtime.tools.git import register_git_tools
    from agent.runtime.tools.time import register_time_tools
    from agent.runtime.prompts import DEFAULT_SYSTEM_PROMPT
    from agent.sandbox.local import LocalSandbox

    load_project_env(PROJECT_ROOT)
    catalog = configured_model_catalog()
    entry = catalog.resolve(model_key) or catalog.resolve_persisted(model_key)
    if entry is None:
        raise ValueError(f"unknown model: {model_key}")
    profile = entry.profile
    api_key = profile.api_key()
    if not api_key:
        raise ValueError(f"{profile.api_key_env} not set for model {model_key}")
    llm_config = apply_reasoning_effort(LLMConfig(
        provider=profile.provider,
        model=entry.model_id,
        api_key=api_key or "local",
        base_url=entry.base_url,
        capabilities=profile.capabilities,
        **profile.generation_settings(),
    ))
    llm = LLMClient(llm_config)
    tools = ToolRegistry()
    sandbox = LocalSandbox(timeout=120, workdir=str(workdir))
    register_file_tools(tools, workdir=str(workdir), sandbox=sandbox)
    register_code_tools(tools, sandbox, task_store=None)
    register_git_tools(tools, workdir=str(workdir))
    register_time_tools(tools)
    agent = ReActAgent(
        name="coding-bench",
        llm_client=llm,
        tool_registry=tools,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        max_iterations=40,
    )
    agent.context.set_session(str(workdir / ".bench-session.jsonl"))
    agent.begin_session()
    return agent
