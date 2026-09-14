import asyncio
import time

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.runtime.react import ReActAgent, _stream_with_generation_stats
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def test_generation_stats_include_wait_and_reasoning_but_exclude_stream_cleanup(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(time, "perf_counter", lambda: clock[0])

    async def source():
        try:
            clock[0] += 3  # connection and first-token wait
            yield {"type": "reasoning", "content": "hidden thinking"}
            clock[0] += 2
            yield {
                "type": "tool_calls", "calls": [],
                "usage": {"completion_tokens": 100,
                          "completion_tokens_details": {"reasoning_tokens": 80}},
            }
        finally:
            clock[0] += 7  # teardown is after the completed request

    async def collect():
        return [event async for event in _stream_with_generation_stats(source())]

    events = asyncio.run(collect())
    assert events[-1] == {
        "type": "generation_stats", "completion_tokens": 100,
        "elapsed_seconds": 5.0, "tokens_per_second": 20.0,
    }


@pytest.mark.parametrize("usage", [
    None, {}, {"prompt_tokens": 20}, {"completion_tokens": None},
    {"completion_tokens": 0}, {"completion_tokens": -5},
    {"completion_tokens": True}, {"completion_tokens": "unknown"},
])
def test_generation_stats_do_not_invent_missing_output_counts(usage):
    async def source():
        yield {"type": "done", "content": "text is not a token count", "usage": usage}

    async def collect():
        return [event async for event in _stream_with_generation_stats(source())]

    assert [event["type"] for event in asyncio.run(collect())] == ["done"]


@pytest.mark.parametrize("elapsed", [0, -1, float("inf"), float("nan")])
def test_generation_stats_require_finite_positive_elapsed(elapsed, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(time, "perf_counter", lambda: clock[0])

    async def source():
        clock[0] += elapsed
        yield {"type": "done", "usage": {"completion_tokens": 25}}

    async def collect():
        return [event async for event in _stream_with_generation_stats(source())]

    assert [event["type"] for event in asyncio.run(collect())] == ["done"]


def test_generation_stats_not_emitted_for_a_failed_stream():
    seen = []

    async def source():
        yield {"type": "chunk", "content": "partial"}
        raise RuntimeError("connection lost")

    async def collect():
        async for event in _stream_with_generation_stats(source()):
            seen.append(event)

    with pytest.raises(RuntimeError, match="connection lost"):
        asyncio.run(collect())
    assert [event["type"] for event in seen] == ["chunk"]


def test_react_emits_one_generation_measurement_per_request_without_tool_time(tmp_path, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(time, "perf_counter", lambda: clock[0])

    class FakeLLM:
        calls = 0

        async def chat_stream(self, messages, tools):
            self.calls += 1
            clock[0] += 2
            yield {"type": "reasoning", "content": "thinking"}
            clock[0] += 3
            if self.calls == 1:
                yield {
                    "type": "tool_calls", "content": "", "reasoning_content": "thinking",
                    "calls": [{"id": "slow-1", "name": "slow_tool", "arguments": "{}"}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 100},
                }
            else:
                yield {"type": "done", "content": "done", "usage": {
                    "prompt_tokens": 16, "completion_tokens": 50,
                }}

    async def slow_tool():
        clock[0] += 100
        return "result"

    async def scenario():
        registry = ToolRegistry()
        registry.register(ToolDef("slow_tool", "Read a result", {"type": "object"}, slow_tool))
        agent = ReActAgent("test", FakeLLM(), registry, max_iterations=2, timing_log_enabled=False)
        agent.context.set_session(str(tmp_path / "session.json"))
        agent.context.show_reasoning = False
        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("run")]))]
        assert agent.context.total_completion_tokens == 150
        return events

    events = asyncio.run(scenario())
    stats = [event for event in events if event["type"] == "generation_stats"]
    assert [(event["completion_tokens"], event["elapsed_seconds"], event["tokens_per_second"])
            for event in stats] == [(100, 5.0, 20.0), (50, 5.0, 10.0)]
    assert all(event.get("request_id") for event in stats)
    assert not any(event["type"] == "reasoning" for event in events)
    assert next(i for i, event in enumerate(events) if event["type"] == "generation_stats") < next(
        i for i, event in enumerate(events) if event["type"] == "tool_result")


def test_graceful_stop_synthesis_reports_its_own_request_stats(tmp_path, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(time, "perf_counter", lambda: clock[0])

    class FakeLLM:
        calls = 0

        async def chat_stream(self, messages, tools):
            self.calls += 1
            clock[0] += 2
            if self.calls == 1:
                yield {"type": "tool_calls", "content": "", "calls": [
                    {"id": "read-1", "name": "read_result", "arguments": "{}"},
                ], "usage": {"completion_tokens": 20}}
            else:
                yield {"type": "done", "content": "Result obtained.",
                       "usage": {"completion_tokens": 10}}

    async def read_result():
        return "result"

    async def scenario():
        registry = ToolRegistry()
        registry.register(ToolDef("read_result", "Read", {"type": "object"}, read_result))
        agent = ReActAgent("test", FakeLLM(), registry, max_iterations=1, timing_log_enabled=False)
        agent.context.set_session(str(tmp_path / "session.json"))
        return [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("run")]))]

    stats = [event for event in asyncio.run(scenario()) if event["type"] == "generation_stats"]
    assert [event["tokens_per_second"] for event in stats] == [10.0, 5.0]
