"""Runtime-switchable sandbox delegation."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable

from .base import OutputCallback, Sandbox
from .docker import DockerSandbox
from .local import LocalSandbox


class SandboxRouter(Sandbox):
    """Route execution to Docker or the guarded local host at runtime."""

    def __init__(
        self,
        initial: Sandbox,
        *,
        local_factory: Callable[[], LocalSandbox],
        docker_factory: Callable[[], DockerSandbox],
    ):
        self._local_factory = local_factory
        self._docker_factory = docker_factory
        self._sandboxes: dict[str, Sandbox] = {}
        self._sidecar_lock = asyncio.Lock()
        mode = self._mode_for(initial)
        self._sandboxes[mode] = initial
        self._mode = mode
        self.timeout = getattr(initial, "timeout", 30)
        self.workdir = getattr(initial, "workdir", ".")

    @staticmethod
    def _mode_for(sandbox: Sandbox) -> str:
        return "docker" if isinstance(sandbox, DockerSandbox) else "local"

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def enabled(self) -> bool:
        return self._mode == "docker"

    @property
    def current(self) -> Sandbox:
        return self._sandboxes[self._mode]

    @property
    def description(self) -> str:
        if self.enabled:
            return "DockerSandbox (ON; isolated container)"
        return "LocalSandbox (OFF; guarded host execution)"

    def switch(self, mode: str) -> str:
        normalized = mode.strip().lower()
        aliases = {
            "on": "docker",
            "true": "docker",
            "docker": "docker",
            "off": "local",
            "false": "local",
            "local": "local",
            "host": "local",
        }
        selected = aliases.get(normalized)
        if selected is None:
            raise ValueError("Sandbox mode must be on/off (aliases: docker/local/host)")
        if selected not in self._sandboxes:
            self._sandboxes[selected] = (
                self._docker_factory() if selected == "docker" else self._local_factory()
            )
        self._mode = selected
        active = self.current
        self.timeout = getattr(active, "timeout", self.timeout)
        self.workdir = getattr(active, "workdir", self.workdir)
        return self._mode

    async def execute_python(self, code: str) -> dict:
        return await self.current.execute_python(code)

    async def execute_shell(self, command: str, environment: str = "auto") -> dict:
        return await self.current.execute_shell(command, environment=environment)

    async def execute_python_stream(
        self,
        code: str,
        on_output: OutputCallback | None = None,
    ) -> dict:
        return await self.current.execute_python_stream(code, on_output=on_output)

    async def execute_shell_stream(
        self,
        command: str,
        environment: str = "auto",
        on_output: OutputCallback | None = None,
    ) -> dict:
        return await self.current.execute_shell_stream(
            command,
            environment=environment,
            on_output=on_output,
        )

    async def execute_persistent_bash_stream(
        self,
        command: str,
        on_output: OutputCallback | None = None,
    ) -> dict:
        """Run one command in the Minimal-owned persistent Bash sidecar.

        Minimal Mode keeps PTC on Docker, but its DSH-compatible ``bash``
        surface needs a persistent Bash shell. The sidecar is deliberately
        separate from ``current``: a bash call never silently turns Docker off
        for the rest of the agent session, and repeated calls reuse the same
        owner-scoped PTY.
        """
        async with self._sidecar_lock:
            local = self._sandboxes.get("local")
            if local is None:
                local = self._local_factory()
                self._sandboxes["local"] = local
        persistent_execute = getattr(local, "execute_persistent_bash_stream", None)
        legacy_persistent_execute = getattr(local, "execute_wsl_persistent_shell_stream", None)
        # A lightweight LocalSandbox double may override only the historic
        # method while inheriting the production generic implementation. Honor
        # that explicit override so older tests/integrations keep their seam.
        if (
            isinstance(local, LocalSandbox)
            and type(local).execute_persistent_bash_stream is LocalSandbox.execute_persistent_bash_stream
            and type(local).execute_wsl_persistent_shell_stream
            is not LocalSandbox.execute_wsl_persistent_shell_stream
        ):
            persistent_execute = legacy_persistent_execute
        if persistent_execute is None:
            # Keep non-LocalSandbox doubles and older implementations usable.
            persistent_execute = legacy_persistent_execute
        if persistent_execute is not None:
            return await persistent_execute(command, on_output=on_output)
        execute = getattr(local, "execute_shell_stream", None)
        if execute is None:
            # Keep lightweight test doubles and older sandbox implementations
            # usable while the production LocalSandbox supplies the PTY.
            raise RuntimeError("persistent Bash sidecar does not support shell execution")
        environment = "wsl" if sys.platform == "win32" else "posix"
        return await execute(command, environment=environment, on_output=on_output)

    async def execute_wsl_shell_stream(
        self,
        command: str,
        on_output: OutputCallback | None = None,
    ) -> dict:
        """Compatibility alias for :meth:`execute_persistent_bash_stream`."""
        return await self.execute_persistent_bash_stream(command, on_output=on_output)

    async def execute_persistent_bash(self, command: str) -> dict:
        return await self.execute_persistent_bash_stream(command)

    async def execute_wsl_shell(self, command: str) -> dict:
        return await self.execute_persistent_bash_stream(command)

    def close_persistent_bash(self) -> None:
        local = self._sandboxes.get("local")
        close = getattr(local, "close_persistent_bash", None)
        if close is None:
            close = getattr(local, "close_wsl_shell", None)
        if close is not None:
            close()

    def close_wsl_shell(self) -> None:
        """Compatibility alias for :meth:`close_persistent_bash`."""
        self.close_persistent_bash()

    async def close(self) -> None:
        seen: set[int] = set()
        for sandbox in self._sandboxes.values():
            if id(sandbox) in seen:
                continue
            seen.add(id(sandbox))
            close = getattr(sandbox, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result
