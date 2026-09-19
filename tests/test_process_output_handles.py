import asyncio
import json
import shlex
import sys

import pytest

from agent.runtime.tools.code import register_code_tools
from agent.runtime.tools.registry import ToolRegistry
from agent.sandbox.local import LocalSandbox


@pytest.mark.skipif(sys.platform == "win32", reason="native Bash integration")
@pytest.mark.parametrize("foreground_yield_ms", [0, 10000])
def test_truncated_foreground_output_has_usable_process_reader(tmp_path, foreground_yield_ms):
    async def scenario():
        registry = ToolRegistry(artifact_dir=tmp_path / "artifacts")
        register_code_tools(registry, LocalSandbox(workdir=str(tmp_path), max_output_bytes=64))
        command = shlex.join([sys.executable, "-c", "print('begin-' + 'x' * 200 + '-end')"])
        result = await registry.execute("execute_shell", {"command": command, "foreground_yield_ms": foreground_yield_ms})
        assert not result["error"], result
        marker = "[Read full output: "
        handle = json.loads(result["output"].split(marker)[1].split("]")[0])
        assert handle["tool"] == "process_read"
        readback = await registry.execute(handle["tool"], handle["arguments"])
        assert not readback["error"], readback
        assert "begin-" + "x" * 200 + "-end" in json.loads(readback["output"])["content"]
    asyncio.run(scenario())
