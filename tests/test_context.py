import pytest
from test_appshot_backend import bundle as bundle, verifier

from agent.runtime.appshot_media import AppshotMediaError, AppshotMediaStore
from agent.runtime.context import AgentContext


def test_context_owned_image_save_reload(bundle, tmp_path):
    with verifier(bundle) as v:
        decoded = v.decode()
    path = tmp_path.resolve() / "session.json"
    store = AppshotMediaStore(path)
    ref = store.put(decoded.png_bytes, 12, 9)
    context = AgentContext()
    context.set_session(str(path))
    context.add_user([{"type": "text", "text": "inspect"}, {"type": "appshot_image", "appshot_image": ref}])
    context.save()
    restored = AgentContext()
    restored.set_session(str(path))
    assert restored.load()
    prompt = restored.get_prompt()
    assert "data:image/png;base64," in str(prompt)
    assert "media_id" not in str(prompt)
    assert "base64" not in path.with_suffix(".jsonl").read_text()
    (store.path / (ref["media_id"] + ".png")).unlink()
    with pytest.raises(AppshotMediaError):
        restored.get_prompt()


def test_react_owned_media_provider_after_source_removed(bundle, tmp_path):
    import asyncio

    from agent.core.msg import ContentBlock, Msg
    from agent.runtime.llm import LLMConfig
    from agent.runtime.react import ReActAgent
    from agent.runtime.tools.registry import ToolRegistry

    class LLM:
        config = LLMConfig(model="gpt-4o", base_url="https://api.openai.com/v1", capabilities=frozenset({"vision"}))

        def __init__(self):
            self.calls = []

        def estimate_tokens(self, messages):
            return 1

        async def chat_stream(self, messages, tools, **kwargs):
            self.calls.append(messages)
            yield {"type": "done", "content": "seen", "usage": None}

    with verifier(bundle) as v:
        decoded = v.decode()
    path = tmp_path.resolve() / "session.json"
    store = AppshotMediaStore(path)
    ref = store.put(decoded.png_bytes, 12, 9)
    block = ContentBlock.image_url(decoded.image_data_url)
    block.data["appshot_media"] = ref
    msg = Msg(
        content=[block, ContentBlock.appshot_context(decoded.source, decoded.projection)],
        metadata={"appshot_budget_required": True},
    )
    for source in bundle[0].iterdir():
        source.unlink()
    llm = LLM()
    agent = ReActAgent(
        "fixture",
        llm,
        ToolRegistry(),
        system_prompt="system",
        max_iterations=1,
        vision_cache_root=tmp_path / "cache",
        timing_log_enabled=False,
        query_profile_enabled=False,
    )
    agent.context.set_session(str(path))
    agent.context.max_prompt_tokens = 100000

    async def run():
        return [event async for event in agent.reply_stream(msg)]

    events = asyncio.run(run())
    assert llm.calls, events
    assert decoded.image_data_url in str(llm.calls)
    assert "media_id" not in str(llm.calls)
    agent.context.save()
    restored = AgentContext()
    restored.set_session(str(path))
    assert restored.load()
    assert decoded.image_data_url in str(restored.get_prompt())
    assert "base64" not in path.with_suffix(".jsonl").read_text()
    # A much larger final actual request stops before reaching the provider.
    agent.context.max_prompt_tokens = 1
    with pytest.raises(ValueError, match="context_budget_exceeded"):
        agent._llm_stream(messages=restored.get_prompt(), tools=[])
    assert len(llm.calls) == 1
    # Historical pixels are retained, while text-only follow-ups receive an
    # explicit absence notice instead of a permanent Appshot validation error.
    agent.context = restored
    agent.context.max_prompt_tokens = 100000
    llm.config.capabilities = frozenset()

    async def switched():
        return [event async for event in agent._llm_stream(messages=restored.get_prompt(), tools=[])]

    asyncio.run(switched())
    assert len(llm.calls) == 2
    assert "image_url" not in str(llm.calls[-1])
    assert "image unavailable to this text-only model" in str(llm.calls[-1])
    llm.config.capabilities = frozenset({"vision"})
    asyncio.run(switched())
    assert decoded.image_data_url in str(llm.calls[-1])


def test_mixed_message_only_appshot_bypasses_ordinary_preprocessing(bundle, tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from agent.core.msg import ContentBlock, Msg
    from agent.runtime.llm import LLMConfig
    from agent.runtime.react import ReActAgent
    from agent.runtime.tools.registry import ToolRegistry

    with verifier(bundle) as v:
        decoded = v.decode()
    ref = AppshotMediaStore(tmp_path.resolve() / "session.json").put(decoded.png_bytes, 12, 9)
    native = ContentBlock.image_url(decoded.image_data_url)
    native.data["appshot_media"] = ref
    ordinary = ContentBlock.image_url(decoded.image_data_url, source_path="/ordinary.png")
    msg = Msg(content=[ordinary, native], metadata={"appshot_budget_required": True})
    agent = ReActAgent(
        "fixture",
        SimpleNamespace(config=LLMConfig(vision_preprocess=SimpleNamespace())),
        ToolRegistry(),
        vision_cache_root=tmp_path / "cache",
        timing_log_enabled=False,
    )
    agent.context.set_session(str(tmp_path.resolve() / "session.json"))
    monkeypatch.setattr(agent, "_vision_tile_tool_enabled", lambda: True)
    calls = []

    def prepare(blocks, **kwargs):
        calls.append(blocks)
        return SimpleNamespace(
            chat_blocks=blocks,
            storage_blocks=blocks,
            status="fixture",
            protected=True,
            protected_local_images=1,
            unprotected_external_images=0,
        )

    monkeypatch.setattr(agent.vision_preprocessor, "prepare_blocks", prepare)
    asyncio.run(agent._prepare_turn(msg))
    assert len(calls) == 1
    assert [b.type for b in calls[0]] == ["image_url", "appshot_passthrough"]
    assert agent.context.messages[0]["content"][1]["type"] == "appshot_image"
