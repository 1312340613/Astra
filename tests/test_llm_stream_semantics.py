import asyncio
from types import SimpleNamespace as NS

import pytest

from agent.runtime.llm import LLMConfig, LLMResponseError, OpenAICompatibleProvider
from agent.runtime.react import ReActAgent
from agent.runtime.context import AgentContext
from agent.runtime.tools.registry import ToolRegistry


def chunk(text=None, *, reasoning=None, finish=None, calls=None):
    return NS(choices=[NS(finish_reason=finish, delta=NS(content=text,
               reasoning_content=reasoning, tool_calls=calls))], usage=None)


def call(index, name=None, identity=None, args=None):
    return NS(index=index, id=identity, function=NS(name=name, arguments=args))


class Stream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = next(self.chunks, None)
        if item is None:
            raise StopAsyncIteration
        return item

    async def aclose(self):
        self.closed = True


def provider(*responses):
    streams = [Stream(response) for response in responses]
    requests = []
    llm = object.__new__(OpenAICompatibleProvider)
    llm.config = LLMConfig(overall_timeout=2, max_retries=0)

    async def create(kwargs):
        requests.append(kwargs)
        return streams[len(requests) - 1]

    llm._create_completion = create
    return llm, streams, requests


def collect(llm):
    async def run():
        return [event async for event in llm.chat_stream([{"role": "user", "content": "do it"}])]
    return asyncio.run(run())


@pytest.mark.parametrize("reasoning", [None, "only a draft"])
def test_one_visible_recovery_for_empty_or_reasoning_only(reasoning):
    llm, streams, requests = provider([chunk(reasoning=reasoning, finish="stop")], [chunk("answer", finish="stop")])
    events = collect(llm)
    assert events[-1]["content"] == "answer"
    assert sum(e["type"] == "generation_recovery" for e in events) == 1
    assert len(requests) == 2
    assert "previous response ended" in requests[1]["messages"][-1]["content"]
    assert all(stream.closed for stream in streams)


def test_semantic_recovery_cannot_loop():
    llm, streams, requests = provider([chunk(finish="stop")], [chunk(reasoning="draft", finish="stop")])
    with pytest.raises(LLMResponseError, match="reasoning_only_response"):
        collect(llm)
    assert len(requests) == 2
    assert all(stream.closed for stream in streams)


def test_recovery_usage_counts_both_requests_without_inflating_context_size():
    first = chunk(finish="stop")
    first.usage = NS(prompt_tokens=100, completion_tokens=50, total_tokens=150)
    last = chunk("answer", finish="stop")
    last.usage = NS(prompt_tokens=120, completion_tokens=10, total_tokens=130)
    llm, _, _ = provider([first], [last])
    usage = collect(llm)[-1]["usage"]
    context = AgentContext()
    context.add_usage(usage)
    assert context.total_prompt_tokens == 220
    assert context.total_completion_tokens == 60
    assert context.last_prompt_tokens == 120


@pytest.mark.parametrize("response,code", [
    ([chunk("partial")], "stream_closed"),
    ([chunk("partial", finish="length")], "response_truncated"),
    ([chunk(calls=[call(0, "write_file", "id", '{"path":"x"}')])], "stream_closed"),
    ([chunk(calls=[call(0, "one", "a", "{}")]), chunk(calls=[call(0, "two", "a")], finish="tool_calls")], "conflicting_tool_identity"),
])
def test_incomplete_or_conflicting_stream_is_not_replayed(response, code):
    llm, streams, requests = provider(response)
    with pytest.raises(LLMResponseError, match=code):
        collect(llm)
    assert len(requests) == 1
    assert streams[0].closed


def test_interleaved_tools_null_identity_and_duplicate_terminal_frames():
    terminal = chunk(calls=[call(0, "", "", "1}"), call(1, None, None, '"yes"}')], finish="tool_calls")
    llm, _, _ = provider([
        chunk(calls=[call(1, "second", "b", '{"v":')]),
        chunk(calls=[call(0, "first", "a", '{"n":')]), terminal, terminal,
    ])
    final = collect(llm)[-1]
    assert final["calls"] == [
        {"name": "first", "id": "a", "arguments": '{"n":1}'},
        {"name": "second", "id": "b", "arguments": '{"v":"yes"}'},
    ]


def test_terminal_signal_does_not_wait_for_a_stuck_socket():
    class TrailingStall(Stream):
        async def __anext__(self):
            item = next(self.chunks, None)
            if item is not None:
                return item
            await asyncio.sleep(10)
            raise StopAsyncIteration

    llm, _, _ = provider([])
    stream = TrailingStall([chunk("answer", finish="stop")])
    llm.config.idle_timeout = 0.02
    async def create(kwargs):
        return stream
    llm._create_completion = create
    assert collect(llm)[-1]["content"] == "answer"
    assert stream.closed


def test_nested_incomplete_json_cannot_be_repaired_into_a_different_call():
    agent = object.__new__(ReActAgent)
    agent.tools = ToolRegistry()
    agent.context = AgentContext()
    calls, failure = agent._validated_tool_calls([
        {"id": "one", "name": "write_file", "arguments": '{"config":{"valid":true},"path":'}
    ], "tool_calls", None)
    assert calls is None
    assert failure and failure.code == "invalid_arguments"


@pytest.mark.parametrize("finish,calls,code", [
    ("length", [{"name": "write_file", "id": "one", "arguments": "{}"}], "tool_call_truncated"),
    ("tool_calls", [{"name": "write_file", "id": "one", "arguments": "{}"}] * 2, "invalid_arguments"),
])
def test_invalid_tool_batches_never_reach_execution(finish, calls, code):
    agent = object.__new__(ReActAgent)
    agent.tools = ToolRegistry()
    agent.context = AgentContext()
    validated, failure = agent._validated_tool_calls(calls, finish, None)
    assert not validated
    assert failure and failure.code == code
