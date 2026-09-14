import asyncio
from contextvars import ContextVar

import pytest

from agent.runtime.stream_progress import stream_with_progress


def test_provider_context_and_cleanup_stay_in_one_task():
    scope = ContextVar("provider_scope", default="caller")
    closed = []

    async def source():
        token = scope.set("provider")
        try:
            yield {"type": "reasoning", "content": "first"}
            assert scope.get() == "provider"
            yield {"type": "done"}
        finally:
            scope.reset(token)
            closed.append(True)

    async def scenario():
        async for _ in stream_with_progress(source()):
            assert scope.get() == "caller"
        assert closed == [True]

    asyncio.run(scenario())


def test_waiting_updates_keep_one_pending_read_and_resume_normally():
    async def scenario():
        release = asyncio.Event()
        closed = asyncio.Event()
        reads = 0

        async def source():
            nonlocal reads
            try:
                reads += 1
                await release.wait()
                yield {"type": "reasoning", "content": "working"}
                yield {"type": "done"}
            finally:
                closed.set()

        seen = []
        async for event in stream_with_progress(source(), interval=0.01, quiet_after=0.01):
            seen.append(event)
            if event.get("phase") == "waiting":
                assert reads == 1
                assert not closed.is_set(), "display updates must not cancel the provider read"
                release.set()
        assert closed.is_set()
        phases = [event["phase"] for event in seen if event["type"] == "generation_progress"]
        assert phases[0] == "requesting"
        assert "waiting" in phases
        assert phases[-2:] == ["streaming", "finished"]
        assert [event["type"] for event in seen if event["type"] != "generation_progress"] == ["reasoning", "done"]

    asyncio.run(asyncio.wait_for(scenario(), 2))


def test_progress_cancellation_closes_the_pending_provider_read():
    async def scenario():
        waiting = asyncio.Event()
        closed = asyncio.Event()

        async def source():
            try:
                waiting.set()
                await asyncio.Event().wait()
                yield {"type": "done"}
            finally:
                closed.set()

        async def consume():
            async for _ in stream_with_progress(source(), interval=0.01):
                pass

        task = asyncio.create_task(consume())
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()

    asyncio.run(asyncio.wait_for(scenario(), 2))


def test_empty_events_do_not_count_as_output_or_hide_stream_failure():
    async def scenario():
        seen = []

        async def source():
            yield {"type": "reasoning", "content": ""}
            await asyncio.sleep(0.025)
            raise RuntimeError("transport stopped")

        with pytest.raises(RuntimeError, match="transport stopped"):
            async for event in stream_with_progress(source(), interval=0.005, quiet_after=0.01):
                seen.append(event)
        phases = [event.get("phase") for event in seen]
        assert "waiting" in phases
        assert "streaming" not in phases
        assert "finished" not in phases

    asyncio.run(scenario())


def test_provider_initiated_cancellation_reaches_the_consumer():
    async def source():
        yield {"type": "chunk", "content": "partial"}
        raise asyncio.CancelledError()

    async def scenario():
        seen = []
        with pytest.raises(asyncio.CancelledError):
            async for event in stream_with_progress(source(), interval=0.005):
                seen.append(event)
        assert any(event.get("content") == "partial" for event in seen)
        assert not any(event.get("phase") == "finished" for event in seen)

    asyncio.run(asyncio.wait_for(scenario(), 2))
