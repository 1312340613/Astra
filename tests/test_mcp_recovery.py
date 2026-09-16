import asyncio
from types import SimpleNamespace as NS

import pytest

from agent.runtime.mcp import MCPManager
from agent.runtime.tools.registry import ToolRegistry


class Session:
    def __init__(self, mode="disconnect"):
        self.calls = 0
        self.mode = mode
        self.started = asyncio.Event()

    async def call_tool(self, name, arguments):
        self.calls += 1  # The remote action has happened; its response may be lost.
        self.started.set()
        if self.mode == "disconnect":
            raise ConnectionError("reply lost")
        if self.mode == "wait":
            await asyncio.Event().wait()
        return NS(content=[NS(text="timeout after remote action")], isError=True)


def setup(tmp_path, session, risk="network", hint=None):
    manager, registry = MCPManager(tmp_path / "mcp.json"), ToolRegistry()
    tool = NS(name="action", description="fixture", inputSchema={"type": "object", "properties": {}},
              annotations=NS(readOnlyHint=hint))
    config = {"risk": risk, "timeout": 0.02}
    manager._register_tools(registry, "fixture", session, [tool], config)
    return manager, registry, tool, config


@pytest.mark.parametrize("mode", ["disconnect", "wait", "error"])
@pytest.mark.parametrize("risk", ["network", "write", "execute"])
def test_mutating_failure_never_retries_or_suggests_replay(tmp_path, mode, risk):
    async def scenario():
        session = Session(mode)
        _, registry, _, _ = setup(tmp_path, session, risk)
        result = await registry.execute("mcp__fixture__action", {})
        assert session.calls == 1
        assert result["retryable"] is False
        assert "repeat" in result["recovery_hint"].lower()
        if mode != "error":
            assert result["code"] == "mcp_unknown_outcome" and result["partial"]
            assert result["details"]["dispatch_state"] == "unknown"
    asyncio.run(scenario())


@pytest.mark.parametrize("risk,hint,can_retry", [("read", None, True), ("network", True, True),
                                              ("write", True, False), ("network", None, False)])
def test_read_recovery_does_not_override_configured_side_effects(tmp_path, risk, hint, can_retry):
    session = Session()
    _, registry, _, _ = setup(tmp_path, session, risk, hint)
    result = asyncio.run(registry.execute("mcp__fixture__action", {}))
    assert result["retryable"] is can_retry and session.calls == 1


def test_cancel_propagates_and_old_definition_cannot_dispatch_after_reconnect(tmp_path):
    async def scenario():
        session = Session("wait")
        manager, registry, remote, config = setup(tmp_path, session)
        old = registry.get("mcp__fixture__action")
        task = asyncio.create_task(registry.execute(old.name, {}))
        await session.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "Do not replay" in manager.statuses[0].error
        disconnected = await old.fn()
        assert disconnected.details["dispatch_state"] == "not_dispatched"
        fresh = Session("error")
        manager._register_tools(registry, "fixture", fresh, [remote], config)
        stale = await old.fn()
        assert stale.code == "mcp_stale_connection"
        assert fresh.calls == 0 and session.calls == 1
    asyncio.run(scenario())
