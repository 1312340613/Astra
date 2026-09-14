import asyncio
import base64

import pytest

from agent.runtime.tools.code import register_code_tools
from agent.runtime.tools.registry import ToolRegistry
from agent.sandbox.docker import DockerSandbox
from agent.sandbox.local import LocalSandbox


def test_auto_routes_linux_commands_and_paths_to_wsl(monkeypatch):
    monkeypatch.setattr("agent.sandbox.local.sys.platform", "win32")

    assert LocalSandbox.resolve_shell_environment("ls -la /home/example", "auto") == "wsl"
    assert LocalSandbox.resolve_shell_environment("ps aux | grep python", "auto") == "wsl"
    assert LocalSandbox.resolve_shell_environment("Get-ChildItem -Force", "auto") == "windows"
    assert LocalSandbox.resolve_shell_environment("dir C:\\\\Users", "auto") == "windows"


def test_explicit_shell_environment_overrides_detection(monkeypatch):
    monkeypatch.setattr("agent.sandbox.local.sys.platform", "win32")

    assert LocalSandbox.resolve_shell_environment("ls", "windows") == "windows"
    assert LocalSandbox.resolve_shell_environment("dir", "wsl") == "wsl"


def test_auto_uses_posix_on_non_windows(monkeypatch):
    monkeypatch.setattr("agent.sandbox.local.sys.platform", "darwin")

    assert LocalSandbox.resolve_shell_environment("pwd", "auto") == "posix"


def test_explicit_windows_shell_is_rejected_on_posix(monkeypatch):
    monkeypatch.setattr("agent.sandbox.local.sys.platform", "darwin")

    with pytest.raises(ValueError, match="unavailable"):
        LocalSandbox.resolve_shell_environment("dir", "windows")


def test_explicit_wsl_shell_is_rejected_on_posix(monkeypatch):
    monkeypatch.setattr("agent.sandbox.local.sys.platform", "darwin")

    with pytest.raises(ValueError, match="unavailable"):
        LocalSandbox.resolve_shell_environment("pwd", "wsl")


def test_explicit_posix_shell_is_rejected_on_windows(monkeypatch):
    monkeypatch.setattr("agent.sandbox.local.sys.platform", "win32")

    with pytest.raises(ValueError, match="unavailable"):
        LocalSandbox.resolve_shell_environment("pwd", "posix")


def test_docker_allows_auto_and_posix(monkeypatch):
    sandbox = DockerSandbox()

    async def fake_ensure_image():
        pass

    async def fake_exec(command, on_output=None):
        return {"output": command, "error": "", "exit_code": 0}

    monkeypatch.setattr(sandbox, "_ensure_image", fake_ensure_image)
    monkeypatch.setattr(sandbox, "_exec", fake_exec)

    assert asyncio.run(sandbox.execute_shell("pwd", "auto"))["output"] == "pwd"
    assert asyncio.run(sandbox.execute_shell("pwd", "posix"))["output"] == "pwd"


def test_posix_shell_executes_with_native_bash(monkeypatch):
    class FakeProcess:
        def __init__(self):
            self.returncode = 0
            self.stdin = None
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        async def wait(self):
            return self.returncode

    created = []

    async def fake_exec(*args, **kwargs):
        created.append(args)
        return FakeProcess()

    async def unexpected_shell(*args, **kwargs):
        raise AssertionError("POSIX commands must execute with Bash")

    monkeypatch.setattr("agent.sandbox.local.sys.platform", "darwin")
    monkeypatch.setattr("agent.sandbox.local.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("agent.sandbox.local.asyncio.create_subprocess_shell", unexpected_shell)

    result = asyncio.run(LocalSandbox().execute_shell("pwd", "auto"))

    assert created == [("/bin/bash", "-lc", "pwd")]
    assert result["environment"] == "posix"


def test_wsl_args_use_configured_distro(monkeypatch):
    monkeypatch.setenv("AGENT_WSL_DISTRO", "Ubuntu-24.04")

    encoded = base64.b64encode("ls /home".encode("utf-8")).decode("ascii")
    assert LocalSandbox._wsl_args("ls /home") == [
        "wsl.exe", "-d", "Ubuntu-24.04", "--", "bash", "-lc",
        f"set -o pipefail; echo {encoded} | base64 -d | bash",
    ]


def test_decode_output_handles_utf16_wsl_prefix_and_utf8_linux_error():
    raw = (
        "wsl: localhost proxy warning\r\n".encode("utf-16-le")
        + b"strings: /home/missing.png: No such file or directory\n"
    )

    decoded = LocalSandbox._decode_output(raw)

    assert "wsl: localhost proxy warning" in decoded
    assert "strings: /home/missing.png: No such file or directory" in decoded
    assert "瑳楲杮" not in decoded


def test_nonzero_shell_exit_is_reported_as_tool_error():
    class FailingSandbox:
        async def execute_shell(self, command: str, environment: str = "auto") -> dict:
            return {
                "output": "",
                "error": "command not found",
                "exit_code": 1,
                "environment": "windows",
            }

    registry = ToolRegistry()
    register_code_tools(registry, FailingSandbox())

    result = asyncio.run(registry.execute("execute_shell", {"command": "ls"}))

    assert result["output"] == ""
    assert "Shell command failed in windows environment" in result["error"]
    assert "command not found" in result["error"]
