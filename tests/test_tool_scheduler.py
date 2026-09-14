import asyncio

import pytest

from agent.runtime.tool_scheduler import execute_ordered
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from test_search_responsiveness import call, make_agent


def test_reads_observe_the_correct_version_across_write_barriers(tmp_path):
    async def scenario():
        path = tmp_path / "state.txt"
        path.write_text("before")
        registry = ToolRegistry(artifact_dir=tmp_path / "results")

        async def read():
            return path.read_text()

        async def write():
            path.write_text("after")
            return "written"

        for name, fn, risk in [("read", read, "read"), ("write", write, "write")]:
            registry.register(ToolDef(name, name, {"type": "object"}, fn, risk=risk))
        events = await make_agent(registry)._execute_tool_calls([
            call("read", "before"), call("write", "change"), call("read", "after"),
        ])
        assert [event["output"] for event in events] == ["before", "written", "after"]
        assert path.read_text() == "after"
    asyncio.run(scenario())


def test_queued_tool_reclassified_as_exclusive_waits_for_the_pool_to_drain():
    async def scenario():
        release = asyncio.Event()
        both = asyncio.Event()
        running = set()
        order = []
        exclusive = set()

        async def execute(index):
            if index == 2:
                assert not running
            running.add(index)
            order.append(index)
            if index == 0:
                await release.wait()
            if index == 1:
                exclusive.add(2)
                both.set()
            running.remove(index)
            return index

        task = asyncio.create_task(execute_ordered(list(range(4)), execute, lambda index: index not in exclusive, 2))
        await asyncio.wait_for(both.wait(), 1)
        await asyncio.sleep(0)
        assert order == [0, 1]
        release.set()
        assert await task == [0, 1, 2, 3]
        assert order == [0, 1, 2, 3]
    asyncio.run(scenario())


def test_scheduler_failure_drains_started_calls_and_never_starts_the_barrier():
    async def scenario():
        running = asyncio.Event()
        stopped = asyncio.Event()
        order = []

        async def execute(index):
            order.append(index)
            if index == 0:
                running.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            if index == 1:
                await running.wait()
                raise ValueError("injected scheduler failure")
            return index

        with pytest.raises(ValueError, match="injected"):
            await execute_ordered([0, 1, 2], execute, lambda index: index != 2, 2)
        assert stopped.is_set()
        assert order == [0, 1]
    asyncio.run(scenario())
