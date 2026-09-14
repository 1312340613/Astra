from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from agent.sandbox.docker import DockerSandbox

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SELECTED_GROUPS = ("mcp", "server", "tracing", "dev")
SANDBOX_IMAGE = os.environ.get(
    "ASTRA_SANDBOX_TEST_IMAGE", "astra-sandbox:agent-system-dev"
)


def _requirement_contract(
    specs: list[str], *, require_private_pyright_runtime: bool = False
) -> dict[str, tuple[frozenset[str], str, str | None, str | None]]:
    contract = {}
    for spec in specs:
        requirement = Requirement(spec)
        name = canonicalize_name(requirement.name)
        extras = {canonicalize_name(extra) for extra in requirement.extras}
        if require_private_pyright_runtime and name == "pyright":
            extras.add("nodejs")
        assert name not in contract, f"duplicate sandbox requirement: {name}"
        contract[name] = (
            frozenset(extras),
            str(requirement.specifier),
            str(requirement.marker) if requirement.marker is not None else None,
            requirement.url,
        )
    return contract


def _requirement_specs(text: str) -> list[str]:
    return [
        line
        for raw_line in text.splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
    ]


def _docker_or_skip() -> str:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable on this host")
    available = subprocess.run(
        [docker, "info", "--format", "{{.OSType}}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if available.returncode != 0:
        pytest.skip("Docker daemon is unavailable on this host")
    if available.stdout.strip() != "linux":
        pytest.skip("The sandbox image requires a Linux Docker engine")
    return docker


def _docker_with_image_or_skip() -> str:
    docker = _docker_or_skip()
    inspected = subprocess.run(
        [docker, "image", "inspect", SANDBOX_IMAGE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if inspected.returncode != 0:
        pytest.skip(f"sandbox image is unavailable: {SANDBOX_IMAGE}")
    return docker


def test_sandbox_requirements_cover_complete_python_environment() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    required_specs = list(project["project"]["dependencies"])
    optional = project["project"]["optional-dependencies"]
    for group in SELECTED_GROUPS:
        required_specs.extend(optional[group])

    required = _requirement_contract(
        required_specs, require_private_pyright_runtime=True
    )
    installed = _requirement_contract(
        _requirement_specs(
            (PROJECT_ROOT / "docker/sandbox/requirements.txt").read_text(
                encoding="utf-8"
            )
        )
    )

    assert installed == required


def test_pillow_requirement_is_identical_across_install_surfaces() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_requirements = _requirement_contract(list(project["project"]["dependencies"]))
    compatibility_requirements = _requirement_contract(
        _requirement_specs((PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8"))
    )
    sandbox_requirements = _requirement_contract(
        _requirement_specs(
            (PROJECT_ROOT / "docker/sandbox/requirements.txt").read_text(encoding="utf-8")
        )
    )

    expected = project_requirements["pillow"]
    assert compatibility_requirements["pillow"] == expected
    assert sandbox_requirements["pillow"] == expected


def test_sandbox_dockerfile_installs_required_tools_without_docker_cli() -> None:
    dockerfile = (PROJECT_ROOT / "docker/sandbox/Dockerfile").read_text(encoding="utf-8")
    for executable_package in ("git", "ripgrep", "curl"):
        assert re.search(rf"\b{re.escape(executable_package)}\b", dockerfile)
    for forbidden_package in ("docker.io", "docker-ce", "docker-ce-cli", "podman"):
        assert forbidden_package not in dockerfile


def test_default_image_and_runtime_isolation_remain_unchanged() -> None:
    source = (PROJECT_ROOT / "agent/sandbox/docker.py").read_text(encoding="utf-8")
    assert 'IMAGE = "python:3.12-slim"' in source

    sandbox = DockerSandbox(
        workdir=str(PROJECT_ROOT), memory_limit="768m", image=SANDBOX_IMAGE
    )
    for args in (sandbox._build_args("true"), sandbox._build_start_args()):
        assert ["--workdir", "/workspace"] == args[
            args.index("--workdir") : args.index("--workdir") + 2
        ]
        assert ["-v", f"{PROJECT_ROOT}:/workspace"] == args[
            args.index("-v") : args.index("-v") + 2
        ]
        assert ["--memory", "768m"] == args[
            args.index("--memory") : args.index("--memory") + 2
        ]
        assert ["--network", "none"] == args[
            args.index("--network") : args.index("--network") + 2
        ]
        assert "no-new-privileges:true" in args
        assert ["--cap-drop", "ALL"] == args[
            args.index("--cap-drop") : args.index("--cap-drop") + 2
        ]
        assert ["--pids-limit", "100"] == args[
            args.index("--pids-limit") : args.index("--pids-limit") + 2
        ]


def test_pyright_runs_real_analysis_offline_with_private_runtime(tmp_path: Path) -> None:
    docker = _docker_with_image_or_skip()
    sample = tmp_path / "pyright_smoke.py"
    sample.write_text(
        "def twice(value: int) -> int:\n"
        "    return value * 2\n"
        "\n"
        "result: int = twice(21)\n",
        encoding="utf-8",
    )
    common = [docker, "run", "--rm", "--network", "none", SANDBOX_IMAGE]

    version = subprocess.run(
        [*common, "pyright", "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert version.returncode == 0, version.stderr
    assert re.search(r"\bpyright \d+\.\d+\.\d+\b", version.stdout, re.IGNORECASE)

    analysis = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "--network",
            "none",
            "--mount",
            f"type=bind,src={tmp_path},dst=/pyright-smoke,readonly",
            SANDBOX_IMAGE,
            "pyright",
            "--outputjson",
            "/pyright-smoke/pyright_smoke.py",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert analysis.returncode == 0, analysis.stderr
    report = json.loads(analysis.stdout)
    assert report["summary"]["filesAnalyzed"] == 1
    assert report["summary"]["errorCount"] == 0


def test_development_image_does_not_expose_forbidden_clis() -> None:
    docker = _docker_with_image_or_skip()
    forbidden = (
        "node",
        "npm",
        "npx",
        "corepack",
        "docker",
        "podman",
        "chromium",
        "chromium-browser",
        "google-chrome",
        "firefox",
        "playwright",
        "textual",
    )
    for executable in forbidden:
        probe = subprocess.run(
            [
                docker,
                "run",
                "--rm",
                "--network",
                "none",
                SANDBOX_IMAGE,
                "sh",
                "-c",
                f"command -v {executable}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        assert probe.returncode != 0, f"forbidden sandbox CLI is exposed: {executable}"


def test_dockerignore_exposes_only_required_sandbox_build_inputs(
    tmp_path: Path,
) -> None:
    dockerignore = PROJECT_ROOT / ".dockerignore"
    assert dockerignore.is_file(), "repository-root .dockerignore is required"

    docker = _docker_or_skip()

    context = tmp_path / "context"
    output = tmp_path / "output"
    dockerfile = context / "docker/sandbox/Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("FROM scratch\nCOPY . /\n", encoding="utf-8")
    (context / "docker/sandbox/requirements.txt").write_text(
        "sentinel\n", encoding="utf-8"
    )
    shutil.copyfile(dockerignore, context / ".dockerignore")

    excluded = (
        ".env",
        ".git/config",
        ".venv/bin/python",
        ".astra/settings.json",
        ".agent_system/state.json",
        "node_modules/package/index.js",
        "agent/core/agent.py",
        "README.md",
    )
    for relative in excluded:
        path = context / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("non-secret sentinel\n", encoding="utf-8")

    built = subprocess.run(
        [
            docker,
            "build",
            "--network",
            "none",
            "--file",
            str(dockerfile),
            "--output",
            f"type=local,dest={output}",
            str(context),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    included = {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file()
    }
    assert included == {
        "docker/sandbox/Dockerfile",
        "docker/sandbox/requirements.txt",
    }
