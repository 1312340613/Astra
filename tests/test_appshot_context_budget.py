"""Compression thresholds are not provider input capacity."""

import asyncio
import copy
import os
from types import SimpleNamespace

import pytest

from agent.cli.appshot_admission import prepare_appshot_message
from agent.cli.appshots import AppshotValidationError
from agent.cli.connections import sync_context_budget
from agent.cli.models import ModelProfile, model_profiles
from agent.core.msg import ContentBlock, Msg
from agent.runtime.llm import LLMConfig
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolRegistry
from test_llm import png_url


@pytest.fixture
def portable_appshot_media(monkeypatch):
    """Keep model-budget contracts portable; POSIX still exercises durable media."""
    if os.name == 'posix':
        return
    import base64
    import hashlib
    import uuid

    from agent.cli.appshots import validate_png
    from agent.runtime.appshot_media import AppshotMediaStore

    stored = {}

    def put(store, raw, width, height):
        validate_png(raw, width, height)
        ref = dict(media_id=uuid.uuid4().hex, sha256=hashlib.sha256(raw).hexdigest(), width=width, height=height)
        stored[str(store.path), ref['media_id']] = raw
        return ref

    def hydrate(store, ref):
        store.validate_reference(ref)
        raw = stored[str(store.path), ref['media_id']]
        return {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(raw).decode('ascii')}}

    monkeypatch.setattr(AppshotMediaStore, 'put', put)
    monkeypatch.setattr(AppshotMediaStore, 'hydrate', hydrate)


@pytest.mark.parametrize("model,base_url", [
    ("qwen3.8-flash", "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"),
    ("deepseek-v4-flash-vision-exp", "https://api.deepseek.com"),
    ("deepseek-flash", "https://api.deepseek.com"),
])
@pytest.mark.usefixtures('portable_appshot_media')
def test_long_history_and_tool_results_use_tokens_across_appshot_guards(tmp_path, model, base_url):
    class LLM:
        def __init__(self):
            self.config = LLMConfig(
                model=model,
                base_url=base_url,
                max_tokens=16384,
                capabilities=frozenset({"vision"}),
            )
            self.calls = []

        def estimate_tokens(self, messages):
            from agent.runtime.token_estimator import estimate_messages_tokens
            return estimate_messages_tokens(messages)

        async def chat_stream(self, messages, tools, **kwargs):
            self.calls.append(messages)
            yield {"type": "done", "content": "seen", "usage": None}

    llm = LLM()
    agent = ReActAgent(
        "fixture", llm, ToolRegistry(), system_prompt="system", max_iterations=1,
        vision_cache_root=tmp_path / "cache", timing_log_enabled=False, query_profile_enabled=False,
    )
    agent.context.set_session(str(tmp_path.resolve() / "session.json"))
    profile = ModelProfile(base_url=llm.config.base_url, context_limit=1000000)
    sync_context_budget(agent, profile)
    assert agent.context.max_prompt_tokens == 500000
    agent.context.add_user("ordinary Chinese 中文 history " * 20000)
    before = copy.deepcopy(agent.context.messages)
    msg = Msg(content=[
        ContentBlock.image_url(png_url()),
        ContentBlock.appshot_context({"window_title": "fixture"}, {"root": {"role": "AXWindow"}}),
    ])
    asyncio.run(prepare_appshot_message(agent, msg))
    assert agent.context.messages == before
    assert not llm.calls
    # Mimic admission's persisted image reference: the runtime/final guard must
    # agree with preview, not clear the draft then fail at its old 50% limit.
    from agent.runtime.appshot_media import AppshotMediaStore
    import base64
    data = msg.content[0].data
    raw = base64.b64decode(data["url"].split(",", 1)[1])
    data["appshot_media"] = AppshotMediaStore(agent.context.session_path).put(raw, 32, 32)

    async def run():
        return [event async for event in agent.reply_stream(msg)]

    events = asyncio.run(run())
    assert llm.calls, events
    assert png_url() in str(llm.calls[-1])
    # The first request succeeds. Additional tool text then crosses a million
    # serialized bytes while its token estimate remains comfortably in budget.
    # This is the user's failure phase: after a tool, on an ordinary follow-up
    # with historical Appshot media (no fresh admission metadata).
    import json
    from agent.cli.appshot_admission import request_token_bound
    from agent.runtime.token_estimator import estimate_messages_tokens

    prompt = copy.deepcopy(llm.calls[-1])
    prompt.extend([
        {"role": "assistant", "content": "", "tool_calls": [{"id": "catalog", "type": "function",
         "function": {"name": "computer_apps", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "catalog", "content": "application title ref and bounds\n" * 15000},
    ])
    before_tool = copy.deepcopy(prompt)
    assert len(json.dumps(prompt, ensure_ascii=False).encode()) > 1000000
    assert estimate_messages_tokens(prompt) < agent.context.max_prompt_tokens

    async def after_tool():
        return [event async for event in agent._llm_stream(messages=prompt, tools=[])]

    asyncio.run(after_tool())
    assert len(llm.calls) == 2
    assert png_url() in str(llm.calls[-1])
    assert prompt == before_tool

    # Refresh to a smaller window, retaining Appshot history: final request
    # must use the new capacity and refuse before another provider call.
    needed = request_token_bound(prompt, [], llm.config)
    sync_context_budget(agent, ModelProfile(base_url=llm.config.base_url, context_limit=needed))
    with pytest.raises(AppshotValidationError, match="context_budget_exceeded"):
        agent._llm_stream(messages=llm.calls[-1], tools=[])
    assert len(llm.calls) == 2


@pytest.mark.parametrize("overrides", [{"max_tokens": 400000}, {"max_completion_tokens": 400000}])
def test_capacity_reserves_actual_output_and_preserves_unknown_fallback(overrides):
    from agent.cli.appshot_admission import appshot_prompt_limit

    context = SimpleNamespace(max_prompt_tokens=500000)
    config = LLMConfig(max_tokens=16384)
    assert appshot_prompt_limit(context, config) == 500000
    config.context_limit = 1000000
    assert appshot_prompt_limit(context, config) == 983616
    assert appshot_prompt_limit(context, config, overrides) == 600000
    assert appshot_prompt_limit(context, config, {"max_tokens": 1}) == 983616
    assert appshot_prompt_limit(context, config, {"max_tokens": 1000001}) == 0


def test_sync_context_keeps_compression_threshold_and_actual_window_separate():
    agent = SimpleNamespace(llm=SimpleNamespace(config=LLMConfig(max_tokens=16384)),
                            context=SimpleNamespace(max_prompt_tokens=1))
    sync_context_budget(agent, ModelProfile(base_url="https://example", context_limit=1000000))
    assert agent.context.max_prompt_tokens == 500000
    assert agent.llm.config.context_limit == 1000000
    sync_context_budget(agent, ModelProfile(base_url="https://example", context_limit=32768))
    assert agent.context.max_prompt_tokens == 16384
    assert agent.llm.config.context_limit == 32768


def test_direct_model_switch_does_not_reuse_old_capacity():
    from agent.runtime.llm import LLMClient

    client = object.__new__(LLMClient)
    client.config = LLMConfig(model="large", context_limit=1000000)
    client.provider = object()
    created = []
    client._create_provider = lambda config: created.append(config) or object()
    client.switch_model("unknown")
    assert client.config.context_limit is None
    assert created[-1].context_limit is None
    client.switch_model("small", context_limit=32768)
    assert client.config.context_limit == 32768


@pytest.mark.parametrize("window", [0, -1, True, "1000000"])
def test_invalid_resolved_capacity_is_not_silently_expanded(window):
    from agent.cli.appshot_admission import appshot_prompt_limit

    config = LLMConfig(context_limit=window)
    with pytest.raises(AppshotValidationError, match="context_budget_unavailable"):
        appshot_prompt_limit(SimpleNamespace(max_prompt_tokens=500000), config)


@pytest.mark.parametrize("target_name", [*model_profiles(), "custom-vision", "custom-text"])
@pytest.mark.usefixtures('portable_appshot_media')
def test_appshot_history_survives_timeout_then_model_switch_and_plain_followup(tmp_path, target_name):
    import base64
    from dataclasses import replace

    from agent.runtime.appshot_media import AppshotMediaStore
    from agent.runtime.llm import _messages_for_capabilities

    class LLM:
        def __init__(self):
            self.config = LLMConfig(model="qwen3.8-flash", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", capabilities=frozenset({"vision"}), context_limit=1000000)
            self.calls = []

        async def chat_stream(self, messages, tools, **kwargs):
            self.calls.append(_messages_for_capabilities(messages, self.config.capabilities, vision_detail=self.config.vision_detail))
            if len(self.calls) == 1:
                raise TimeoutError("fixture timeout")
            yield {"type": "done", "content": "seen", "usage": None}

    llm = LLM()
    agent = ReActAgent("fixture", llm, ToolRegistry(), system_prompt="system", max_iterations=1,
                       vision_cache_root=tmp_path / "cache", timing_log_enabled=False, query_profile_enabled=False)
    agent.context.set_session(str(tmp_path.resolve() / "session.json"))
    url = png_url()
    image = ContentBlock.image_url(url)
    image.data["appshot_media"] = AppshotMediaStore(agent.context.session_path).put(base64.b64decode(url.split(",", 1)[1]), 32, 32)
    msg = Msg(content=[image, ContentBlock.appshot_context({"window_title": "fixture"}, {"root": {"role": "AXWindow"}})])
    asyncio.run(prepare_appshot_message(agent, msg))

    async def reply(message):
        return [event async for event in agent.reply_stream(message)]

    with pytest.raises(TimeoutError, match="fixture timeout"):
        asyncio.run(reply(msg))
    assert len(llm.calls) == 1
    assert "appshot_image" in str(agent.context.messages)

    if target_name.startswith("custom-"):
        profile = ModelProfile(base_url="http://localhost:9999/v1", context_limit=32768,
                               capabilities=frozenset({"vision"}) if target_name == "custom-vision" else frozenset())
    else:
        profile = model_profiles()[target_name]
    llm.config = replace(llm.config, model=target_name, base_url=profile.base_url,
                         capabilities=profile.capabilities, **profile.generation_settings())
    sync_context_budget(agent, profile)
    for text in ("继续吧", "再看看"):
        events = asyncio.run(reply(Msg(content=[ContentBlock.text(text)])))
        assert not [event for event in events if event.get("type") == "error"], events
        images = [part for message in llm.calls[-1] if isinstance(message.get("content"), list)
                  for part in message["content"] if part.get("type") == "image_url"]
        if "vision" in profile.capabilities:
            assert any(part["image_url"]["url"] == url for part in images)
            if profile.vision_detail == "original":
                assert all(part["image_url"]["detail"] == "original" for part in images)
        else:
            assert not images
            assert "image unavailable to this text-only model" in str(llm.calls[-1])
            assert "AXWindow" in str(llm.calls[-1])
        assert "appshot_media" not in str(llm.calls[-1])
    assert len(llm.calls) == 3

    # Fresh Appshot admission also succeeds on the new model, without losing
    # or re-encoding the original PNG to satisfy the tiler's ordinary-image path.
    fresh = Msg(content=[ContentBlock.image_url(url), ContentBlock.appshot_context({}, {"root": {"role": "AXWindow"}})])
    fresh.content[0].data["appshot_verified"] = True
    before = copy.deepcopy(agent.context.messages)
    if "vision" in profile.capabilities:
        asyncio.run(prepare_appshot_message(agent, fresh))
    else:
        with pytest.raises(AppshotValidationError, match="appshot_vision_unavailable"):
            asyncio.run(prepare_appshot_message(agent, fresh))
    assert agent.context.messages == before
    assert fresh.content[0].data["url"] == url

    # Switching back restores the same persisted bytes, including after a
    # text-only interlude; it never erases or re-encodes session attachments.
    llm.config = replace(llm.config, model="qwen3.8-flash", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                         capabilities=frozenset({"vision"}), vision_preprocess=None, context_limit=1000000)
    asyncio.run(reply(Msg(content=[ContentBlock.text("再切回来")])))
    assert url in str(llm.calls[-1])


@pytest.mark.parametrize("shrinks", [True, False])
def test_historical_appshot_budget_compacts_once_before_plain_followup(tmp_path, monkeypatch, shrinks):
    from unittest.mock import AsyncMock
    from agent.cli import appshot_admission

    class LLM:
        config = LLMConfig(model="deepseek-flash", base_url="https://api.deepseek.com",
                           context_limit=40000, max_tokens=8192, capabilities=frozenset({"vision"}))

    agent = ReActAgent("fixture", LLM(), ToolRegistry(), system_prompt="system",
                      vision_cache_root=tmp_path / "cache", timing_log_enabled=False,
                      query_profile_enabled=False)
    agent.context.messages = [{"role": "user", "content": [{"type": "appshot_image"}]},
                              {"role": "user", "content": "continue"}]
    original = [{"role": "user", "content": "long history"}]
    compacted = [{"role": "user", "content": "continue"}]
    agent.context.compress_if_needed = AsyncMock()
    agent._prepare_prompt_for_llm = AsyncMock(return_value=(compacted if shrinks else original, 100))
    schemas = [{"type": "function", "function": {"name": "large_schema"}}]
    seen_tools = []

    def estimate(prompt, tools, config):
        seen_tools.append(tools)
        return 1000 if prompt == compacted else 35000

    monkeypatch.setattr(appshot_admission, "request_token_bound", estimate)
    call = agent._fit_appshot_request_budget(original, schemas, user_text="continue")
    if shrinks:
        prompt, count = asyncio.run(call)
        assert prompt == compacted
        assert count == 100
    else:
        with pytest.raises(AppshotValidationError, match="context_budget_exceeded"):
            asyncio.run(call)
    agent.context.compress_if_needed.assert_awaited_once_with(force=True)
    assert seen_tools == [schemas, schemas]
    # Old indices are invalid after compaction, and the active request is rebuilt.
    assert "current_user_index" not in agent._prepare_prompt_for_llm.call_args.kwargs


def test_budget_recovery_does_not_compact_invalid_image_data(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from agent.cli import appshot_admission

    agent = ReActAgent("fixture", SimpleNamespace(config=LLMConfig(capabilities=frozenset({"vision"}))),
                      ToolRegistry(), system_prompt="system", vision_cache_root=tmp_path / "cache",
                      timing_log_enabled=False, query_profile_enabled=False)
    agent.context.messages = [{"role": "user", "content": [{"type": "appshot_image"}]}]
    agent.context.compress_if_needed = AsyncMock()
    def invalid(*args):
        raise AppshotValidationError("context_budget_unavailable")
    monkeypatch.setattr(appshot_admission, "request_token_bound", invalid)
    with pytest.raises(AppshotValidationError, match="context_budget_unavailable"):
        asyncio.run(agent._fit_appshot_request_budget([], [], user_text="continue"))
    agent.context.compress_if_needed.assert_not_awaited()


@pytest.mark.parametrize("vision", [True, False])
def test_optional_prefix_cannot_overflow_final_budget_or_restore_unsupported_images(tmp_path, monkeypatch, vision):
    from agent.cli import appshot_admission
    from unittest.mock import Mock

    class LLM:
        config = LLMConfig(model="fixture", context_limit=40000, max_tokens=8192,
                           capabilities=frozenset({"vision"}) if vision else frozenset())
        calls = []
        def estimate_tokens(self, messages):
            return 1000
        async def chat_stream(self, messages, tools, **kwargs):
            self.calls.append(messages)
            yield {"type": "done", "content": "ok", "usage": None}

    llm = LLM()
    agent = ReActAgent("fixture", llm, ToolRegistry(), system_prompt="system",
                      vision_cache_root=tmp_path / "cache", timing_log_enabled=False,
                      query_profile_enabled=False)
    agent.context.messages = [{"role": "user", "content": [{"type": "appshot_image"}]}]
    prompt = [{"role": "user", "content": [
        {"type": "text", "text": "retained AX"},
        {"type": "image_url", "image_url": {"url": png_url()}},
    ]}]
    projection = SimpleNamespace(
        project=lambda messages, series: [{"role": "system", "content": "oversized-prefix"}, *messages],
        reset=Mock(),
    )
    agent.context.system_projection = projection
    monkeypatch.setattr(appshot_admission, "request_token_bound",
                        lambda messages, tools, config: 35000 if "oversized-prefix" in str(messages) else 1000)
    async def run():
        return [e async for e in agent._llm_stream(messages=prompt, tools=[])]
    asyncio.run(run())
    projection.reset.assert_called_once()
    assert len(llm.calls) == 1
    assert "retained AX" in str(llm.calls[0])
    assert (png_url() in str(llm.calls[0])) is vision


@pytest.mark.usefixtures('portable_appshot_media')
def test_plain_continue_with_old_appshot_recovers_through_reply_stream(tmp_path, monkeypatch):
    import base64
    from agent.runtime.appshot_media import AppshotMediaStore
    from agent.cli import appshot_admission

    class LLM:
        config = LLMConfig(model="deepseek-flash", base_url="https://api.deepseek.com",
                           context_limit=40000, max_tokens=8192, capabilities=frozenset({"vision"}))
        calls = []
        def estimate_tokens(self, messages):
            return 1000  # Ordinary estimate does not trigger compaction.
        async def chat_stream(self, messages, tools, **kwargs):
            self.calls.append(messages)
            yield {"type": "done", "content": "continued", "usage": None}

    llm = LLM()
    agent = ReActAgent("fixture", llm, ToolRegistry(), system_prompt="system", max_iterations=1,
                      vision_cache_root=tmp_path / "cache", timing_log_enabled=False,
                      query_profile_enabled=False)
    agent.context.set_session(str(tmp_path.resolve() / "session.json"))
    ref = AppshotMediaStore(agent.context.session_path).put(base64.b64decode(png_url().split(',')[1]), 32, 32)
    agent.context.messages = [
        {"role": "user", "content": [{"type": "appshot_image", "appshot_image": ref}]},
        {"role": "assistant", "content": "oversized-old-history"},
    ]
    forced = []
    async def compact(force=False):
        forced.append(force)
        agent.context.messages = [m for m in agent.context.messages if m.get('content') != 'oversized-old-history']
    agent.context.compress_if_needed = compact
    monkeypatch.setattr(appshot_admission, 'request_token_bound',
                        lambda messages, tools, config: 35000 if 'oversized-old-history' in str(messages) else 1000)
    async def run():
        return [e async for e in agent.reply_stream(Msg(content=[ContentBlock.text('continue')]))]
    events = asyncio.run(run())
    assert forced == [True]
    assert len(llm.calls) == 1, events
    assert 'continue' in str(llm.calls[0])
    assert png_url() in str(llm.calls[0])
