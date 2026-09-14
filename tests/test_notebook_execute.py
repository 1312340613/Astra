import asyncio
import json

import pytest

nbformat = pytest.importorskip("nbformat")
pytest.importorskip("nbclient")
pytest.importorskip("ipykernel")

from agent.runtime.notebook_worker import run_notebook
from agent.runtime.tools.notebook import notebook_code, register_notebook_tools
from agent.runtime.tools.registry import ToolRegistry


def config(path, **overrides):
    settings = {"path": str(path), "output_path": "", "start_cell": 1, "end_cell": 0,
                    "skip_cells": [], "cell_timeout": 10, "kernel_name": "python3"}
    settings.update(overrides)
    return settings


def notebook(tmp_path, cells):
    path = tmp_path / "input notebook.ipynb"
    nbformat.write(nbformat.v4.new_notebook(cells=cells), path)
    return path


def test_real_kernel_range_skip_and_source_unchanged(tmp_path):
    path = notebook(tmp_path, [
        nbformat.v4.new_markdown_cell("heading"),
        nbformat.v4.new_code_cell("x = 7"),
        nbformat.v4.new_code_cell("raise RuntimeError('must skip')"),
        nbformat.v4.new_code_cell("print(x * 2)"),
        nbformat.v4.new_code_cell("raise RuntimeError('outside range')"),
    ])
    original = path.read_bytes()
    settings = config(path)
    settings.update(end_cell=4, skip_cells=[3])
    result = run_notebook(settings)
    output = nbformat.read(result["output_path"], as_version=4)
    assert path.read_bytes() == original
    assert result["status"] == "completed"
    assert [c["cell"] for c in result["cells"]] == [2, 4]
    assert output.cells[3].outputs[0].text == "14\n"
    assert output.cells[2].outputs == output.cells[4].outputs == []
    assert output.metadata.astra_execution.status == "completed"


def test_cell_failure_preserves_partial_output_without_running_later_cells(tmp_path):
    path = notebook(tmp_path, [
        nbformat.v4.new_code_cell("print('done')"),
        nbformat.v4.new_code_cell("1 / 0"),
        nbformat.v4.new_code_cell("print('must not run')"),
    ])
    settings = config(path)
    settings["output_path"] = str(tmp_path / "partial.ipynb")
    with pytest.raises(Exception, match="ZeroDivisionError"):
        run_notebook(settings)
    output = nbformat.read(settings["output_path"], as_version=4)
    assert output.cells[0].outputs[0].text == "done\n"
    assert output.cells[1].outputs[-1].ename == "ZeroDivisionError"
    assert output.cells[2].outputs == []
    assert output.metadata.astra_execution.status == "failed"


def test_existing_output_and_invalid_range_are_refused(tmp_path):
    path = notebook(tmp_path, [nbformat.v4.new_code_cell("print(1)")])
    original = path.read_bytes()
    settings = config(path)
    settings["output_path"] = str(path)
    with pytest.raises(ValueError):
        run_notebook(settings)
    settings["output_path"] = str(tmp_path / "occupied.ipynb")
    (tmp_path / "occupied.ipynb").write_text("keep")
    with pytest.raises(FileExistsError):
        run_notebook(settings)
    assert path.read_bytes() == original
    assert (tmp_path / "occupied.ipynb").read_text() == "keep"


def test_tool_forwards_background_and_task_binding():
    calls = []
    async def execute(code, **kwargs):
        compile(code, "<runner>", "exec")
        calls.append(kwargs)
        return '{"process_id":"p"}'
    registry = ToolRegistry()
    register_notebook_tools(registry, execute, lambda args: None, lambda *args: None)
    result = asyncio.run(registry.get("notebook_execute").fn(
        path="quote' notebook.ipynb", end_cell=5, background=True, _task_id="task"))
    assert json.loads(result)["process_id"] == "p"
    assert calls[0]["background"] is True and calls[0]["_task_id"] == "task"


def test_generated_runner_is_standalone():
    code = notebook_code({"path": "a\"b.ipynb"})
    compile(code, "<runner>", "exec")
    assert "from agent." not in code


def test_real_kernel_timeout_saves_failed_artifact(tmp_path):
    from nbclient.exceptions import CellTimeoutError
    path = notebook(tmp_path, [nbformat.v4.new_code_cell("import time; time.sleep(5)")])
    settings = config(path, cell_timeout=1, output_path=str(tmp_path / "timeout.ipynb"))
    with pytest.raises(CellTimeoutError):
        run_notebook(settings)
    output = nbformat.read(settings["output_path"], as_version=4)
    assert output.metadata.astra_execution.status == "failed"
    assert output.metadata.astra_execution.cells[0].error_type == "CellTimeoutError"


def test_actual_tool_background_poll_and_read(tmp_path):
    from agent.runtime.tools.code import register_code_tools
    from agent.sandbox.local import LocalSandbox
    path = notebook(tmp_path, [nbformat.v4.new_code_cell("print('background verified')")])
    destination = tmp_path / "background.ipynb"

    async def scenario():
        registry = ToolRegistry()
        register_code_tools(registry, LocalSandbox(workdir=str(tmp_path)))
        started = await registry.execute("notebook_execute", {
            "path": str(path), "output_path": str(destination), "background": True,
            "kernel_name": "python3",
        }, task_id="notebook-test")
        process_id = json.loads(started["output"])["process_id"]
        try:
            polled = await registry.execute("process_poll", {"process_id": process_id, "wait_ms": 30000})
            state = json.loads(polled["output"])
            assert state["status"] == "completed", await registry.execute("process_read", {"process_id": process_id, "byte_offset": 0})
            assert state["exit_code"] == 0, state
            read = await registry.execute("process_read", {"process_id": process_id, "byte_offset": 0})
            assert "notebook_finished" in read["output"]
        finally:
            await registry.execute("process_cancel", {"process_id": process_id})
    asyncio.run(scenario())
    output = nbformat.read(destination, as_version=4)
    assert output.cells[0].outputs[0].text == "background verified\n"


@pytest.mark.parametrize("exit_code,expected", [(0, "completed"), (3, "failed")])
def test_background_stderr_does_not_override_exit_code(tmp_path, exit_code, expected):
    from agent.runtime.tools.code import register_code_tools
    from agent.sandbox.local import LocalSandbox

    async def scenario():
        registry = ToolRegistry()
        register_code_tools(registry, LocalSandbox(workdir=str(tmp_path)))
        started = await registry.execute("execute_python", {
            "code": f"import sys; print('diagnostic', file=sys.stderr); sys.exit({exit_code})",
            "background": True,
        })
        process_id = json.loads(started["output"])["process_id"]
        polled = await registry.execute("process_poll", {"process_id": process_id, "wait_ms": 30000})
        state = json.loads(polled["output"])
        assert state["status"] == expected
        assert state["exit_code"] == exit_code
        read = await registry.execute("process_read", {"process_id": process_id, "stream": "stderr"})
        assert "diagnostic" in read["output"]
    asyncio.run(scenario())
