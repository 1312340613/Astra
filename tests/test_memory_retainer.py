import asyncio
from pathlib import Path

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.runtime.hooks import HookRegistry, HookReject
from agent.runtime.memory import MemoryStore
from agent.runtime.memory_provider import BuiltinMemoryProvider
from agent.runtime.memory_retainer import MemoryRetainer, ToolEvidence
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def make_store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(path=tmp_path / "memory.db", core_dir=tmp_path / "core")


@pytest.fixture(autouse=True)
def opt_in(monkeypatch):
    monkeypatch.setenv("MEMORY_AUTO_RETAIN", "1")


def test_retainer_skips_questions_hypotheticals_and_casual_chat(tmp_path):
    store = make_store(tmp_path)
    retainer = MemoryRetainer(BuiltinMemoryProvider(store), store)

    async def scenario():
        outcomes = []
        for index, text in enumerate(("我喜欢什么？", "如果我喜欢表格会怎么样", "今天天气不错")):
            outcomes.append(await retainer.retain_turn(
                text,
                session_id="s1",
                message_id=f"m{index}",
            ))
        return outcomes

    outcomes = asyncio.run(scenario())
    assert all(item.decision == "skipped" for item in outcomes)
    assert store.record_store.stats()["total"] == 0


def test_evolving_mode_still_skips_one_shot_commands_questions_and_hypotheticals(tmp_path):
    store = make_store(tmp_path)
    retainer = MemoryRetainer(BuiltinMemoryProvider(store), store, mode="evolving")

    async def scenario():
        return [
            await retainer.retain_turn(text, session_id="s1", message_id=f"m{index}")
            for index, text in enumerate((
                "帮我检查一下当前配置",
                "我现在使用的是哪个模型？",
                "如果我以后使用 Hermes 会怎么样",
            ))
        ]

    outcomes = asyncio.run(scenario())
    assert all(item.decision == "skipped" for item in outcomes)
    assert store.record_store.stats()["total"] == 0


def test_retainer_rejects_unknown_mode(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="Unknown memory retention mode"):
        MemoryRetainer(BuiltinMemoryProvider(store), store, mode="turbo")


def test_retainer_dispatches_memory_hook_before_persisting(tmp_path):
    store = make_store(tmp_path)
    hooks = HookRegistry()
    observed = []

    def annotate(record):
        observed.append(dict(record))
        return {**record, "content": f"{record['content']}（已审计）"}

    hooks.on_memory_retain(annotate)
    retainer = MemoryRetainer(BuiltinMemoryProvider(store), store, hooks=hooks)

    outcome = asyncio.run(retainer.retain_turn(
        "inspect timezone",
        session_id="s1",
        message_id="message-1",
        tool_evidence=(ToolEvidence("timezone", "Timezone is UTC", "call-1"),),
    ))

    assert outcome.decision == "retained"
    assert observed and observed[0]["source_session_id"] == "s1"
    record = store.record_store.get(outcome.retained_ids[0])
    assert record is not None and record.content.endswith("（已审计）")


def test_retainer_memory_hook_can_reject_persistence(tmp_path):
    store = make_store(tmp_path)
    hooks = HookRegistry()
    hooks.on_memory_retain(lambda record: (_ for _ in ()).throw(HookReject("private")))
    retainer = MemoryRetainer(BuiltinMemoryProvider(store), store, hooks=hooks)

    outcome = asyncio.run(retainer.retain_turn(
        "inspect timezone",
        session_id="s1",
        message_id="message-1",
        tool_evidence=(ToolEvidence("timezone", "Timezone is UTC", "call-1"),),
    ))

    assert outcome.decision == "rejected"
    assert "private" in outcome.reason
    assert store.record_store.stats()["total"] == 0


def test_react_wires_registry_memory_hooks_into_injected_retainer(tmp_path):
    class FakeConfig:
        model = "fake"
        capabilities = frozenset()

    class FakeLLM:
        config = FakeConfig()

    store = make_store(tmp_path)
    hooks = HookRegistry()
    registry = ToolRegistry(hooks=hooks)
    retainer = MemoryRetainer(BuiltinMemoryProvider(store), store)

    agent = ReActAgent(
        "agent",
        FakeLLM(),  # type: ignore[arg-type]
        registry,
        memory_store=store,
        memory_retainer=retainer,
    )

    assert agent.memory_retainer is not None
    assert agent.memory_retainer.hooks is hooks


def test_react_retains_opt_in_tool_evidence_as_observation(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_AUTO_RETAIN", "1")
    class FakeConfig:
        model = "fake"
        capabilities = frozenset()

    class ToolLLM:
        config = FakeConfig()

        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{"id": "call-1", "name": "verify_timezone", "arguments": "{}"}],
                    "content": "",
                    "usage": None,
                }
            else:
                yield {"type": "done", "content": "时区已确认", "usage": None}

    async def verify_timezone():
        return "Asia/Shanghai"

    registry = ToolRegistry()
    registry.register(ToolDef(
        name="verify_timezone",
        description="verify timezone",
        parameters={"type": "object", "properties": {}},
        fn=verify_timezone,
        risk="read",
        memory_evidence=lambda event: f"已验证用户时区为 {event['output']}",
    ))
    store = make_store(tmp_path)
    agent = ReActAgent("agent", ToolLLM(), registry, memory_store=store, max_iterations=3)  # type: ignore[arg-type]
    agent.context.set_session(str(tmp_path / "s1.jsonl"))

    asyncio.run(agent.reply(Msg(content=[ContentBlock.text("检查当前时区")])))

    records = store.recall_records("Asia/Shanghai", kinds=("observation",))
    assert [record.content for record in records] == ["已验证用户时区为 Asia/Shanghai"]
    assert records[0].metadata["ttl_days"] == 90
    assert records[0].metadata["maturity"] == "provisional"
    assert records[0].valid_until



def test_tool_evidence_preserves_source_without_promoting_repetition(tmp_path):
    store = make_store(tmp_path)
    retainer = MemoryRetainer(BuiltinMemoryProvider(store), store)

    async def retain(message):
        return await retainer.retain_turn("", session_id="s1", message_id=message,
            tool_evidence=(ToolEvidence("timezone", "Timezone is UTC", "call-1"),))

    first = asyncio.run(retain("m1"))
    old = store.record_store.get(first.retained_ids[0])
    assert old.metadata["tool_call_id"] == "call-1"
    assert old.source_session_id == "s1" and old.source_message_id == "m1"
    assert old.valid_until
    assert asyncio.run(retain("m1")).decision == "skipped"
    other = asyncio.run(retain("m2"))
    assert len(other.retained_ids) == 1
    assert asyncio.run(retain("m1")).decision == "skipped"
    assert store.record_store.get(old.record_id) == old
    assert other.confirmed_ids == () and other.superseded_ids == ()


@pytest.mark.parametrize("evidence", [ToolEvidence("probe", "fact"), ToolEvidence("probe", "x" * 1001, "call")])
def test_unattributed_or_oversized_evidence_is_not_stored(tmp_path, evidence):
    store = make_store(tmp_path)
    retainer = MemoryRetainer(BuiltinMemoryProvider(store), store)
    outcome = asyncio.run(retainer.retain_turn("", session_id="s", message_id="m", tool_evidence=[evidence]))
    assert outcome.decision == "skipped"
    assert store.record_store.stats()["total"] == 0


@pytest.mark.parametrize("result_kind", ["tool_error", "nonzero", "running", "unknown"])
def test_react_never_retains_unsuccessful_execution_evidence(tmp_path, result_kind):
    from agent.runtime.tool_execution import ExecutionResult

    class FakeConfig:
        model = "fake"
        capabilities = frozenset()

    class ToolLLM:
        config = FakeConfig()
        calls = 0

        async def chat_stream(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield {"type": "tool_calls", "calls": [{"id": "call", "name": "probe", "arguments": "{}"}],
                       "content": "", "usage": None}
            else:
                yield {"type": "done", "content": "finished", "usage": None}

    async def probe():
        if result_kind == "tool_error":
            raise ValueError("failed probe")
        metadata = {"nonzero": {"exit_code": 1}, "running": {"status": "running"}, "unknown": {}}[result_kind]
        return ExecutionResult("untrusted observation", metadata)

    extracted = []
    registry = ToolRegistry()
    registry.register(ToolDef(name="probe", description="probe", risk="execute", fn=probe,
        parameters={"type": "object", "properties": {}},
        memory_evidence=lambda event: extracted.append(event) or "should not be saved"))
    store = make_store(tmp_path)
    agent = ReActAgent("agent", ToolLLM(), registry, memory_store=store, max_iterations=3)
    agent.context.set_session(str(tmp_path / "session.jsonl"))
    asyncio.run(agent.reply(Msg(content=[ContentBlock.text("probe")])) )
    assert extracted == []
    assert store.record_store.stats()["total"] == 0


def test_auto_retention_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_AUTO_RETAIN")
    store = make_store(tmp_path)
    retainer = MemoryRetainer(BuiltinMemoryProvider(store), store)
    assert not retainer.enabled
    outcome = asyncio.run(retainer.retain_turn("记住：我喜欢表格", session_id="s", message_id="m",
        tool_evidence=[ToolEvidence("probe", "fact", "call")]))
    assert outcome.retained_ids == ()
