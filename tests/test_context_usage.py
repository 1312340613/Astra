"""Defensive normalization tests for session token-usage accumulation."""

from agent.runtime.context import AgentContext


def test_add_usage_accepts_null_and_string_fields():
    ctx = AgentContext()
    ctx.add_usage({
        "prompt_tokens": None,
        "completion_tokens": None,
        "prompt_cache_hit_tokens": None,
        "prompt_cache_miss_tokens": None,
    })
    ctx.add_usage({
        "prompt_tokens": "120",
        "completion_tokens": "8",
        "prompt_cache_hit_tokens": "96",
        "prompt_cache_miss_tokens": "24",
    })

    assert ctx.total_prompt_tokens == 120
    assert ctx.total_completion_tokens == 8
    assert ctx.total_cache_hit_tokens == 96
    assert ctx.total_cache_miss_tokens == 24
    assert ctx.last_prompt_tokens == 120


def test_add_usage_clamps_negative_and_invalid_values():
    ctx = AgentContext()
    ctx.add_usage({
        "prompt_tokens": -5,
        "completion_tokens": "not-a-number",
        "prompt_cache_hit_tokens": -2,
        "prompt_cache_miss_tokens": 7,
    })

    assert ctx.total_prompt_tokens == 0
    assert ctx.total_completion_tokens == 0
    assert ctx.total_cache_hit_tokens == 0
    assert ctx.total_cache_miss_tokens == 7
    assert ctx.last_prompt_tokens == 0
