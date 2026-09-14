"""Model output allowances survive mode and per-call answer-only limits."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent.cli import models
from agent.cli.mode_preferences import apply_reasoning_effort
from agent.runtime.bar_mode import BarModeController
from agent.runtime.llm import LLMConfig, OpenAICompatibleProvider


CLOUD_LIMITS = {
    "deepseek-flash": 384_000,
    "qwen3.8-max": 131_072,
    "qwen3.8-flash": 131_072,
    "glm-5.3-flash": 131_072,
    "hy4-preview": 65_536,
}
MESSAGES = [{"role": "user", "content": "Reply with OK."}]


@pytest.fixture
def profiles(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MODELS_FILE", str(models.DEFAULT_MODELS_PATH))
    monkeypatch.setenv("AGENT_USER_MODELS_FILE", str(tmp_path / "absent.yaml"))
    return models.model_profiles()


def recording_provider(config):
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = config
    captured = []

    async def stream():
        for reasoning, content in [("thinking", None), (None, "OK")]:
            yield SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop" if content else None,
                delta=SimpleNamespace(
                    reasoning_content=reasoning, content=content, tool_calls=None,
                ),
            )], usage=None)

    async def completion(kwargs):
        captured.append(deepcopy(kwargs))
        if kwargs.get("stream"):
            return stream()
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content="OK", tool_calls=None),
        )], usage=None)

    provider._create_completion = completion
    return provider, captured


def invoke(provider, entrypoint, **options):
    async def request():
        if entrypoint in {"stream", "bar", "low-mode", "high-mode", "max-mode"}:
            if entrypoint in {"low-mode", "high-mode", "max-mode"}:
                mode = entrypoint.removesuffix("-mode")
                provider.config = apply_reasoning_effort(provider.config, mode)
            overrides = BarModeController.generation_overrides() if entrypoint == "bar" else None
            events = [event async for event in provider.chat_stream(
                MESSAGES, generation_overrides=overrides,
            )]
            assert events[-1]["type"] == "done"
            return events[-1]
        method = provider.chat if entrypoint == "chat" else provider.chat_limited
        return await method(MESSAGES, max_tokens=256, **options)

    result = asyncio.run(request())
    assert result["content"] == "OK"


@pytest.mark.parametrize("model,limit", CLOUD_LIMITS.items())
@pytest.mark.parametrize("entrypoint", ["stream", "bar", "chat", "limited", "low-mode", "high-mode", "max-mode"])
def test_cloud_allowance_survives_every_request_path(profiles, model, limit, entrypoint):
    profile = profiles[model]
    assert profile.max_tokens == limit
    config = LLMConfig(
        model=model,
        capabilities=profile.capabilities,
        context_limit=profile.context_limit,
        overall_timeout=0,
        max_retries=0,
        **profile.generation_settings(),
    )
    provider, captured = recording_provider(config)

    invoke(provider, entrypoint)

    assert captured[0]["max_tokens"] == limit
    assert config.max_tokens == limit
    if entrypoint == "bar":
        assert captured[0]["temperature"] == 0.65
        assert captured[0]["top_p"] == 0.9


@pytest.mark.parametrize("effort", [None, "low", "high", "max"])
@pytest.mark.parametrize("entrypoint", ["stream", "bar", "chat", "limited"])
def test_legacy_deepseek_budget_includes_default_on_reasoning(effort, entrypoint):
    provider, captured = recording_provider(LLMConfig(
        model="deepseek-v4-flash", max_tokens=4096, reasoning_effort=effort,
    ))

    invoke(provider, entrypoint)

    assert captured[0]["max_tokens"] == 384_000
    assert captured[0].get("reasoning_effort") == effort
    assert provider.config.max_tokens == 4096


def test_per_call_effort_can_override_provider_default():
    provider, captured = recording_provider(LLMConfig(reasoning_effort=None))

    invoke(provider, "limited", reasoning_effort="low")

    assert captured[0]["reasoning_effort"] == "low"
    assert captured[0]["max_tokens"] == 384_000


@pytest.mark.parametrize("thinking_mode", [None, "enabled"])
def test_disabling_deepseek_thinking_uses_native_flag_and_small_answer_cap(thinking_mode):
    provider, captured = recording_provider(LLMConfig(
        max_tokens=384_000, thinking_mode=thinking_mode,
    ))

    invoke(provider, "limited", disable_thinking=True)

    assert captured[0]["max_tokens"] == 256
    assert captured[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert provider.config.thinking_mode == thinking_mode


@pytest.mark.parametrize("entrypoint,limit", [("stream", 4096), ("bar", 8192), ("chat", 256), ("limited", 256)])
def test_explicit_disabled_profile_is_not_auto_expanded(entrypoint, limit):
    provider, captured = recording_provider(LLMConfig(thinking_mode="disabled"))

    invoke(provider, entrypoint)

    assert captured[0]["max_tokens"] == limit


@pytest.mark.parametrize("required", [False, True])
def test_generic_disabled_thinking_respects_required_capability(required):
    capabilities = {"reasoning"}
    if required:
        capabilities.add("thinking-required")
    provider, captured = recording_provider(LLMConfig(
        model="generic-thinking", max_tokens=131_072,
        capabilities=frozenset(capabilities),
    ))

    invoke(provider, "limited", disable_thinking=True)

    if required:
        assert captured[0]["max_tokens"] == 131_072
        assert "extra_body" not in captured[0]
    else:
        assert captured[0]["max_tokens"] == 256
        assert captured[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_retry_with_thinking_restores_allowance(monkeypatch):
    import agent.runtime.llm as llm_module

    class FakeBadRequestError(Exception):
        pass

    monkeypatch.setattr(llm_module, "BadRequestError", FakeBadRequestError)
    provider, captured = recording_provider(LLMConfig(
        model="generic-thinking", max_tokens=131_072,
        capabilities=frozenset({"reasoning"}), max_retries=0,
    ))
    original = provider._create_completion

    async def reject_disable(kwargs):
        if not captured:
            captured.append(deepcopy(kwargs))
            raise FakeBadRequestError("enable_thinking must be true")
        return await original(kwargs)

    provider._create_completion = reject_disable

    invoke(provider, "limited", disable_thinking=True)

    assert [request["max_tokens"] for request in captured] == [256, 131_072]
    assert "extra_body" not in captured[1]


def test_auto_expansion_reserves_prompt_room_for_small_context():
    provider, captured = recording_provider(LLMConfig(context_limit=32_768))

    invoke(provider, "limited")

    assert captured[0]["max_tokens"] == 16_384
    prompt_budget = models.prompt_token_budget(32_768, provider.config.max_tokens)
    assert prompt_budget + captured[0]["max_tokens"] <= 32_768


@pytest.mark.parametrize("model,limit", [("Qwen3.6-35B-A3B", 32_768), ("gemma-4-12B-it", 4096)])
def test_local_deployment_budgets_are_preserved(profiles, model, limit):
    profile = profiles[model]
    provider, captured = recording_provider(LLMConfig(
        model=model, capabilities=profile.capabilities,
        context_limit=profile.context_limit, **profile.generation_settings(),
    ))

    invoke(provider, "stream")

    assert profile.max_tokens == limit
    assert captured[0]["max_tokens"] == limit


def test_non_reasoning_models_keep_small_per_call_cap():
    provider, captured = recording_provider(LLMConfig(model="plain", max_tokens=16_384))

    invoke(provider, "limited")

    assert captured[0]["max_tokens"] == 256


def test_reasoning_allowance_never_lowers_a_larger_explicit_override():
    provider, _ = recording_provider(LLMConfig(
        model="custom-thinking", max_tokens=8192, capabilities=frozenset({"reasoning"}),
    ))

    kwargs = provider._completion_kwargs(MESSAGES, generation_overrides={"max_tokens": 16_384})

    assert kwargs["max_tokens"] == 16_384
