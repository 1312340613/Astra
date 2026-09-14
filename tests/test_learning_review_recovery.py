import asyncio
import json

import pytest

from agent.cli.backend import _learning_review_error_message, _write_learning_review_error
from agent.runtime.learning import LearningReviewer, LearningReviewResultError, LearningStore
from agent.runtime.memory import MemoryStore
from agent.runtime.skills import SkillStore


class SequenceLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def chat_limited(self, messages, **options):
        self.calls.append((messages, options))
        return next(self.responses)


def reviewer_for(tmp_path, llm):
    return LearningReviewer(llm, LearningStore(tmp_path / "learning.db"),
                            MemoryStore(tmp_path / "memory.db"), SkillStore(tmp_path / "skills"))


MESSAGES = [{"role": "user", "content": "I prefer short replies."}]
VALID = {"content": json.dumps({"summary": "No new learning.", "proposals": []}), "finish_reason": "stop"}


@pytest.mark.parametrize("bad,code,budget", [
    ({"content": "not JSON SENSITIVE_SENTINEL"}, "invalid_json", 2048),
    ({"content": '{"summary":"missing array"}'}, "invalid_shape", 2048),
    ({"content": '{"summary":"cut off', "finish_reason": "length"}, "truncated_response", 4096),
    ({"content": VALID["content"], "finish_reason": "length"}, "truncated_response", 4096),
])
def test_one_precise_repair_reuses_evidence_without_echoing_bad_output(tmp_path, monkeypatch, bad, code, budget):
    monkeypatch.setenv("LEARNING_REVIEW_MAX_TOKENS", "2048")
    llm = SequenceLLM([bad, VALID])
    reviewer = reviewer_for(tmp_path, llm)
    outcome = asyncio.run(reviewer.review_outcome(MESSAGES, "session"))
    assert outcome["proposals"] == []
    assert len(llm.calls) == 2
    correction = llm.calls[1][0][-1]["content"]
    assert f"Result validation failed: {code}" in correction
    assert "SENSITIVE_SENTINEL" not in correction
    assert llm.calls[1][1]["max_tokens"] == budget
    assert llm.calls[1][1]["request_timeout"] <= llm.calls[0][1]["request_timeout"]
    assert reviewer.lifecycle.new_review_sources("session", MESSAGES) == []


def test_failed_repair_is_not_a_timeout_and_preserves_evidence(tmp_path, caplog, capsys):
    llm = SequenceLLM([{"content": "SENSITIVE_SENTINEL"}] * 2)
    reviewer = reviewer_for(tmp_path, llm)
    with pytest.raises(LearningReviewResultError) as caught:
        asyncio.run(reviewer.review_outcome(MESSAGES, "session"))
    assert len(llm.calls) == 2
    assert reviewer.lifecycle.new_review_sources("session", MESSAGES)
    assert reviewer.store.recent_review_summaries() == []
    _write_learning_review_error(caught.value, 360)
    output = caplog.text + capsys.readouterr().err
    assert "invalid_json" in output and "attempt=2" in output
    assert "SENSITIVE_SENTINEL" not in output
    assert "timeout=360s" not in output
    assert "Provider request failed" not in output


@pytest.mark.parametrize("cancel", [False, True])
def test_review_deadline_and_foreground_cancellation_close_the_provider(tmp_path, monkeypatch, cancel):
    monkeypatch.setenv("LEARNING_REVIEW_TIMEOUT", "0.04")

    async def scenario():
        started = asyncio.Event()
        closed = asyncio.Event()

        class WaitingLLM:
            async def chat_limited(self, messages, **options):
                try:
                    started.set()
                    await asyncio.Event().wait()
                finally:
                    closed.set()

        reviewer = reviewer_for(tmp_path, WaitingLLM())
        task = asyncio.create_task(reviewer.review_outcome(MESSAGES, "session"))
        await started.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError) as caught:
            await task
        assert closed.is_set()
        assert reviewer.lifecycle.new_review_sources("session", MESSAGES)
        if not cancel:
            assert "timed out" in _learning_review_error_message(caught.value, 0.04)

    asyncio.run(asyncio.wait_for(scenario(), 2))
