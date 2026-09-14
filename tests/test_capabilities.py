import asyncio
import json
import sys
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace

from anyio import BrokenResourceError

from agent.cli.diagnostics import build_doctor_report
from agent.runtime.capabilities import CapabilityState, build_runtime_capabilities
from agent.runtime.mcp import MCPManager, MCPServerStatus
from agent.runtime.memory import MemoryStore
from agent.runtime.skills import SkillStore
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def test_runtime_capabilities_report_live_objects_not_aspirational_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_AUTO_RETAIN", "1")
    async def extract_url(url: str):
        return url

    tools = ToolRegistry()
    tools.register(ToolDef(
        name="extract_url",
        description="read a URL",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        fn=extract_url,
    ))
    memory = MemoryStore(path=tmp_path / "memory.db", core_dir=tmp_path / "memory")
    agent = SimpleNamespace(
        memory_store=memory,
        memory_router=SimpleNamespace(recall_limit=4),
        memory_retainer=SimpleNamespace(enabled=True, mode="tool-evidence"),
        skill_store=SkillStore(tmp_path / "skills"),
        task_store=object(),
        tools=tools,
    )
    mcp = MCPManager(tmp_path / "mcp.json")
    mcp.statuses = [MCPServerStatus("demo", "ready", tools=2)]
    mcp.config_warnings = ["unknown or unsupported field: servers.demo.magic"]

    report = build_runtime_capabilities(agent, sandbox=SimpleNamespace(description="local"), mcp_manager=mcp)

    statuses = {item.name: item for item in report.statuses}
    assert statuses["memory.builtin"].state is CapabilityState.AVAILABLE
    assert statuses["memory.recall_router"].state is CapabilityState.AVAILABLE
    assert statuses["memory.auto_retain"].state is CapabilityState.AVAILABLE
    assert statuses["tasks.durable"].state is CapabilityState.AVAILABLE
    assert statuses["browser.read"].state is CapabilityState.AVAILABLE
    assert statuses["browser.interactive"].state is CapabilityState.UNSUPPORTED
    assert statuses["mcp"].state is CapabilityState.AVAILABLE
    assert "servers.demo.magic" in report.render()


def test_mcp_schema_warns_for_unknown_and_missing_transport_fields():
    warnings = MCPManager._validate_config({
        "unexpected": True,
        "servers": {
            "stdio-demo": {"transport": "stdio", "magic": True},
            "http-demo": {"transport": "streamable-http"},
            "off": {"enabled": False, "future": "ignored but visible"},
        },
    })

    assert "unknown root field: unexpected" in warnings
    assert "unknown or unsupported field: servers.stdio-demo.magic" in warnings
    assert "missing required field: servers.stdio-demo.command" in warnings
    assert "missing required field: servers.http-demo.url" in warnings
    assert "unknown or unsupported field: servers.off.future" in warnings


def test_mcp_schema_accepts_startup_timeout():
    warnings = MCPManager._validate_config({
        "servers": {
            "demo": {
                "transport": "stdio",
                "command": "demo",
                "startup_timeout": 5,
            },
        },
    })

    assert not any("startup_timeout" in warning for warning in warnings)


def test_mcp_startup_timeout_degrades_instead_of_blocking(tmp_path):
    server = tmp_path / "hanging_mcp.py"
    server.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({
        "servers": {
            "slow": {
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(server)],
            },
        },
    }), encoding="utf-8")
    manager = MCPManager(config, startup_timeout=1)

    asyncio.run(manager.load(ToolRegistry()))

    assert manager.statuses == [MCPServerStatus(
        "slow",
        "error",
        error="startup timed out after 1s",
    )]


def test_mcp_cleanup_exception_does_not_crash_backend(tmp_path, monkeypatch):
    import mcp
    import mcp.client.stdio
    import agent.runtime.mcp as mcp_runtime

    class FakeStack:
        def callback(self, callback, *args, **kwargs):
            return None

        async def enter_async_context(self, context: AbstractAsyncContextManager):
            if isinstance(context, FakeSession):
                return context
            return object(), object()

        async def aclose(self):
            raise ExceptionGroup("stdio cleanup failed", [BrokenResourceError()])

    class FakeSession:
        def __init__(self, read_stream, write_stream, *, message_handler=None):
            self.message_handler = message_handler
            pass

        async def initialize(self):
            await asyncio.sleep(60)

    monkeypatch.setattr(mcp_runtime, "AsyncExitStack", FakeStack)
    monkeypatch.setattr(mcp, "ClientSession", FakeSession)
    monkeypatch.setattr(mcp, "StdioServerParameters", lambda **kwargs: kwargs)
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", lambda params, errlog: object())

    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({
        "servers": {
            "slow": {
                "transport": "stdio",
                "command": "demo",
                "startup_timeout": 1,
            },
            "after": {"enabled": False},
        },
    }), encoding="utf-8")
    manager = MCPManager(config)

    asyncio.run(manager.load(ToolRegistry()))

    assert len(manager.statuses) == 2
    status = manager.statuses[0]
    assert status.name == "slow"
    assert status.state == "error"
    assert status.error.startswith("startup timed out after 1s")
    assert "cleanup warning: BrokenResourceError" in status.error
    assert manager.statuses[1] == MCPServerStatus("after", "disabled")


def test_mcp_background_start_is_immediate_and_failed_server_can_reconnect(tmp_path):
    class RecoveringManager(MCPManager):
        def __init__(self):
            super().__init__(tmp_path / "mcp.json")
            self.loaded = asyncio.Event()
            self.reconnected = asyncio.Event()

        async def load(self, registry):
            self._registry = registry
            self._server_configs = {"demo": {}}
            self.statuses = [MCPServerStatus("demo", "error", error="401 unauthorized")]
            self.loaded.set()

        async def _reconnect_server(self, name):
            self._set_status(name, "ready", tools=2)
            self.reconnected.set()

    async def scenario():
        manager = RecoveringManager()
        manager.start_background(ToolRegistry())
        assert manager.statuses == [MCPServerStatus("startup", "connecting")]
        await manager.loaded.wait()
        await asyncio.sleep(0)
        manager.request_reconnect()
        await asyncio.wait_for(manager.reconnected.wait(), timeout=1)
        assert manager.statuses == [MCPServerStatus("demo", "ready", tools=2)]
        await manager.close()

    asyncio.run(scenario())


def test_mcp_startup_is_bounded_and_server_failure_isolated(tmp_path):
    class ProbedManager(MCPManager):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.active = 0
            self.peak = 0
            self.started = []

        async def _enter_server_session(self, name, raw, stack):
            del stack
            manager = self

            class Session:
                async def initialize(self):
                    manager.active += 1
                    manager.peak = max(manager.peak, manager.active)
                    manager.started.append(name)
                    try:
                        await asyncio.sleep(float(raw.get("delay", 0.02)))
                        if raw.get("fail"):
                            raise RuntimeError("server failed")
                    finally:
                        manager.active -= 1

                async def list_tools(self):
                    return SimpleNamespace(tools=[SimpleNamespace(name="demo")])

            return Session()

    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({
        "servers": {
            "first": {"command": "demo", "delay": 0.03},
            "second": {"command": "demo", "delay": 0.03},
            "failed": {"command": "demo", "fail": True},
            "last": {"command": "demo"},
        },
    }), encoding="utf-8")
    manager = ProbedManager(config, startup_concurrency=2)

    async def scenario():
        await manager.load(ToolRegistry())
        await manager.close()

    asyncio.run(scenario())

    statuses = {status.name: status for status in manager.statuses}
    assert manager.peak == 2
    assert set(manager.started) == {"first", "second", "failed", "last"}
    assert statuses["first"] == MCPServerStatus("first", "ready", tools=1)
    assert statuses["second"] == MCPServerStatus("second", "ready", tools=1)
    assert statuses["last"] == MCPServerStatus("last", "ready", tools=1)
    assert statuses["failed"].state == "error"
    assert statuses["failed"].error == "RuntimeError: server failed"


def test_mcp_background_startup_cancellation_closes_stacks_in_lifecycle_task(tmp_path):
    import agent.runtime.mcp as mcp_runtime

    class RecordingStack:
        def __init__(self):
            self.closed = 0
            self.closed_by = None

        async def aclose(self):
            self.closed += 1
            self.closed_by = asyncio.current_task().get_name()

    class BlockingManager(MCPManager):
        def __init__(self):
            super().__init__(tmp_path / "mcp.json", startup_concurrency=2)
            self.entered = asyncio.Event()
            self.stack = RecordingStack()
            self.block = asyncio.Event()

        async def _open_one(self, name, raw):
            del name, raw
            manager = self

            class Session:
                async def initialize(self):
                    manager.entered.set()
                    await manager.block.wait()

                async def list_tools(self):
                    return SimpleNamespace(tools=[])

            return mcp_runtime._MCPStartupResult(
                "connected",
                stack=self.stack,
                session=Session(),
                deadline=asyncio.get_running_loop().time() + 30,
                timeout=30,
            )

    config = tmp_path / "mcp.json"
    config.write_text(
        '{"servers":{"blocking":{"command":"demo"}}}',
        encoding="utf-8",
    )

    async def scenario():
        manager = BlockingManager()
        lifecycle = manager.start_background(ToolRegistry())
        await asyncio.wait_for(manager.entered.wait(), timeout=1)

        await manager.close()

        assert lifecycle.done()
        assert manager.stack.closed == 1
        assert manager.stack.closed_by == "astra-mcp-lifecycle"

    asyncio.run(scenario())


def test_mcp_status_listener_observes_background_lifecycle(tmp_path):
    class ReadyManager(MCPManager):
        def __init__(self):
            super().__init__(tmp_path / "mcp.json")
            self.loaded = asyncio.Event()

        async def load(self, registry):
            self._replace_statuses([MCPServerStatus("demo", "ready", tools=2)])
            self.loaded.set()

    async def scenario():
        manager = ReadyManager()
        updates = []
        manager.set_status_listener(updates.append)

        manager.start_background(ToolRegistry())
        await asyncio.wait_for(manager.loaded.wait(), timeout=1)

        assert updates == [
            (MCPServerStatus("startup", "connecting"),),
            (MCPServerStatus("demo", "ready", tools=2),),
        ]
        await manager.close()

    asyncio.run(scenario())


def test_runtime_capabilities_degrade_when_core_markdown_exceeds_hard_limit(tmp_path):
    core_dir = tmp_path / "core"
    memory = MemoryStore(path=tmp_path / "memory.db", core_dir=core_dir)
    (core_dir / "MEMORY.md").write_text("x" * 2201, encoding="utf-8")
    agent = SimpleNamespace(
        memory_store=memory,
        memory_router=None,
        memory_retainer=None,
        skill_store=None,
        task_store=None,
        tools=ToolRegistry(),
    )

    report = build_runtime_capabilities(agent)
    status = next(item for item in report.statuses if item.name == "memory.builtin")

    assert status.state is CapabilityState.DEGRADED
    assert "exceed their hard limit" in status.warnings[0]


def test_disabled_mcp_config_surfaces_schema_warning_without_connecting(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(
        '{"servers":{"demo":{"enabled":false,"transport":"stdio","future_flag":true}}}',
        encoding="utf-8",
    )
    manager = MCPManager(config)

    asyncio.run(manager.load(ToolRegistry()))

    assert manager.statuses == [MCPServerStatus("demo", "disabled")]
    assert "future_flag" in manager.report()


def test_doctor_section_reports_actual_browser_gap(tmp_path):
    agent = SimpleNamespace(
        memory_store=None,
        skill_store=None,
        task_store=None,
        tools=ToolRegistry(),
    )

    output = asyncio.run(build_doctor_report(agent, section="browser"))

    assert "Agent doctor (browser)" in output
    assert "browser.read: unsupported" in output
    assert "browser.interactive: unsupported" in output
    assert "Model:" not in output
