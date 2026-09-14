import asyncio
import json

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def call(name, ident):
    return {"name": name, "id": ident, "arguments": "{}"}


def make_agent(registry, llm=None, **kwargs):
    return ReActAgent("probe", llm or object(), registry, timing_log_enabled=False,
                      query_profile_enabled=False, progressive_tools=False, **kwargs)


def test_network_opt_in_overlaps_and_publishes_before_batch_finishes(tmp_path):
    async def scenario():
        registry = ToolRegistry(artifact_dir=tmp_path)
        slow_started = asyncio.Event()
        release = asyncio.Event()
        published = asyncio.Event()
        events = []

        async def slow():
            slow_started.set()
            await release.wait()
            return "slow result"

        async def fast():
            await slow_started.wait()
            return "fast result"

        for name, fn in [("slow", slow), ("fast", fast)]:
            registry.register(ToolDef(name, name, {"type": "object"}, fn,
                                      risk="network", idempotent=True, parallel_safe=True))
        agent = make_agent(registry)

        def receive(event):
            events.append(event)
            published.set()

        task = asyncio.create_task(agent._execute_tool_calls(
            [call("slow", "1"), call("fast", "2")], result_callback=receive))
        try:
            await asyncio.wait_for(published.wait(), 2)
            assert [e["id"] for e in events] == ["2"]
            assert not task.done()
            release.set()
            results = await task
            assert [e["id"] for e in events] == ["2", "1"]
            assert [e["id"] for e in results] == ["1", "2"]
            assert all(not any(k.startswith("_") for k in e) for e in events)
        finally:
            release.set()
            await task
    asyncio.run(scenario())


def test_network_without_opt_in_is_still_ordered_and_cancel_drains(tmp_path):
    async def scenario():
        registry = ToolRegistry(artifact_dir=tmp_path)
        started = asyncio.Event()
        cancelled = asyncio.Event()
        seen = []

        async def first():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def second():
            seen.append("second")
            return "ok"

        for name, fn in [("first", first), ("second", second)]:
            registry.register(ToolDef(name, name, {"type": "object"}, fn, risk="network", idempotent=True))
        task = asyncio.create_task(make_agent(registry)._execute_tool_calls([call("first", "1"), call("second", "2")]))
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.sleep(0)
        assert not seen
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert not seen
    asyncio.run(scenario())


def test_concurrent_batch_cancellation_drains_all_workers(tmp_path):
    async def scenario():
        registry = ToolRegistry(artifact_dir=tmp_path)
        started = []
        stopped = []
        both = asyncio.Event()

        async def wait(number):
            started.append(number)
            if len(started) == 2:
                both.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(number)

        registry.register(ToolDef("wait", "wait", {"type": "object", "properties": {"number": {"type": "integer"}}}, wait,
                                  risk="network", idempotent=True, parallel_safe=True))
        calls = [{"name": "wait", "id": str(i), "arguments": json.dumps({"number": i})} for i in range(3)]
        task = asyncio.create_task(make_agent(registry, tool_concurrency=2)._execute_tool_calls(calls))
        await asyncio.wait_for(both.wait(), 2)
        assert started == [0, 1]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sorted(stopped) == [0, 1]
    asyncio.run(scenario())


def test_result_stream_is_early_exactly_once_and_model_order_is_original(tmp_path):
    async def scenario():
        registry = ToolRegistry(artifact_dir=tmp_path)
        release = asyncio.Event()

        async def slow():
            await release.wait()
            return "slow"

        async def fast():
            return "fast"

        for name, fn in [("slow", slow), ("fast", fast)]:
            registry.register(ToolDef(name, name, {"type": "object"}, fn, risk="network", idempotent=True, parallel_safe=True))

        class LLM:
            count = 0
            async def chat_stream(self, messages, **kwargs):
                self.count += 1
                if self.count == 1:
                    yield {"type": "tool_calls", "calls": [call("slow", "s"), call("fast", "f")], "content": ""}
                else:
                    assert [m["tool_call_id"] for m in messages if m.get("role") == "tool"] == ["s", "f"]
                    yield {"type": "chunk", "content": "done"}
                    yield {"type": "done", "content": "done"}

        agent = make_agent(registry, LLM(), max_iterations=3)
        agent.context.set_session(str(tmp_path / "session.json"))
        ids = []
        async with asyncio.timeout(3):
            async for e in agent.reply_stream(Msg(content=[ContentBlock.text("test")])):
                if e["type"] == "tool_result":
                    ids.append(e["id"])
                    if e["id"] == "f":
                        assert not release.is_set()
                        release.set()
        assert ids == ["f", "s"]
    asyncio.run(scenario())


def test_parallel_capability_cannot_opt_in_side_effecting_tools():
    with pytest.raises(ValueError, match="parallel_safe"):
        ToolRegistry().register(ToolDef("write", "write", {}, lambda: "", risk="write", idempotent=True, parallel_safe=True))


def test_parallel_network_calls_do_not_cross_ordered_operation_barriers(tmp_path):
    async def scenario():
        registry = ToolRegistry(artifact_dir=tmp_path)
        order = []
        async def before():
            order.append("before")
            return "ok"
        async def search():
            assert order == ["before"]
            order.append("search")
            return "ok"
        async def after():
            assert order == ["before", "search"]
            order.append("after")
            return "ok"
        for name, fn, parallel in [("before", before, False), ("search", search, True), ("after", after, False)]:
            registry.register(ToolDef(name, name, {"type": "object"}, fn, risk="network", idempotent=True, parallel_safe=parallel))
        result = await make_agent(registry)._execute_tool_calls([call("before", "1"), call("search", "2"), call("after", "3")])
        assert order == ["before", "search", "after"]
        assert all(not event["error"] for event in result)
    asyncio.run(scenario())
