import asyncio
import json

from agent.runtime import runtime_identity as identity_module
from agent.runtime.execution_limits import COMMAND_FOREGROUND_MAX_MS, POLL_MAX_MS
from agent.runtime.tools import delegate
from agent.runtime.tools.code import register_code_tools
from agent.runtime.tools.delegate import register_delegate_tools
from agent.runtime.tools.processes import ProcessManager
from agent.runtime.tools.registry import ToolRegistry
from agent.sandbox.local import LocalSandbox


def test_poll_contract_and_cancellation_preserve_background_worker(tmp_path, monkeypatch):
    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        registry = ToolRegistry()
        register_delegate_tools(registry, llm_getter=lambda: None)
        started = asyncio.Event()

        async def work(_output):
            started.set()
            await asyncio.Future()

        process = manager.start(work, kind="subagent", label="wait", task_id="owner")
        manager.expose(process)
        await started.wait()
        assert registry.get("delegate_poll").timeout is None
        assert registry.get("delegate_poll").max_calls_per_turn is None
        assert registry.get("team_wait").max_calls_per_turn is None
        result = await registry.execute(
            "delegate_poll", {"process_id": process.process_id, "wait_ms": 1}, task_id="owner"
        )
        assert not result["error"]
        assert json.loads(result["output"])["status"] == "running"
        waiting = asyncio.create_task(registry.execute(
            "delegate_poll", {"process_id": process.process_id, "wait_ms": 35_000}, task_id="owner"
        ))
        await asyncio.sleep(0.01)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        assert manager.status(process) == "running"
        cancelled = await registry.execute(
            "delegate_cancel", {"process_id": process.process_id}, task_id="owner"
        )
        assert not cancelled["error"]
        assert process.task.done()
        assert manager.status(process) == "cancelled"
    asyncio.run(scenario())


def test_schema_and_boundary_validation_share_loaded_limits(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_code_tools(registry, LocalSandbox(workdir=str(tmp_path)))
        assert registry.get("execute_shell").parameters["properties"]["foreground_yield_ms"]["maximum"] == COMMAND_FOREGROUND_MAX_MS
        assert registry.get("process_poll").parameters["properties"]["wait_ms"]["maximum"] == POLL_MAX_MS
        invalid = await registry.execute(
            "process_poll", {"process_id": "unused", "wait_ms": POLL_MAX_MS + 1}
        )
        assert str(POLL_MAX_MS) in invalid["error"]
        assert "handle" in invalid["error"]
    asyncio.run(scenario())


def test_runtime_identity_is_frozen_but_disk_change_is_visible(monkeypatch):
    original = identity_module.runtime_identity(compare_disk=True)
    monkeypatch.setattr(identity_module, "_source_digest", lambda: "changed-on-disk")
    changed = identity_module.runtime_identity(compare_disk=True)
    assert changed["source_sha256"] == original["source_sha256"]
    assert changed["run_id"] == original["run_id"]
    assert changed["source_changed"] is True
    assert changed["disk_source_sha256"] == "changed-on-disk"
    assert changed["execution_limits"]["poll_max_ms"] == POLL_MAX_MS
    changed["execution_limits"]["poll_max_ms"] = 1
    assert identity_module.runtime_identity()["execution_limits"]["poll_max_ms"] == POLL_MAX_MS
