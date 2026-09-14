"""Tests for Code Mode tool-exposure filtering (native / code / both)."""

from types import SimpleNamespace

from agent.runtime.code_mode import register_run_code_tool
from agent.runtime.react import ReActAgent
from agent.runtime.tools.bar import register_bar_tools
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolDef("echo", "echo", {
        "type": "object", "properties": {"message": {"type": "string"}},
    }, lambda message="": "ok", group="core"))
    register_run_code_tool(registry, agent_getter=lambda: None)
    return registry


def _schemas() -> list[dict]:
    return [
        {"function": {"name": "echo", "parameters": {"type": "object", "properties": {}}}},
        {"function": {"name": "run_code", "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "The program."},
            "description": {"type": "string", "description": "Summary."},
        }}}},
    ]


def _agent(code_mode: str) -> ReActAgent:
    return ReActAgent("a", SimpleNamespace(), _registry(), code_mode=code_mode)


def test_both_passes_everything_through():
    agent = _agent("both")
    schemas = _schemas()
    assert agent._apply_code_mode(schemas) == schemas


def test_strict_both_mode_lists_nested_allowlist_for_ptc():
    agent = _agent("both")
    agent.tool_allowlist = {"run_code", "edit_file", "search_web"}
    registry = agent.tools
    registry.register(ToolDef("edit_file", "edit", {"type": "object", "properties": {}}, lambda: "ok"))
    registry.register(ToolDef("search_web", "search", {"type": "object", "properties": {}}, lambda: "ok", risk="network"))
    schemas = _schemas() + [
        {"function": {"name": "edit_file", "parameters": {"type": "object", "properties": {}}}},
        {"function": {"name": "search_web", "parameters": {"type": "object", "properties": {}}}},
    ]

    result = agent._apply_code_mode(schemas)
    code_desc = result[1]["function"]["parameters"]["properties"]["code"]["description"]
    assert "Available tools (call as tools.<name>(...)): edit_file, search_web" in code_desc


def test_stable_schema_cache_applies_to_strict_allowlists():
    agent = _agent("both")
    agent.minimal_mode = True
    agent.tool_allowlist = {"run_code", "echo"}

    first = agent._available_tool_schemas("work", set(), {}, set())
    second = agent._available_tool_schemas("work", set(), {}, {"echo"})

    # A fixed profile keeps its schema stable for provider prefix caching;
    # execution still enforces blocked tools and per-turn budgets.
    assert [item["function"]["name"] for item in first] == ["echo", "run_code"]
    assert [item["function"]["name"] for item in second] == ["echo", "run_code"]
    assert agent._stable_tool_schema_key is not None
    assert agent._stable_tool_schema_key[0] == ("echo", "run_code")


def test_stable_schema_cache_rebuilds_when_strict_allowlist_changes():
    agent = _agent("both")
    agent.minimal_mode = True
    agent.tool_allowlist = {"run_code", "echo"}
    agent._available_tool_schemas("work", set(), {}, set())

    agent.tool_allowlist = {"run_code"}
    result = agent._available_tool_schemas("work", set(), {}, set())

    assert [item["function"]["name"] for item in result] == ["run_code"]


def test_react_defaults_to_native_code_mode():
    agent = ReActAgent("a", SimpleNamespace(), _registry())
    assert agent.code_mode == "native"
    assert [s["function"]["name"] for s in agent._apply_code_mode(_schemas())] == ["echo"]


def test_native_hides_run_code():
    agent = _agent("native")
    result = agent._apply_code_mode(_schemas())
    names = [s["function"]["name"] for s in result]
    assert names == ["echo"]


def test_code_keeps_run_code_and_lists_tools():
    agent = _agent("code")
    result = agent._apply_code_mode(_schemas())
    assert [item["function"]["name"] for item in result] == ["run_code"]
    code_desc = result[0]["function"]["parameters"]["properties"]["code"]["description"]
    assert "echo" in code_desc
    assert "run_code" not in code_desc.split("Available tools")[-1]


def test_code_keeps_question_and_plan_direct_but_excludes_nested_bindings():
    registry = _registry()
    registry.register(ToolDef(
        "ask_user_question",
        "ask",
        {"type": "object", "properties": {}},
        lambda: "answer",
        group="core",
    ))
    registry.register(ToolDef(
        "plan_update",
        "plan",
        {"type": "object", "properties": {}},
        lambda: "planned",
        group="core",
        risk="write",
    ))
    agent = ReActAgent("a", SimpleNamespace(), registry, code_mode="code")

    result = agent._apply_code_mode(registry.to_openai_tools())

    assert [item["function"]["name"] for item in result] == [
        "run_code",
        "ask_user_question",
        "plan_update",
    ]
    code_desc = result[0]["function"]["parameters"]["properties"]["code"]["description"]
    nested_catalog = code_desc.split("Available tools", 1)[-1]
    nested_names = {
        name.strip()
        for name in nested_catalog.split(":", 1)[-1].split(",")
    }
    assert "echo" in nested_names
    assert "ask_user_question" not in nested_names
    assert "plan_update" not in nested_names


def test_code_keeps_request_local_tools_direct_and_excludes_nested_bindings():
    registry = _registry()
    registry.register(ToolDef(
        "ephemeral_lookup",
        "ephemeral",
        {"type": "object", "properties": {}},
        lambda: "private evidence",
        group="core",
        result_persistence="request_local",
    ))
    agent = ReActAgent("a", SimpleNamespace(), registry, code_mode="code")

    result = agent._apply_code_mode(registry.to_openai_tools())

    assert [item["function"]["name"] for item in result] == [
        "run_code",
        "ephemeral_lookup",
    ]
    code_desc = result[0]["function"]["parameters"]["properties"]["code"]["description"]
    nested_catalog = code_desc.split("Available tools", 1)[-1]
    assert "ephemeral_lookup" not in nested_catalog


def test_code_does_not_list_mode_private_bar_tools():
    registry = _registry()
    register_bar_tools(registry, None)
    agent = ReActAgent("a", SimpleNamespace(), registry, code_mode="code")

    result = agent._apply_code_mode(_schemas())
    code_desc = result[0]["function"]["parameters"]["properties"]["code"]["description"]
    available = code_desc.split("Available tools", 1)[-1]

    assert "echo" in available
    for name in (
        "bar_turn",
        "serve_drink",
        "refill_drink",
        "rename_drink",
        "set_ambiance",
        "pour_lyra_drink",
        "sip_lyra_drink",
    ):
        assert name not in available


def test_code_keeps_native_fallbacks_for_interactive_approval():
    registry = _registry()
    registry.register(ToolDef(
        "dangerous",
        "dangerous",
        {"type": "object", "properties": {}},
        lambda: "blocked",
        group="code",
        approval="on_risk",
    ))
    registry.register(ToolDef(
        "scoped_read",
        "scoped read",
        {"type": "object", "properties": {"path": {"type": "string"}}},
        lambda path="": path,
        group="files",
        permission_check=lambda _args: {"reason": "outside workspace"},
    ))
    agent = ReActAgent("a", SimpleNamespace(), registry, code_mode="code")
    schemas = _schemas() + [
        {"function": {"name": "dangerous", "parameters": {"type": "object", "properties": {}}}},
        {"function": {"name": "scoped_read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    ]

    result = agent._apply_code_mode(schemas)

    assert [item["function"]["name"] for item in result] == [
        "run_code",
        "dangerous",
        "scoped_read",
    ]


def test_progressive_code_mode_restores_fallbacks_from_unrouted_groups():
    registry = _registry()
    registry.register(ToolDef(
        "scoped_read",
        "scoped read",
        {"type": "object", "properties": {"path": {"type": "string"}}},
        lambda path="": path,
        group="files",
        permission_check=lambda _args: {"reason": "outside workspace"},
    ))
    agent = ReActAgent(
        "a",
        SimpleNamespace(),
        registry,
        code_mode="code",
        progressive_tools=True,
    )
    # Exercise the routed (non-stable) catalog: neither the current text nor
    # the active groups select the files group, but its approval fallback must
    # remain callable natively from Code Mode.
    agent.prompt_cache_stable_tools = False

    result = agent._available_tool_schemas(
        "retry",
        {"core"},
        {},
        set(),
    )

    assert [item["function"]["name"] for item in result] == [
        "run_code",
        "scoped_read",
        "activate_tool_group",
    ]


def test_code_mode_reload_invalidates_stable_schema_cache():
    registry = _registry()
    registry.register(ToolDef(
        "scoped_read",
        "scoped read",
        {"type": "object", "properties": {"path": {"type": "string"}}},
        lambda path="": path,
        group="files",
        permission_check=lambda _args: {"reason": "outside workspace"},
    ))
    agent = ReActAgent(
        "a",
        SimpleNamespace(),
        registry,
        code_mode="code",
        progressive_tools=False,
    )
    agent._stable_tool_schema_key = ("stale",)
    agent._stable_tool_schemas = [
        {"function": {"name": "run_code", "parameters": {}}},
    ]

    agent.invalidate_tool_schema_cache()
    result = agent._available_tool_schemas("retry", set(), {}, set())

    assert [item["function"]["name"] for item in result] == [
        "run_code",
        "scoped_read",
    ]


def test_code_without_run_code_schema_is_empty():
    registry = ToolRegistry()
    registry.register(ToolDef("echo", "echo", {
        "type": "object", "properties": {},
    }, lambda: "ok", group="core"))
    agent = ReActAgent("a", SimpleNamespace(), registry, code_mode="code")
    # No run_code in the incoming schemas -> nothing survives code filtering.
    schemas = [{"function": {"name": "echo", "parameters": {}}}]
    assert agent._apply_code_mode(schemas) == []
