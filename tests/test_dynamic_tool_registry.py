import asyncio
from types import SimpleNamespace

import pytest

from agent.runtime.mcp import MCPManager
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def definition(name: str, description: str = "old") -> ToolDef:
    async def invoke(**_kwargs):
        return description

    return ToolDef(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {}},
        fn=invoke,
    )


def test_owned_tool_replacement_is_atomic_and_updates_revision():
    registry = ToolRegistry()
    registry.register(definition("local"))
    registry.replace_owned_tools("mcp:demo", [definition("mcp_demo_a"), definition("mcp_demo_b")])
    first_revision = registry.schema_revision

    registry.replace_owned_tools("mcp:demo", [definition("mcp_demo_a", "new")])

    assert registry.get("local") is not None
    assert registry.get("mcp_demo_b") is None
    assert registry.get("mcp_demo_a").description == "new"
    assert registry.schema_revision == first_revision + 1

    with pytest.raises(ValueError, match="another owner"):
        registry.replace_owned_tools("mcp:other", [definition("mcp_demo_a")])
    assert registry.get("mcp_demo_a").description == "new"


def test_owned_tool_reconnect_refreshes_callable_without_schema_churn():
    registry = ToolRegistry()
    original = definition("mcp_demo_a")
    registry.replace_owned_tools("mcp:demo", [original])
    first_revision = registry.schema_revision

    replacement = definition("mcp_demo_a")
    registry.replace_owned_tools("mcp:demo", [replacement])

    assert registry.get("mcp_demo_a") is replacement
    assert registry.schema_revision == first_revision


def test_mcp_tool_list_notification_wakes_lifecycle(tmp_path):
    manager = MCPManager(tmp_path / "mcp.json")
    root_type = type("ToolListChangedNotification", (), {})

    asyncio.run(manager._message_handler("demo")(SimpleNamespace(root=root_type())))

    assert manager._tool_refresh_servers == {"demo"}
    assert manager._reconnect_event.is_set()


def test_mcp_refresh_replaces_removed_and_changed_tools(tmp_path):
    manager = MCPManager(tmp_path / "mcp.json")
    registry = ToolRegistry()

    class Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[
                SimpleNamespace(name="a", description="updated", inputSchema={"type": "object"}),
            ])

    session = Session()
    manager._registry = registry
    manager._sessions["demo"] = session
    manager._server_configs["demo"] = {}
    manager.statuses = []
    manager._register_tools(
        registry,
        "demo",
        session,
        [
            SimpleNamespace(name="a", description="old", inputSchema={"type": "object"}),
            SimpleNamespace(name="b", description="removed", inputSchema={"type": "object"}),
        ],
        {},
    )

    asyncio.run(manager._refresh_server_tools("demo"))

    assert registry.get("mcp__demo__a").description == "updated"
    assert registry.get("mcp__demo__b") is None
    assert manager.loaded_tools == 1
