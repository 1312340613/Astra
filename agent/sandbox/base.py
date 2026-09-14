"""Sandbox protocol shared by execution backends."""

from abc import ABC, abstractmethod
from collections.abc import Callable


OutputCallback = Callable[[str, str], None]


class Sandbox(ABC):
    timeout: int
    workdir: str

    @abstractmethod
    async def execute_python(self, code: str) -> dict:
        ...

    @abstractmethod
    async def execute_shell(self, command: str, environment: str = "auto") -> dict:
        ...

    async def execute_python_stream(
        self,
        code: str,
        on_output: OutputCallback | None = None,
    ) -> dict:
        result = await self.execute_python(code)
        if on_output is not None:
            if result.get("output"):
                on_output("stdout", str(result["output"]))
            if result.get("error"):
                on_output("stderr", str(result["error"]))
        return result

    async def execute_shell_stream(
        self,
        command: str,
        environment: str = "auto",
        on_output: OutputCallback | None = None,
    ) -> dict:
        result = await self.execute_shell(command, environment=environment)
        if on_output is not None:
            if result.get("output"):
                on_output("stdout", str(result["output"]))
            if result.get("error"):
                on_output("stderr", str(result["error"]))
        return result
