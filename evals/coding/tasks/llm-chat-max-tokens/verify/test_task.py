"""Judge for the llm-chat-max-tokens task.

Runs against the task workspace code (checked out at the start commit)
after the agent has made its fix. Invisible to the agent.
"""

import asyncio

from agent.runtime.llm import LLMClient, LLMConfig


class FakeProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat(self, messages, tools=None, tool_choice=None, max_tokens=None):
        self.calls.append({"max_tokens": max_tokens})
        return {"content": "ok"}

    async def chat_stream(self, messages, tools=None, tool_choice=None, omit_tool_choice=False, generation_overrides=None):
        yield {"type": "done", "content": "", "usage": None}


def _client(provider: FakeProvider) -> LLMClient:
    return LLMClient(LLMConfig(model="fake", max_tokens=4096), provider=provider)


def test_chat_forwards_max_tokens():
    provider = FakeProvider()
    client = _client(provider)
    asyncio.run(client.chat([{"role": "user", "content": "hi"}], max_tokens=16384))
    assert provider.calls == [{"max_tokens": 16384}]


def test_chat_without_max_tokens_keeps_default():
    provider = FakeProvider()
    client = _client(provider)
    asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    assert provider.calls == [{"max_tokens": None}]


def test_chat_still_forwards_tool_choice():
    provider = FakeProvider()
    client = _client(provider)
    asyncio.run(client.chat(
        [{"role": "user", "content": "hi"}],
        tool_choice={"type": "function", "function": {"name": "lookup"}},
    ))
    assert provider.calls == [{"max_tokens": None}]
