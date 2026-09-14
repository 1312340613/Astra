import asyncio
import threading

import pytest

from agent.runtime.async_io import durable_io
from agent.runtime.context import AgentContext


def test_cancel_waits_for_write_and_keeps_loop_responsive():
    async def scenario():
        started, release = threading.Event(), threading.Event()
        committed = []

        def write():
            started.set()
            assert release.wait(2)
            committed.append("saved")

        task = asyncio.create_task(durable_io(write))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()  # Shutdown can cancel an already-cancelling turn.
            await asyncio.sleep(0.01)
            assert not task.done()
            assert not committed
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert committed == ["saved"]

    asyncio.run(scenario())


def test_io_result_and_failure_are_not_lost():
    async def scenario():
        assert await durable_io(lambda *, value: value, value=3) == 3

        def fail():
            raise OSError("disk failure")

        with pytest.raises(OSError, match="disk failure"):
            await durable_io(fail)

    asyncio.run(scenario())


def test_asyncio_shutdown_drains_write_before_owner_cleanup():
    started = threading.Event()
    order = []

    def write():
        started.set()
        threading.Event().wait(0.05)
        order.append("committed")

    async def owner():
        try:
            await durable_io(write)
        finally:
            order.append("owner cleanup")

    async def scenario():
        asyncio.create_task(owner())
        assert await asyncio.to_thread(started.wait, 1)
        # Return with the owner still running, invoking asyncio.run's global
        # cancellation sweep rather than manually cancelling just the owner.

    asyncio.run(scenario())
    assert order == ["committed", "owner cleanup"]


def test_cancellation_drains_failing_write_without_unretrieved_exception():
    async def scenario():
        started, release = threading.Event(), threading.Event()

        def fail():
            started.set()
            assert release.wait(2)
            raise OSError("disk failure after cancel")

        task = asyncio.create_task(durable_io(fail))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_session_save_uses_snapshot_and_cancel_does_not_duplicate_appends(tmp_path):
    async def scenario():
        context = AgentContext()
        context.set_session(str(tmp_path / "session.json"))
        context.add_user("original")
        store = context._session_store
        original_save = store.save
        started, release = threading.Event(), threading.Event()

        def slow_save(data, **kwargs):
            started.set()
            assert release.wait(2)
            original_save(data, **kwargs)

        store.save = slow_save
        task = asyncio.create_task(context.save_async())
        try:
            assert await asyncio.to_thread(started.wait, 1)
            context.messages[0]["content"] = "changed after snapshot"
            task.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.load()["messages"][0]["content"] == "original"
        assert context._saved_message_count == 1
        # Rewriting an already-saved message is explicit (as in undo/retry).
        context._saved_message_count = 0
        await context.save_async()
        assert [m["content"] for m in store.load()["messages"]] == ["changed after snapshot"]
        context.messages = []
        await context.save_async(allow_empty=True)
        assert store.load()["messages"] == []

    asyncio.run(scenario())


def test_cancel_during_tool_claim_never_invokes_or_replays_write(tmp_path):
    from agent.runtime.task_store import TaskStore
    from agent.runtime.tools.registry import ToolDef, ToolRegistry
    from test_search_responsiveness import call, make_agent

    async def scenario():
        store = TaskStore(tmp_path / "tasks.db")
        run = store.start_run("claim-test", "fixture")
        started, release = threading.Event(), threading.Event()
        original_claim = store.claim_tool
        invoked = []

        def blocked_claim(*args, **kwargs):
            started.set()
            assert release.wait(2)
            return original_claim(*args, **kwargs)

        async def write():
            invoked.append(True)
            return "written"

        store.claim_tool = blocked_claim
        registry = ToolRegistry(artifact_dir=tmp_path / "artifacts")
        registry.register(ToolDef("write", "write", {"type": "object"}, write, risk="write", idempotent=False))
        agent = make_agent(registry)
        agent.task_store = store
        pending = asyncio.create_task(agent._execute_tool_call(call("write", "claim"), task_id=run["id"]))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            pending.cancel()
            await asyncio.sleep(0.01)
            assert not pending.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await durable_io(store.finish_run, run["id"], "cancelled")
        assert not invoked
        restored = TaskStore(store.path)
        task = restored.get_task(run["id"])
        assert task["steps"][0]["status"] == "unknown"
        claim = restored.claim_tool(run["id"], "write", {}, "write", invocation_id="claim")
        assert claim.action == "uncertain"

    asyncio.run(scenario())
