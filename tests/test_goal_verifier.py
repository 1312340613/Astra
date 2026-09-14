import asyncio

from agent.runtime.goal_verifier import (
    GoalVerifier,
    build_goal_resume_message,
    build_goal_start_message,
    build_transcript,
    decide_goal_continuation,
    goal_mode_enabled,
    parse_verdict,
    should_verify_goal_turn,
    verification_is_current,
    verdict_is_usable,
)


def test_goal_mode_enabled_default(monkeypatch):
    monkeypatch.delenv("GOAL_MODE_ENABLED", raising=False)
    assert goal_mode_enabled() is True
    monkeypatch.setenv("GOAL_MODE_ENABLED", "false")
    assert goal_mode_enabled() is False
    monkeypatch.setenv("GOAL_MODE_ENABLED", "on")
    assert goal_mode_enabled() is True


def test_build_transcript_bounds_and_order():
    messages = [
        {"role": "system", "content": "secret system prompt"},
        {"role": "user", "content": "fix the bug"},
        {"role": "assistant", "content": [{"type": "text", "text": "ran the tests"}]},
        {"role": "tool", "content": [{"output": "3 passed"}]},
        {"role": "assistant", "content": ""},
    ]
    transcript = build_transcript(messages)
    assert "secret system prompt" not in transcript
    assert "[user] fix the bug" in transcript
    assert "ran the tests" in transcript
    assert "3 passed" in transcript
    # most recent last
    assert transcript.index("fix the bug") < transcript.index("ran the tests")


def test_build_transcript_truncates_long_messages():
    messages = [{"role": "user", "content": "x" * 5000}, {"role": "assistant", "content": "done"}]
    transcript = build_transcript(messages)
    assert "…[truncated]" in transcript
    assert len(transcript) < 5000


def test_parse_verdict_valid_json():
    verdict = parse_verdict('Sure! {"met": true, "evidence": "pytest: 12 passed", "next_step": ""} thanks')
    assert verdict["met"] is True
    assert verdict["evidence"] == "pytest: 12 passed"
    assert verdict["parse_error"] is False


def test_parse_verdict_string_met_coerced():
    assert parse_verdict('{"met": "true", "evidence": "checks passed"}')["met"] is True
    assert parse_verdict('{"met": "nope"}')["met"] is False


def test_parse_verdict_rejects_non_boolean_met_values():
    for raw in ('{"met": 2}', '{"met": {"value": false}}', '{"met": []}', '{"met": null}'):
        verdict = parse_verdict(raw)
        assert verdict["met"] is False
        assert verdict["parse_error"] is True


def test_parse_verdict_garbage_is_conservative():
    verdict = parse_verdict("I think we are basically done!")
    assert verdict["met"] is False
    assert verdict["parse_error"] is True
    assert verdict["next_step"]


def test_parse_verdict_not_met_requires_next_step():
    verdict = parse_verdict('{"met": false}')
    assert verdict["met"] is False
    assert verdict["next_step"]


def test_decide_continuation_stops_on_terminal_or_met():
    verdict_not_met = {"met": False, "next_step": "run tests"}
    assert decide_goal_continuation({"status": "completed", "objective": "x"}, verdict_not_met) is None
    assert decide_goal_continuation({"status": "exhausted", "objective": "x"}, verdict_not_met) is None
    assert decide_goal_continuation({"status": "paused", "objective": "x"}, verdict_not_met) is None
    assert decide_goal_continuation(
        {"status": "active", "objective": "x"}, {"met": True}
    ) is None


def test_unusable_verdict_never_continues():
    goal = {"status": "active", "objective": "x"}
    for verdict in (
        {"met": False, "parse_error": True},
        {"met": False, "error": "provider down"},
    ):
        assert verdict_is_usable(verdict) is False
        assert decide_goal_continuation(goal, verdict) is None


def test_verification_fence_rejects_newer_turn_or_session_switch():
    assert verification_is_current(
        captured_session="s1",
        captured_generation=3,
        current_session="s1",
        current_generation=3,
    ) is True
    assert verification_is_current(
        captured_session="s1",
        captured_generation=3,
        current_session="s2",
        current_generation=3,
    ) is False
    assert verification_is_current(
        captured_session="s1",
        captured_generation=3,
        current_session="s1",
        current_generation=4,
    ) is False


def test_only_successful_completed_turns_are_verified():
    assert should_verify_goal_turn(
        turn_completed=True,
        was_cancelled=False,
        failed_message="",
    ) is True
    assert should_verify_goal_turn(
        turn_completed=False,
        was_cancelled=False,
        failed_message="",
    ) is False
    assert should_verify_goal_turn(
        turn_completed=True,
        was_cancelled=True,
        failed_message="",
    ) is False
    assert should_verify_goal_turn(
        turn_completed=True,
        was_cancelled=False,
        failed_message="provider failed",
    ) is False


def test_decide_continuation_builds_message():
    goal = {
        "status": "active",
        "objective": "make pytest pass",
        "criteria": "exit code 0",
        "round": 2,
        "max_rounds": 8,
    }
    text = decide_goal_continuation(goal, {"met": False, "evidence": "2 failing", "next_step": "fix import"})
    assert "[goal round 2/8" in text
    assert "make pytest pass" in text
    assert "Criteria: exit code 0" in text
    assert "NOT MET" in text
    assert "2 failing" in text
    assert "fix import" in text


def test_build_goal_resume_message_preserves_progress():
    text = build_goal_resume_message({
        "objective": "make pytest pass",
        "criteria": "exit code 0",
        "round": 2,
        "max_rounds": 8,
        "last_verdict": {
            "evidence": "2 failing",
            "next_step": "fix import",
        },
    })
    assert "[goal round 2/8 — resume]" in text
    assert "make pytest pass" in text
    assert "Criteria: exit code 0" in text
    assert "Evidence so far: 2 failing" in text
    assert "Verifier next step: fix import" in text
    assert "Do not restart the plan from scratch" in text
    assert "action=complete" in text


def test_build_goal_start_message_allows_active_completion_claim():
    text = build_goal_start_message({
        "objective": "mua",
        "criteria": "",
        "round": 0,
        "max_rounds": 8,
    })
    assert "Session goal: mua" in text
    assert "action=complete" in text
    assert "Do not invent extra work" in text


class _FakeLLM:
    def __init__(self, content: str):
        self._content = content
        self.calls = []

    async def chat_limited(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return {"content": self._content}


def test_goal_verifier_verify_parses_response():
    llm = _FakeLLM('{"met": false, "evidence": "no output", "next_step": "run it"}')
    verifier = GoalVerifier(llm)
    goal = {"objective": "do the thing", "criteria": "", "round": 0}
    messages = [{"role": "user", "content": "do the thing"}, {"role": "assistant", "content": "ok"}]
    verdict = asyncio.run(verifier.verify(goal, messages))
    assert verdict["met"] is False
    assert verdict["verifier"] == "goal-verifier"
    assert llm.calls and llm.calls[0][1]["max_tokens"] == 512


def test_goal_verifier_treats_delivered_one_shot_answer_as_evidence():
    llm = _FakeLLM('{"met": true, "evidence": "assistant delivered mua", "next_step": ""}')
    verifier = GoalVerifier(llm)
    verdict = asyncio.run(verifier.verify(
        {"objective": "mua", "criteria": "", "round": 0},
        [
            {"role": "user", "content": "Session goal: mua"},
            {"role": "assistant", "content": "mua~"},
            {"role": "tool", "content": "Completion claimed for the independent verifier."},
        ],
    ))
    system_prompt = llm.calls[0][0][0]["content"]
    assert verdict["met"] is True
    assert "delivered answer" in system_prompt
    assert "claim is a signal" in system_prompt
    assert "Never continue solely" in system_prompt


def test_goal_verifier_empty_transcript_skips_llm():
    llm = _FakeLLM("should not be called")
    verifier = GoalVerifier(llm)
    verdict = asyncio.run(verifier.verify({"objective": "x"}, []))
    assert verdict["met"] is False
    assert llm.calls == []


def test_goal_verifier_llm_error_is_non_blocking():
    class _BrokenLLM:
        async def chat_limited(self, prompt, **kwargs):
            raise RuntimeError("provider down")

    verifier = GoalVerifier(_BrokenLLM())
    verdict = asyncio.run(verifier.verify(
        {"objective": "x"}, [{"role": "user", "content": "hi"}]
    ))
    assert verdict["met"] is False
    assert "error" in verdict
