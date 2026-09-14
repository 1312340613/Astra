"""Minimal Mode controller tests: isolation, tool allowlist, and restore."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from agent.cli import sessions as session_helpers
from agent.runtime.context import AgentContext
from agent.runtime.minimal_mode import (
    MINIMAL_STATUS_OPEN_TEMPLATE,
    MINIMAL_SYSTEM_PROMPT,
    MINIMAL_TOOL_NAMES,
    MinimalModeController,
)
from agent.runtime.react import ReActAgent


class _FakeTools:
    """Registry-shaped surface for token-cost estimation."""

    def to_openai_tools(self, names=None, groups=None):
        return [
            {"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object", "properties": {}}}}
            for name in sorted(names or [])
        ]


class _FakeSandbox:
    def __init__(self) -> None:
        self.mode = "local"
        self.switches: list[str] = []
        self.wsl_closes = 0

    def switch(self, mode: str) -> str:
        self.switches.append(mode)
        self.mode = mode
        return mode

    def close_wsl_shell(self) -> None:
        self.wsl_closes += 1


def test_minimal_prefers_generic_persistent_bash_lifecycle(tmp_path: Path) -> None:
    class GenericSandbox(_FakeSandbox):
        def __init__(self) -> None:
            super().__init__()
            self.persistent_bash_closes = 0

        def close_persistent_bash(self) -> None:
            self.persistent_bash_closes += 1

        def close_wsl_shell(self) -> None:
            raise AssertionError("the generic lifecycle should be preferred")

    agent = _FakeAgent(tmp_path / "work-session.json")
    agent._sandbox = GenericSandbox()
    ctl = MinimalModeController(agent)  # type: ignore[arg-type]

    assert ctl.enter(tmp_path / "minimal.json") is True
    assert agent._sandbox.persistent_bash_closes == 1


class _FakeAgent:
    """ReActAgent-shaped surface the controller talks to (duck-typed)."""

    def __init__(self, work_session: Path) -> None:
        self.context = AgentContext(system_prompt="work prompt")
        self.context.set_session(str(work_session))
        self.tools = _FakeTools()
        self.tools.yolo = False
        self.memory_store = object()
        self.external_memory_provider = object()
        self.skill_store = object()
        self.task_store = object()
        self.tools_enabled = True
        self.tool_allowlist: set[str] | None = None
        self.runtime_context_provider = lambda: "runtime"
        self.runtime_turn_context_provider = lambda: "turn"
        self.generation_overrides_provider = None
        self.finalize_after_tools_provider = None
        self.forced_tool_name: str | None = None
        self.code_mode = "native"
        self.minimal_mode = False
        self._sandbox = _FakeSandbox()
        self._ended: list[str] = []
        self._begun = 0

    def end_session(self, reason: str) -> None:
        self._ended.append(reason)

    def begin_session(self) -> None:
        self._begun += 1


@pytest.fixture()
def controller(tmp_path: Path) -> tuple[MinimalModeController, _FakeAgent, Path]:
    work = tmp_path / "work-session.json"
    agent = _FakeAgent(work)
    ctl = MinimalModeController(agent)  # type: ignore[arg-type]
    minimal_path = tmp_path / "minimal" / "minimal.json"
    return ctl, agent, minimal_path


def test_minimal_prompt_is_single_sentence() -> None:
    assert MINIMAL_SYSTEM_PROMPT == "You are a helpful software engineer assistant."


def test_minimal_tool_names_include_dsh_surface_ptc_and_read_only_web_tools() -> None:
    assert {"bash", "str_replace_editor", "run_code"}.issubset(MINIMAL_TOOL_NAMES)
    assert {"search_web", "web_extract"}.issubset(MINIMAL_TOOL_NAMES)


def test_minimal_status_template_names_the_actual_minimal_tools() -> None:
    """Regression: /minimal status must not advertise the stale run_code/edit_file pair."""
    status = MINIMAL_STATUS_OPEN_TEMPLATE.format(session="minimal-session")
    assert "session minimal-session" in status
    assert "bash" in status
    assert "str_replace_editor" in status
    assert "run_code" in status
    assert "search_web" in status
    assert "web_extract" in status
    assert "edit_file" not in status
    assert "run_code/edit_file" not in status


def test_enter_swaps_context_and_applies_allowlist(
    controller: tuple[MinimalModeController, _FakeAgent, Path],
) -> None:
    ctl, agent, minimal_path = controller
    assert ctl.enter(minimal_path) is True
    assert ctl.active is True
    assert agent.context.system_prompt == MINIMAL_SYSTEM_PROMPT
    assert agent.context.compressor is None
    assert agent.context.compaction_enabled is False
    assert agent.tool_allowlist == set(MINIMAL_TOOL_NAMES)
    assert agent.code_mode == "both"
    assert agent.minimal_mode is True
    assert agent.memory_store is None
    assert agent.external_memory_provider is None
    assert agent.skill_store is None
    assert agent.task_store is None
    assert agent.runtime_context_provider is None
    assert agent.runtime_turn_context_provider is None
    assert agent.forced_tool_name is None
    assert agent._ended == ["minimal_enter"]
    assert agent._sandbox.mode == "docker"
    assert agent._sandbox.switches == ["docker"]


def test_enter_twice_is_noop(
    controller: tuple[MinimalModeController, _FakeAgent, Path],
) -> None:
    ctl, _, minimal_path = controller
    assert ctl.enter(minimal_path) is True
    assert ctl.enter(minimal_path) is False
    assert ctl.active is True


def test_reset_clears_only_the_minimal_session(
    controller: tuple[MinimalModeController, _FakeAgent, Path],
) -> None:
    ctl, agent, minimal_path = controller
    agent.context.add_user("parked work")
    work_context = agent.context

    assert ctl.enter(minimal_path) is True
    agent.context.add_user("minimal content")
    assert ctl.reset() is True

    assert ctl.active is True
    assert agent.context is not work_context
    assert agent.context.session_path == str(minimal_path)
    assert agent.context.messages == []
    assert agent.context.system_prompt == MINIMAL_SYSTEM_PROMPT
    assert agent.minimal_mode is True
    assert work_context.messages[0]["content"] == "parked work"


def test_minimal_mode_switches_and_persists_named_sessions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(session_helpers, "SESSION_DIR", tmp_path / ".sessions")
    agent = _FakeAgent(tmp_path / "work-session.json")
    ctl = MinimalModeController(agent)  # type: ignore[arg-type]
    first = session_helpers.minimal_session_path("minimal_first")
    second = session_helpers.minimal_session_path("minimal_second")

    assert ctl.enter(first) is True
    agent.context.add_user("first minimal prompt")
    ctl.switch(second)
    assert agent.context.session_path == str(second)
    assert session_helpers.minimal_session_msg_count("minimal_first") == 1

    agent.context.add_user("second minimal prompt")
    assert ctl.leave() is True
    assert session_helpers.minimal_session_msg_count("minimal_second") == 1
    assert set(session_helpers.list_minimal_sessions()) == {"minimal_first", "minimal_second"}
    assert "minimal_first" not in session_helpers.list_sessions()
    assert "minimal_second" not in session_helpers.list_sessions()

    assert ctl.enter(first) is True
    assert [message["content"] for message in agent.context.messages] == ["first minimal prompt"]
    assert ctl.leave() is True


def test_leave_restores_every_parked_field(
    controller: tuple[MinimalModeController, _FakeAgent, Path],
) -> None:
    ctl, agent, minimal_path = controller
    work_context = agent.context
    memory = agent.memory_store
    skill = agent.skill_store
    runtime = agent.runtime_context_provider

    assert ctl.enter(minimal_path) is True
    assert ctl.leave() is True

    assert ctl.active is False
    assert agent.context is work_context
    assert agent.context.system_prompt == "work prompt"
    assert agent.memory_store is memory
    assert agent.skill_store is skill
    assert agent.runtime_context_provider is runtime
    assert agent.tool_allowlist is None
    assert agent.code_mode == "native"
    assert agent.minimal_mode is False
    assert agent._sandbox.mode == "local"
    assert agent._sandbox.switches == ["docker", "local"]
    assert agent._ended == ["minimal_enter", "minimal_leave"]
    assert agent._sandbox.wsl_closes == 2


def test_reset_closes_persistent_wsl_shell(
    controller: tuple[MinimalModeController, _FakeAgent, Path],
) -> None:
    ctl, agent, minimal_path = controller
    assert ctl.enter(minimal_path) is True
    assert ctl.reset() is True
    assert agent._sandbox.wsl_closes == 2


def test_leave_when_inactive_is_noop(
    controller: tuple[MinimalModeController, _FakeAgent, Path],
) -> None:
    ctl, _, _ = controller
    assert ctl.leave() is False


def test_minimal_does_not_compact_history_even_when_forced(
    controller: tuple[MinimalModeController, _FakeAgent, Path],
) -> None:
    ctl, agent, minimal_path = controller
    assert ctl.enter(minimal_path) is True
    agent.context.messages = [
        {"role": "user", "content": "keep this history"},
        {"role": "assistant", "content": "keep this answer"},
    ]
    before = deepcopy(agent.context.messages)

    asyncio.run(agent.context.compress_if_needed(force=True))

    assert agent.context.messages == before


def test_react_refresh_guards_are_active_in_minimal_mode() -> None:
    """With minimal_mode=True the suffix refreshers must do nothing."""
    agent = object.__new__(ReActAgent)
    agent.minimal_mode = True
    agent.skill_store = None
    agent._skill_catalog_snapshot = "old catalog"
    agent._project_instructions = None
    agent._available_skill_names = {"x"}
    agent.context = AgentContext(system_prompt="minimal")
    agent.context.set_stable_system_suffix("must stay")

    assert agent._refresh_skill_catalog() is False
    assert agent._refresh_project_instructions(force=True) is False
    assert agent.context._stable_system_suffix == "must stay"


def test_minimal_yolo_is_scoped_and_restored_on_leave(
    controller: tuple[MinimalModeController, _FakeAgent, Path],
) -> None:
    ctl, agent, minimal_path = controller
    agent.tools.yolo = True

    assert ctl.enter(minimal_path) is True
    assert agent.tools.yolo is False

    agent.tools.yolo = True
    assert ctl.leave() is True
    assert agent.tools.yolo is True


def test_minimal_switch_and_reset_turn_yolo_off(
    controller: tuple[MinimalModeController, _FakeAgent, Path],
) -> None:
    ctl, agent, minimal_path = controller
    assert ctl.enter(minimal_path) is True
    agent.tools.yolo = True

    other = minimal_path.with_name("minimal-other.json")
    ctl.switch(other)
    assert agent.tools.yolo is False

    agent.tools.yolo = True
    assert ctl.reset() is True
    assert agent.tools.yolo is False


def test_minimal_leave_with_yolo_off_restores_work_yolo_off(
    controller: tuple[MinimalModeController, _FakeAgent, Path],
) -> None:
    ctl, agent, minimal_path = controller
    assert ctl.enter(minimal_path) is True
    assert ctl.leave() is True
    assert agent.tools.yolo is False
