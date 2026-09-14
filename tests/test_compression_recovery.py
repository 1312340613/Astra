"""Tests for compression failure-safety and provider overflow recovery.

Complements test_compression_protection.py for long coding sessions:
- a failed summary must not silently drop history on the opportunistic path
- the pre-LLM prompt path gets the same forced-compaction retry as the
  post-tool path
- provider context-overflow errors trigger one forced compaction + retry
"""

import asyncio

import pytest

from agent.core.msg import Msg, ContentBlock
from agent.runtime.context_compressor import ContextCompressor
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


class ExplodingLLM:
    async def chat(self, messages, **kwargs):
        raise RuntimeError("provider down")

    async def chat_limited(self, messages, *, max_tokens, **kwargs):
        raise RuntimeError("provider down")


def _history():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "第一个请求：调研压缩机制"},
        {"role": "assistant", "content": "已完成调研"},
        {"role": "user", "content": "第二个请求：实现修复"},
        {"role": "assistant", "content": "正在实现"},
        {"role": "user", "content": "最新请求：继续"},
    ]


class TestNonForcedFailureKeepsHistory:
    def test_non_forced_summary_failure_returns_messages_unchanged(self):
        compressor = ContextCompressor(ExplodingLLM())
        messages = _history()
        result = run(compressor.compress(messages, max_prompt_tokens=10_000))
        assert result is messages

    def test_forced_summary_failure_still_frees_space(self):
        compressor = ContextCompressor(ExplodingLLM())
        messages = _history()
        result = run(compressor.compress(messages, max_prompt_tokens=10_000, force=True))
        assert result is not messages
        assert len(result) < len(messages)
        text = " ".join(str(m.get("content", "")) for m in result)
        assert "Summary unavailable" in text
        assert "最新请求" in text

    def test_forced_retry_after_non_forced_failure(self):
        compressor = ContextCompressor(ExplodingLLM())
        messages = _history()
        first = run(compressor.compress(messages, max_prompt_tokens=10_000))
        assert first is messages  # nothing silently dropped
        second = run(compressor.compress(messages, max_prompt_tokens=10_000, force=True))
        # Forced path clears the failure cooldown and still frees space.
        assert len(second) < len(messages)


class TestOverflowClassification:
    @pytest.mark.parametrize("text", [
        "Error code: 400 - {'error': {'message': \"This model's maximum "
        "context length is 128000 tokens.\", 'code': 'context_length_exceeded'}}",
        "prompt is too long: 200001 tokens > 200000 maximum",
        "context length exceeded, please reduce the input",
        "input too long for requested model",
        "too many tokens in the request",
        "The input exceeds the model's context window",
    ])
    def test_detects_overflow(self, text):
        assert ReActAgent._is_context_overflow_error(RuntimeError(text))

    @pytest.mark.parametrize("text", [
        "invalid api key",
        "rate limit exceeded, retry later",
        "too many tokens per minute for this account",
        "tool execution failed: file not found",
        "connection timeout",
    ])
    def test_rejects_non_overflow(self, text):
        assert not ReActAgent._is_context_overflow_error(RuntimeError(text))

    def test_rejects_non_exception(self):
        assert not ReActAgent._is_context_overflow_error(asyncio.CancelledError())


class TestPreparePromptForcedRetry:
    def test_pre_llm_path_forces_compaction_when_opportunistic_noop(self):
        class AlwaysOverLLM(ExplodingLLM):
            def estimate_tokens(self, messages):
                return 10_000_000  # always over budget

        async def scenario():
            agent = ReActAgent("agent", AlwaysOverLLM(), ToolRegistry(), max_iterations=1)
            agent.context.max_prompt_tokens = 10_000
            for msg in _history()[1:]:
                agent.context.messages.append(dict(msg))
            before = len(agent.context.messages)

            prompt, estimate = await agent._prepare_prompt_for_llm(
                "最新请求：继续",
                agent.llm.estimate_tokens,
            )

            # Non-forced compaction was a no-op (summary backend down), the
            # forced retry still freed space with the protected fallback.
            assert len(agent.context.messages) < before
            text = " ".join(str(m.get("content", "")) for m in agent.context.messages)
            assert "Summary unavailable" in text
            assert prompt  # rebuilt prompt returned for the LLM call

        run(scenario())


class TestOverflowReactiveRecovery:
    def test_overflow_error_triggers_compaction_and_retry(self):
        class OverflowThenOkLLM:
            def __init__(self):
                self.calls = 0

            async def chat_limited(self, messages, *, max_tokens, **kwargs):
                raise RuntimeError("summary backend down")

            async def chat_stream(self, messages, tools):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError(
                        "Error code: 400 - {'error': {'message': \"This model's maximum "
                        "context length is 100 tokens.\", 'code': 'context_length_exceeded'}}"
                    )
                yield {"type": "done", "content": "recovered", "usage": None}

        async def scenario():
            llm = OverflowThenOkLLM()
            agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=3)
            for msg in _history()[1:]:
                agent.context.messages.append(dict(msg))
            before = len(agent.context.messages)

            events = [
                event
                async for event in agent.reply_stream(Msg(content=[ContentBlock.text("继续")]))
            ]

            assert events[-1]["type"] == "done"
            assert not any(event.get("type") == "error" for event in events)
            assert llm.calls == 2  # one rejected call + one successful retry
            # Forced compaction shrank the history despite the added user turn.
            assert len(agent.context.messages) < before + 1

        run(scenario())

    def test_token_shrink_retries_even_when_message_count_is_unchanged(self):
        class SameCountCompressor:
            async def compress(self, messages, max_prompt_tokens, force=False):
                compressed = [dict(message) for message in messages]
                compressed[1]["content"] = "short"
                return compressed

        class OverflowThenOkLLM:
            def __init__(self):
                self.calls = 0

            async def chat_stream(self, messages, tools):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("context_length_exceeded")
                yield {"type": "done", "content": "recovered", "usage": None}

        async def scenario():
            llm = OverflowThenOkLLM()
            agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=3)
            agent.context.compressor = SameCountCompressor()
            agent.context.messages.extend([
                {"role": "user", "content": "x" * 20_000},
                {"role": "assistant", "content": "prior answer"},
            ])
            before_count = len(agent.context.messages)

            events = [
                event
                async for event in agent.reply_stream(Msg(content=[ContentBlock.text("继续")]))
            ]

            assert events[-1]["type"] == "done"
            assert llm.calls == 2
            assert len(agent.context.messages) == before_count + 2

        run(scenario())

    def test_non_overflow_error_still_propagates(self):
        class AlwaysFailingLLM:
            async def chat_limited(self, messages, *, max_tokens, **kwargs):
                raise RuntimeError("summary backend down")

            async def chat_stream(self, messages, tools):
                raise RuntimeError("invalid api key")
                yield  # pragma: no cover

        async def scenario():
            agent = ReActAgent("agent", AlwaysFailingLLM(), ToolRegistry(), max_iterations=2)
            agent.context.messages.append({"role": "user", "content": "你好"})
            with pytest.raises(RuntimeError, match="invalid api key"):
                [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("你好")]))]

        run(scenario())
