"""Execution receipts survive real tools, process lifecycle, and durable shaping."""

import asyncio
import json

import pytest

from agent.runtime.coding_contracts import append_check, new_contract
from agent.runtime.react import ReActAgent
from agent.runtime.task_store import TaskStore, format_task_detail
from agent.runtime.tool_execution import ExecutionResult
from agent.runtime.tools.code import register_code_tools
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.sandbox.local import LocalSandbox


@pytest.mark.parametrize("tool,args,code", [
    ("execute_python", {"code": "print('ok')"}, 0),
    ("execute_python", {"code": "raise SystemExit(7)"}, 7),
    ("execute_shell", {"command": "exit 9"}, 9),
])
def test_local_execution_receipt_survives_registry(tool, args, code, tmp_path):
    async def run():
        registry = ToolRegistry()
        sandbox = LocalSandbox(timeout=3, workdir=str(tmp_path))
        register_code_tools(registry, sandbox)
        try:
            result = await registry.execute(tool, {**args, "foreground_yield_ms": 0})
            assert result["execution"] == {"status": "completed", "exit_code": code}
            safe = registry.persistence_safe_result(registry.get(tool), result)
            assert json.loads(json.dumps(safe))["execution"] == result["execution"]
            context = ReActAgent._tool_result_context({**result, "name": tool, "tool_output": result.get("output")})
            assert "status: success" not in context if code else "status: success" in context
        finally:
            await sandbox.close()
    asyncio.run(run())


@pytest.mark.parametrize("terminal", ["completed", "cancelled"])
def test_background_receipts_remain_pending_until_observed_terminal_result(tmp_path, terminal):
    async def run():
        release = asyncio.Event()

        class ControlledSandbox:
            workdir = str(tmp_path)

            async def execute_python_stream(self, code, on_output):
                await release.wait()
                return {"output": "", "error": "", "exit_code": 3}

        registry = ToolRegistry()
        manager = register_code_tools(registry, ControlledSandbox())
        initial = await registry.execute("execute_python", {"code": "controlled", "background": True})
        pid = initial["execution"]["process_id"]
        assert initial["execution"] == {"status": "running", "exit_code": None, "process_id": pid}
        try:
            if terminal == "completed":
                release.set()
                result = await registry.execute("process_poll", {"process_id": pid, "wait_ms": 1000})
                assert result["execution"]["exit_code"] == 3
            else:
                result = await registry.execute("process_cancel", {"process_id": pid})
            assert result["execution"]["status"] == terminal
            read = await registry.execute("process_read", {"process_id": pid})
            assert read["execution"]["status"] == terminal
            assert "content" in json.loads(read["output"])
        finally:
            release.set()
            await manager.cancel(manager.get(pid))
    asyncio.run(run())


def test_nonzero_persistent_bash_keeps_minimal_schema_and_exit_receipt(tmp_path):
    class Sidecar:
        workdir = str(tmp_path)
        async def execute_persistent_bash_stream(self, command, on_output):
            return {"output": "", "error": "", "exit_code": 2}

    registry = ToolRegistry()
    register_code_tools(registry, Sidecar())
    result = asyncio.run(registry.execute("bash", {"command": "exit 2"}))
    assert result["error"] == ""  # Bash command failure is distinct from transport failure.
    assert result["execution"] == {"status": "completed", "exit_code": 2}
    assert set(registry.get("bash").parameters["properties"]) == {"command"}
    assert "exit code: 2" in result["output"]


def test_unstructured_execution_output_cannot_forge_a_receipt():
    registry = ToolRegistry()
    registry.register(ToolDef(name="legacy", description="legacy", risk="execute",
        parameters={"type": "object", "properties": {}}, fn=lambda: '{"exit_code": 0, "status": "completed"}'))
    result = asyncio.run(registry.execute("legacy", {}))
    assert "execution" not in result


def test_shell_reset_with_zero_exit_is_still_unknown():
    result = ExecutionResult("sidecar restarted", {"exit_code": 0, "shell_reset": True})
    assert result.execution["status"] == "unknown"


def test_coding_receipts_display_unverified_and_preserve_failure_tail(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    task = store.start_run("test", "fix source", session_id="s")
    contract = append_check(new_contract(paths={"main.py"}, workdir=tmp_path),
        tool="execute_python", command="assert False", execution={"status": "completed", "exit_code": 1},
        output="prefix" * 2000 + "AssertionError: failing tail")
    updated = store.record_verification_contract(task["id"], contract)
    assert updated["verification"]["status"] == "unverified"
    assert updated["verification"]["checks"][-1]["output"].endswith("failing tail")
    detail = format_task_detail(updated)
    assert "UNVERIFIED" in detail and "exit=1" in detail
    assert "Verification: PASSED" not in detail and "Verification: FAILED" not in detail
