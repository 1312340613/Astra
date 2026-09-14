import asyncio
from types import MethodType, SimpleNamespace

import pytest

from agent.runtime.message_time import (
    LeadingMessageTimeFilter,
    filter_message_time_events,
    strip_leading_message_time_marker,
)
from agent.runtime.react import ReActAgent


MARKER = "<message_time>2026-08-24T20:42:23+08:00</message_time>"
WEEKDAY_MARKER = "<message_time>2026-08-24T20:42:23+08:00 周一</message_time>"


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("marker", [MARKER, WEEKDAY_MARKER])
def test_strip_only_valid_leading_marker(marker):
    assert strip_leading_message_time_marker(f"{marker}\nanswer") == "answer"
    assert strip_leading_message_time_marker(f"answer {marker}") == f"answer {marker}"
    assert strip_leading_message_time_marker(
        "<message_time>example</message_time>\nanswer"
    ) == "<message_time>example</message_time>\nanswer"
    malformed = "<message_time>2026-09-14T00:00:00+08:00 周八</message_time>\nanswer"
    assert strip_leading_message_time_marker(malformed) == malformed


@pytest.mark.parametrize("split", range(1, len(WEEKDAY_MARKER) + 2))
def test_weekday_marker_is_hidden_at_every_stream_boundary(split):
    boundary = LeadingMessageTimeFilter()
    source = WEEKDAY_MARKER + "\nanswer"
    assert boundary.feed(source[:split]) + boundary.feed(source[split:]) + boundary.finish() == "answer"


def test_boundary_filter_handles_split_marker_and_newline():
    boundary = LeadingMessageTimeFilter()

    assert boundary.feed("<message_") == ""
    assert boundary.feed("time>2026-08-24T20:42:23+08:00</message_time>") == ""
    assert boundary.feed("\nhel") == "hel"
    assert boundary.feed("lo") == "lo"
    assert boundary.finish() == ""


def test_boundary_filter_releases_incomplete_prefix_at_end():
    boundary = LeadingMessageTimeFilter()

    assert boundary.feed("<message_") == ""
    assert boundary.finish() == "<message_"


@pytest.mark.parametrize("marker", [MARKER, WEEKDAY_MARKER])
def test_event_filter_cleans_chunks_and_complete_payload_and_closes_source(marker):
    closed = []

    async def source():
        try:
            yield {"type": "chunk", "content": marker[:9]}
            yield {
                "type": "chunk",
                "content": marker[9:] + "\nanswer",
            }
            yield {"type": "done", "content": f"{marker}\nanswer"}
        finally:
            closed.append(True)

    async def collect():
        return [event async for event in filter_message_time_events(source())]

    events = run(collect())

    assert [event["content"] for event in events if event["type"] == "chunk"] == [
        "answer"
    ]
    assert events[-1]["content"] == "answer"
    assert closed == [True]


def test_react_llm_stream_applies_message_time_filter():
    async def source():
        yield {"type": "chunk", "content": f"{MARKER}\nanswer"}
        yield {"type": "done", "content": f"{MARKER}\nanswer"}

    agent = object.__new__(ReActAgent)
    agent.context = SimpleNamespace(messages=[])
    agent.llm = SimpleNamespace(chat_stream=lambda **_kwargs: source())
    agent._vision_tile_tool_enabled = MethodType(lambda _self: False, agent)
    agent._generation_overrides = MethodType(lambda _self: None, agent)

    async def collect():
        return [
            event
            async for event in agent._llm_stream(messages=[], tools=[])
        ]

    events = run(collect())

    assert [event for event in events if event["type"] != "generation_progress"] == [
        {"type": "chunk", "content": "answer"},
        {"type": "done", "content": "answer"},
    ]
    assert events[0]["phase"] == "requesting"
    assert events[-1]["phase"] == "finished"
