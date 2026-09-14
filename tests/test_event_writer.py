import asyncio
import threading

import pytest

from agent.runtime.event_stream import RuntimeEventStream
from agent.runtime.event_writer import OrderedEventWriter, EventQueueFull


def test_slow_event_persistence_does_not_block_loop_and_replay_stays_ordered(tmp_path):
    async def scenario():
        stream = RuntimeEventStream(tmp_path / "events.db")
        started, release = threading.Event(), threading.Event()
        output = []

        def delay(stage):
            if stage == "before_commit":
                started.set()
                assert release.wait(2)

        stream._fault_hook = delay
        writer = OrderedEventWriter(stream, output.append)
        try:
            writer.send({"type": "tool_calls", "calls": [{"id": "c1", "name": "read_file", "arguments": "SECRET"}]})
            assert await asyncio.to_thread(started.wait, 1)
            writer.send({"type": "chunk", "content": "live only"})
            writer.replay(0, limit=500)
            writer.send({"type": "done"})
            await asyncio.sleep(0.01)
            assert output == []  # Publish still waits for commit; the loop does not.
        finally:
            release.set()
            await writer.close()
        assert [event["type"] for event in output] == [
            "tool_calls", "chunk", "tool_calls", "event_replay_complete", "done",
        ]
        assert output[0]["event_id"] == output[2]["event_id"]
        assert output[2]["replayed"] is True
        assert "arguments" not in output[2]["calls"][0]
        assert output[3]["next_cursor"] == output[0]["cursor"]
        assert output[4]["cursor"] > output[0]["cursor"]
        assert not writer.alive

    asyncio.run(scenario())


def test_event_queue_is_bounded_and_async_producers_wait(tmp_path):
    async def scenario():
        started, release = threading.Event(), threading.Event()
        output = []

        def write(event):
            started.set()
            assert release.wait(2)
            output.append(event)

        writer = OrderedEventWriter(None, write, max_items=2, max_bytes=4096)
        writer.send({"type": "chunk", "content": "1"})
        assert await asyncio.to_thread(started.wait, 1)
        writer.send({"type": "chunk", "content": "2"})
        writer.send({"type": "chunk", "content": "3"})
        with pytest.raises(EventQueueFull):
            writer.send({"type": "done"})
        pending = asyncio.create_task(writer.send_async({"type": "done"}))
        await asyncio.sleep(0.01)
        assert not pending.done()
        release.set()
        await pending
        await writer.close()
        assert [e.get("content", e["type"]) for e in output] == ["1", "2", "3", "done"]

    asyncio.run(scenario())


def test_event_snapshot_and_writer_failure_are_explicit():
    async def scenario():
        output = []
        writer = OrderedEventWriter(None, output.append)
        event = {"type": "tool_calls", "calls": [{"id": "original"}]}
        writer.send(event)
        event["calls"][0]["id"] = "mutated"
        await writer.close()
        assert output[0]["calls"][0]["id"] == "original"
        with pytest.raises(RuntimeError, match="closed"):
            writer.send({"type": "done"})

        def broken(_event):
            raise BrokenPipeError("reader closed")

        writer = OrderedEventWriter(None, broken)
        writer.send({"type": "done"})
        with pytest.raises(BrokenPipeError):
            await writer.close()
        assert not writer.alive

    asyncio.run(scenario())


def test_oversized_event_fails_without_waiting_forever():
    async def scenario():
        writer = OrderedEventWriter(None, lambda event: None, max_bytes=32)
        try:
            with pytest.raises(EventQueueFull):
                await writer.send_async({"type": "chunk", "content": "x" * 100})
        finally:
            await writer.close()

    asyncio.run(scenario())


def test_failed_persistence_does_not_invent_cursor_or_block_later_live_events(tmp_path):
    async def scenario():
        stream = RuntimeEventStream(tmp_path / "events.db")
        output = []

        def fault(stage):
            if stage == "before_commit":
                stream._fault_hook = None
                raise OSError("injected disk failure")

        stream._fault_hook = fault
        writer = OrderedEventWriter(stream, output.append)
        writer.send({"type": "tool_progress", "stage": "running"})
        writer.send({"type": "done"})
        await writer.close()
        assert output[0]["replay_unavailable"] is True
        assert "cursor" not in output[0]
        assert output[1]["replayable"] is True
        assert [event["type"] for event in stream.replay(0)] == ["done"]

    asyncio.run(scenario())

