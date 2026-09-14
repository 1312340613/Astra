"""Persistent WSL PTY lifecycle tests with a deterministic subprocess double."""

from __future__ import annotations

import asyncio
import base64
import re

from agent.sandbox.wsl_pty import PersistentWslShell


class _FakeStdin:
    def __init__(self, process: "_FakeProcess") -> None:
        self.process = process

    def write(self, payload: bytes) -> None:
        self.process.receive(payload.decode("utf-8"))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.process.returncode = 0
        self.process.stdout.feed_eof()


class _FakeProcess:
    instances = 0

    def __init__(self) -> None:
        type(self).instances += 1
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader()
        self.stdin = _FakeStdin(self)
        self.commands: list[str] = []
        self.cwd = "/workspace"
        self.wait_calls = 0

    def receive(self, script: str) -> None:
        markers = re.findall(r"(__ASTRA_WSL_PTY_RESULT_[0-9a-f]+__:)", script)
        marker = markers[-1]
        if "__astra_cmd=" not in script:
            echoed = f"printf '\\n{marker}0\\n'\n".encode("utf-8")
            self.stdout.feed_data(echoed + f"\n{marker}0\n".encode("utf-8"))
            return
        encoded = re.search(r"printf %s ([A-Za-z0-9+/=]+) \| base64", script).group(1)
        command = base64.b64decode(encoded).decode("utf-8")
        self.commands.append(command)
        if command.startswith("cd "):
            self.cwd = command[3:].strip()
            output = b""
        else:
            output = f"{self.cwd}\n".encode("utf-8")
        self.stdout.feed_data(output + f"{marker}0\n".encode("utf-8"))

    def kill(self) -> None:
        self.returncode = -9
        self.stdout.feed_eof()

    async def wait(self) -> int:
        self.wait_calls += 1
        return self.returncode or 0


def test_persistent_wsl_shell_reuses_pty_and_preserves_state(monkeypatch) -> None:
    _FakeProcess.instances = 0

    async def spawn(*args, **kwargs):
        return _FakeProcess()

    monkeypatch.setattr("agent.sandbox.wsl_pty.asyncio.create_subprocess_exec", spawn)

    async def scenario() -> None:
        shell = PersistentWslShell(
            ["fake-wsl", "script"],
            workdir=".",
            timeout=1,
            max_output_bytes=10_000,
        )
        first = await shell.execute("cd /tmp")
        second = await shell.execute("pwd")
        assert first["exit_code"] == 0
        assert second["output"] == "/tmp"
        assert _FakeProcess.instances == 1
        await shell.close()

    asyncio.run(scenario())


def test_persistent_bash_shell_reports_configured_posix_environment(monkeypatch) -> None:
    """The transport is platform-neutral even though its compatibility name remains WSL."""
    async def spawn(*args, **kwargs):
        return _FakeProcess()

    monkeypatch.setattr("agent.sandbox.wsl_pty.asyncio.create_subprocess_exec", spawn)

    async def scenario() -> None:
        shell = PersistentWslShell(
            ["script", "-q", "/dev/null", "/bin/bash", "-i"],
            workdir=".",
            timeout=1,
            max_output_bytes=10_000,
            environment="posix",
        )
        result = await shell.execute("pwd")

        assert result["environment"] == "posix"
        assert "WSL" not in str(result)
        await shell.close()

    asyncio.run(scenario())


def test_persistent_wsl_shell_reports_exit_and_resets_after_shell_exit(monkeypatch) -> None:
    class ExitingProcess(_FakeProcess):
        def receive(self, script: str) -> None:
            markers = re.findall(r"(__ASTRA_WSL_PTY_RESULT_[0-9a-f]+__:)", script)
            marker = markers[-1]
            if "__astra_cmd=" not in script:
                self.stdout.feed_data(f"\n{marker}0\n".encode("utf-8"))
                return
            self.stdout.feed_data(b"partial output\n")
            self.returncode = 7
            self.stdout.feed_eof()

    processes: list[_FakeProcess] = []

    async def spawn(*args, **kwargs):
        process = ExitingProcess()
        processes.append(process)
        return process

    monkeypatch.setattr("agent.sandbox.wsl_pty.asyncio.create_subprocess_exec", spawn)

    async def scenario() -> None:
        shell = PersistentWslShell(
            ["fake-wsl", "script"],
            workdir=".",
            timeout=1,
            max_output_bytes=5,
        )
        result = await shell.execute("exit")
        assert result["exit_code"] == -1
        assert "persistent bash shell was reset" in result["output"]
        assert len(processes) == 1
        await shell.close()

    asyncio.run(scenario())


def test_close_nowait_reaps_the_killed_pty_on_the_running_loop(monkeypatch) -> None:
    processes: list[_FakeProcess] = []

    async def spawn(*args, **kwargs):
        process = _FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr("agent.sandbox.wsl_pty.asyncio.create_subprocess_exec", spawn)

    async def scenario() -> None:
        shell = PersistentWslShell(
            ["fake-wsl", "script"],
            workdir=".",
            timeout=1,
            max_output_bytes=10_000,
        )
        await shell.execute("pwd")
        shell.close_nowait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert processes[0].wait_calls == 1

    asyncio.run(scenario())
