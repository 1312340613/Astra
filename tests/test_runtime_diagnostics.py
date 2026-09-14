import asyncio
import json
import sys
from types import SimpleNamespace

from agent.cli.diagnostics import build_doctor_report, build_runtime_diagnostics
from agent.runtime.capabilities import CapabilityState, build_runtime_capabilities
from agent.runtime.context import AgentContext
from agent.runtime.react import ReActAgent
from agent.runtime.tools.code import register_code_tools
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.sandbox.local import LocalSandbox


def _agent():
    context = AgentContext(system_prompt="system guidance", max_prompt_tokens=10_000)
    context.add_user("hello")
    context.add_assistant("world")
    context.set_tools_token_cost(12)
    context.total_cache_hit_tokens = 80
    context.total_cache_miss_tokens = 20
    tools = ToolRegistry()
    tools.register(ToolDef(
        name="demo",
        description="demo",
        parameters={"type": "object", "properties": {}},
        fn=lambda: "ok",
        group="core",
    ))
    return SimpleNamespace(
        context=context,
        tools=tools,
        llm=SimpleNamespace(config=SimpleNamespace(model="test-model")),
        prompt_cache_stable_tools=True,
    )


def test_prompt_token_breakdown_is_role_aware():
    breakdown = _agent().context.prompt_token_breakdown()

    assert breakdown["system"] > 0
    assert breakdown["tools"] == 12
    assert breakdown["role_messages"] == {"user": 1, "assistant": 1}
    assert breakdown["estimated_current"] == (
        breakdown["system"] + breakdown["tools"] + breakdown["messages"]
    )


def test_runtime_diagnostics_json_is_structured_and_read_only():
    agent = _agent()
    manager = SimpleNamespace(statuses=[
        SimpleNamespace(name="vision", state="ready", tools=3, error=""),
    ])
    tasks = SimpleNamespace(list_tasks=lambda limit: [
        {"status": "running"}, {"status": "completed"},
    ])
    processes = SimpleNamespace(list=lambda include_completed: [
        {"status": "running"},
    ])

    payload = json.loads(build_runtime_diagnostics(
        agent,
        startup_profile={"total_ms": 25, "phases": []},
        mcp_manager=manager,
        task_store=tasks,
        process_manager=processes,
        section="json",
    ))

    assert payload["context"]["role_messages"]["user"] == 1
    assert payload["prompt_cache"]["hit_rate"] == 0.8
    assert payload["mcp"] == {
        "ready": 1,
        "servers": [{"error": "", "name": "vision", "state": "ready", "tools": 3}],
        "tools": 3,
        "total": 1,
    }
    assert payload["tasks"]["counts"] == {"completed": 1, "running": 1}
    assert payload["processes"]["counts"] == {"running": 1}


def test_runtime_diagnostics_sections_and_invalid_name():
    agent = _agent()

    context = build_runtime_diagnostics(agent, section="context")
    startup = build_runtime_diagnostics(agent, section="startup")
    invalid = build_runtime_diagnostics(agent, section="remote")

    assert "Prompt cache: 80 hit / 20 miss (80.0% hit)" in context
    assert "ASTRA_PROFILE_STARTUP=1" in startup
    assert "Available: computer, context, startup, mcp, tasks, json" in invalid


def test_register_code_tools_returns_manager_after_all_process_tools(tmp_path):
    registry = ToolRegistry()
    manager = register_code_tools(registry, LocalSandbox(workdir=str(tmp_path)))

    assert manager.artifact_dir == (tmp_path / ".astra" / "processes").resolve()
    assert {
        "process_poll", "process_read", "process_list", "process_cancel",
    }.issubset(registry.tool_names)


def test_dynamic_registry_revision_refreshes_prompt_tool_cost():
    registry = ToolRegistry()
    registry.register(ToolDef(
        name="local",
        description="short",
        parameters={"type": "object", "properties": {}},
        fn=lambda: "ok",
    ))
    llm = SimpleNamespace(config=SimpleNamespace(model="test-model"))
    agent = ReActAgent("test", llm, registry, "system")
    before = agent.context.prompt_token_breakdown()["tools"]

    registry.replace_owned_tools("mcp:demo", [ToolDef(
        name="mcp__demo__large",
        description="long schema " * 100,
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        fn=lambda: "ok",
    )])
    agent.refresh_tool_token_cost()

    assert agent.context.prompt_token_breakdown()["tools"] > before


def test_computer_capability_is_platform_truth_not_tool_name(monkeypatch):
    agent = _agent()
    agent.tools.register(ToolDef(
        name="computer_status",
        description="unrelated tool with a misleading name",
        parameters={"type": "object", "properties": {}},
        fn=lambda: "ok",
    ))
    monkeypatch.setattr(sys, "platform", "linux")

    status = build_runtime_capabilities(agent).get("computer.use")

    assert status is not None
    assert status.state is CapabilityState.UNSUPPORTED
    assert "macOS" in status.detail


def test_computer_diagnostics_report_configured_without_probing(monkeypatch):
    agent = _agent()
    probes = []
    agent._computer_runtime = SimpleNamespace(
        capability_status=lambda: (
            "configured",
            "Native helper is configured but has not been probed.",
        ),
        probe=lambda: probes.append(True),
    )
    monkeypatch.setattr(sys, "platform", "darwin")

    payload = json.loads(build_runtime_diagnostics(agent, section="json"))

    assert payload["computer"] == {
        "detail": "Native helper is configured but has not been probed.",
        "state": "configured",
    }
    assert probes == []


def test_doctor_computer_section_explicitly_probes(monkeypatch):
    agent = _agent()
    state = {"value": ("configured", "not probed")}

    async def probe():
        state["value"] = ("available", "probe complete")

    agent._computer_runtime = SimpleNamespace(
        capability_status=lambda: state["value"],
        probe=probe,
    )
    monkeypatch.setattr(sys, "platform", "darwin")

    output = asyncio.run(build_doctor_report(agent, section="computer"))

    assert "computer.use: available" in output
    assert "probe complete" in output
