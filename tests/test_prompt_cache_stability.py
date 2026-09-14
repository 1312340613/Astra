"""Prompt-cache stability checks following dsh's byte-prefix discipline."""

import json
from types import SimpleNamespace

from agent.runtime.llm import _usage_dict
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def _registry(names):
    registry = ToolRegistry()
    for name in names:
        registry.register(ToolDef(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            fn=lambda name=name: name,
            group="core",
        ))
    return registry


def _tool_bytes(registry):
    return json.dumps(
        registry.to_openai_tools(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def test_model_tool_order_is_byte_stable_across_registration_order():
    forward = _registry(["zeta", "alpha", "beta"])
    backward = _registry(["beta", "zeta", "alpha"])

    assert [tool["function"]["name"] for tool in forward.to_openai_tools()] == [
        "alpha", "beta", "zeta",
    ]
    assert _tool_bytes(forward) == _tool_bytes(backward)


def test_activation_catalog_lists_group_tools_in_stable_order():
    registry = ToolRegistry()
    for group, names in (("b", ["z", "a"]), ("a", ["m", "b"])):
        for name in names:
            registry.register(ToolDef(
                name=name,
                description=name,
                parameters={"type": "object", "properties": {}},
                fn=lambda name=name: name,
                group=group,
            ))
    schema = registry.activation_tool_schema()
    assert schema is not None
    assert "a: b, m" in schema["function"]["description"]
    assert "b: a, z" in schema["function"]["description"]


def test_dsh_cache_hit_math_first_request_misses_later_request_hits():
    first = SimpleNamespace(
        prompt_tokens=120,
        completion_tokens=0,
        prompt_cache_hit_tokens=0,
        prompt_cache_miss_tokens=120,
    )
    second = SimpleNamespace(
        prompt_tokens=120,
        completion_tokens=0,
        prompt_cache_hit_tokens=96,
        prompt_cache_miss_tokens=24,
    )

    first_usage = _usage_dict(first)
    second_usage = _usage_dict(second)

    assert first_usage["prompt_cache_hit_tokens"] == 0
    assert second_usage["prompt_cache_hit_tokens"] == 96
    hit_input = second_usage["prompt_cache_hit_tokens"] + second_usage["prompt_cache_miss_tokens"]
    assert hit_input == 120
    assert second_usage["prompt_cache_hit_tokens"] / hit_input == 0.8
