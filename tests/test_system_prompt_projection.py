import copy

import pytest

from agent.runtime.context import AgentContext
from agent.runtime.llm import LLMConfig
from agent.runtime.system_prompt_projection import SystemPromptProjection, system_prompt_series


def prompt(system, history):
    return [{"role": "system", "content": system}, *history]


def test_updates_preserve_request_prefix_without_mutating_history():
    projection = SystemPromptProjection()
    history = [{"role": "user", "content": "hello"}]
    initial = projection.project(prompt("first", history), "model-a")
    updated = projection.project(prompt("second", history), "model-a")
    assert updated[:-1] == initial
    assert updated[-1] == {"role": "system", "content": "second"}
    assert projection.project(prompt("second", history), "model-a") == updated
    history.append({"role": "assistant", "content": "answer"})
    third = projection.project(prompt("third", history), "model-a")
    assert third[:len(updated)] == updated
    assert len(history) == 2
    assert SystemPromptProjection(copy.deepcopy(projection.state)).project(prompt("third", history), "model-a") == third


@pytest.mark.parametrize("change", ["rewrite", "compact", "model", "disabled", "empty"])
def test_invalidated_prefix_rebases_to_current_authority(change):
    projection = SystemPromptProjection()
    history = [{"role": "user", "content": "hello"}]
    projection.project(prompt("old", history), "a")
    projection.project(prompt("current", history), "a")
    if change == "rewrite":
        history[0]["content"] = "edited"
    if change == "compact":
        history = []
    series = "b" if change == "model" else "" if change == "disabled" else "a"
    current = "" if change == "empty" else "current"
    assert projection.project(prompt(current, history), series) == prompt(current, history)


def test_projection_persists_in_session_header(tmp_path):
    path = str(tmp_path / "session.json")
    context = AgentContext(system_prompt="first")
    context.set_session(path)
    context.add_user("hello")
    context.system_projection.project(context.get_prompt(), "a")
    context.set_system_prompt("latest")
    expected = context.system_projection.project(context.get_prompt(), "a")
    context.save()
    restored = AgentContext()
    restored.set_session(path)
    assert restored.load()
    assert restored.system_projection.project(restored.get_prompt(), "a") == expected
    assert len(restored.messages) == 1
    restored.reset()
    assert restored.system_projection.state == {}


def test_capability_route_and_tools_gate_the_series(monkeypatch):
    monkeypatch.delenv("ASTRA_SYSTEM_PROMPT_HISTORY", raising=False)
    config = LLMConfig(model="deepseek-flash")
    series = system_prompt_series(config, [])
    assert series
    assert system_prompt_series(config, [{"name": "new"}]) != series
    config.base_url = "https://custom.example/v1"
    assert not system_prompt_series(config, [])
    config.capabilities = frozenset({"system-prompt-in-history"})
    assert system_prompt_series(config, [])
    monkeypatch.setenv("ASTRA_SYSTEM_PROMPT_HISTORY", "0")
    assert not system_prompt_series(config, [])


def test_verified_addition_reuses_prefix_without_repeating_persona():
    projection = SystemPromptProjection()
    history = [{"role": "user", "content": "hello"}]
    initial = projection.project(prompt("identity\nstyle", history), "a")
    updated = projection.project(prompt("identity\nwork rules\nstyle", history), "a", addition="\nwork rules")
    assert updated[:-1] == initial
    assert updated[-1]["content"] == "\nwork rules"
    assert SystemPromptProjection(copy.deepcopy(projection.state)).project(
        prompt("identity\nwork rules\nstyle", history), "a",
    ) == updated


@pytest.mark.parametrize("change", ["invalid-addition", "changed-persona", "corrupt-restore"])
def test_unproven_delta_falls_back_to_complete_current_authority(change):
    projection = SystemPromptProjection()
    history = [{"role": "user", "content": "hello"}]
    projection.project(prompt("base", history), "a")
    current = "base rules" if change != "changed-persona" else "different rules"
    delta = " rules" if change != "invalid-addition" else "unrelated"
    result = projection.project(prompt(current, history), "a", addition=delta)
    if change == "corrupt-restore":
        projection.state["updates"][0]["addition"] = "forged"
        assert projection.project(prompt(current, history), "a") == prompt(current, history)
    else:
        assert result[-1]["content"] == current
