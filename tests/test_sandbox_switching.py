import asyncio
import json

from agent.cli.sandbox_preferences import (
    execute_sandbox_command,
    load_selected_sandbox_mode,
    resolve_startup_sandbox_mode,
)
from agent.sandbox.docker import DockerSandbox
from agent.sandbox.local import LocalSandbox
from agent.sandbox.router import SandboxRouter


class FakeLocalSandbox(LocalSandbox):
    async def execute_python(self, code: str) -> dict:
        return {"output": f"local-python:{code}", "error": "", "exit_code": 0}

    async def execute_shell(self, command: str, environment: str = "auto") -> dict:
        return {"output": f"local-shell:{environment}:{command}", "error": "", "exit_code": 0}

    async def execute_wsl_persistent_shell_stream(self, command: str, on_output=None) -> dict:
        return await self.execute_shell(command, environment="wsl")


class FakeDockerSandbox(DockerSandbox):
    async def execute_python(self, code: str) -> dict:
        return {"output": f"docker-python:{code}", "error": "", "exit_code": 0}

    async def execute_shell(self, command: str, environment: str = "auto") -> dict:
        return {"output": f"docker-shell:{environment}:{command}", "error": "", "exit_code": 0}

    async def close(self):
        return None


def _router() -> SandboxRouter:
    return SandboxRouter(
        FakeDockerSandbox(workdir="."),
        local_factory=lambda: FakeLocalSandbox(workdir="."),
        docker_factory=lambda: FakeDockerSandbox(workdir="."),
    )


def test_sandbox_router_switches_existing_code_tools_immediately():
    router = _router()

    docker_result = asyncio.run(router.execute_shell("where-am-i"))
    router.switch("off")
    local_result = asyncio.run(router.execute_shell("where-am-i"))
    router.switch("on")
    docker_again = asyncio.run(router.execute_python("print(1)"))

    assert docker_result["output"] == "docker-shell:auto:where-am-i"
    assert local_result["output"] == "local-shell:auto:where-am-i"
    assert docker_again["output"] == "docker-python:print(1)"
    assert router.enabled is True


def test_sandbox_router_runs_wsl_sidecar_without_disabling_docker():
    router = _router()

    result = asyncio.run(router.execute_wsl_shell("pwd"))

    assert result["output"] == "local-shell:wsl:pwd"
    assert router.mode == "docker"
    assert router.enabled is True
    assert "local" in router._sandboxes


def test_sandbox_router_runs_generic_persistent_bash_without_disabling_docker():
    router = _router()

    result = asyncio.run(router.execute_persistent_bash_stream("pwd"))

    assert result["output"] == "local-shell:wsl:pwd"
    assert router.mode == "docker"
    assert router.enabled is True


def test_sandbox_command_persists_without_overwriting_other_settings(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"selected_model": "deepseek-v4-flash"}), encoding="utf-8")
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings))
    router = _router()

    output, error = execute_sandbox_command(router, ["off"])

    assert error == ""
    assert "switched OFF immediately" in output
    assert router.mode == "local"
    assert load_selected_sandbox_mode() == "local"
    assert resolve_startup_sandbox_mode("true") == "false"
    saved = json.loads(settings.read_text(encoding="utf-8"))
    assert saved["selected_model"] == "deepseek-v4-flash"
    assert saved["sandbox_mode"] == "local"


def test_sandbox_status_explains_host_safety_boundary():
    router = _router()
    router.switch("off")

    output, error = execute_sandbox_command(router, ["status"])

    assert error == ""
    assert "Sandbox: OFF" in output
    assert "guarded host execution" in output
