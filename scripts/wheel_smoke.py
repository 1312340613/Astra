"""Check installed wheel contents offline without editable/source import fallback.

Run after locked dependency installation has populated uv's build cache. Runtime
dependencies come from the current interpreter's site-packages, but Python starts
with -I -S: neither PYTHONPATH nor editable .pth files can rescue missing modules.
The wheel itself is installed into a temporary target without dependency resolution.
"""

from __future__ import annotations

import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SMOKE_CODE = r"""
import importlib
import importlib.metadata
import importlib.resources
import pathlib
import sys

installed = pathlib.Path(sys.argv[1]).resolve()
sys.path[:0] = sys.argv[1:]

def deny_network(event, arguments):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo"}:
        raise RuntimeError("Network access is forbidden during wheel smoke")

sys.addaudithook(deny_network)
for name in ("agent.runtime.session_recall", "agent.cli.main", "agent.cli.backend",
             "agent.runtime.context_index.embedding_runtime",
             "agent.runtime.context_index.embedding_worker",
             "agent.evals.context_index_benchmark.resources"):
    module = importlib.import_module(name)
    assert pathlib.Path(module.__file__).resolve().is_relative_to(installed), name
assert "mlx.core" not in sys.modules, "importing the client must not load MLX"
catalog = importlib.resources.files("agent").joinpath("_data/models.yaml")
assert catalog.is_file(), "wheel is missing its model catalog"
contract = importlib.resources.files("agent.runtime").joinpath("astra.md")
assert contract.is_file(), "wheel is missing its core rules"
prompts = importlib.import_module("agent.runtime.prompts")
assert prompts.AGENT_CORE_PROMPT == contract.read_text(encoding="utf-8").rstrip("\n")
skills = importlib.import_module("agent.runtime.skills").SkillStore()
assert skills.list()[0]["name"] == "astra-core"
assert prompts.AGENT_CORE_PROMPT in skills.view("astra-core")
models = importlib.import_module("agent.cli.models")
assert models.model_profiles(), "installed model catalog is empty"
quality = importlib.import_module("agent.evals.context_index_benchmark.quality")
assert pathlib.Path(quality.__file__).resolve().is_relative_to(installed)
assert len(quality.load_fixture()["scenarios"]) == 24, "wheel is missing the memory quality fixture"
distribution = importlib.metadata.distribution("agent-lab-local")
assert pathlib.Path(distribution.locate_file("")).resolve() == installed
entry_points = {entry.name: entry for entry in distribution.entry_points if entry.group == "console_scripts"}
for name in ("astra", "agent-lab", "agent-lab-backend", "agent-lab-eval"):
    assert callable(entry_points[name].load()), name
print("Wheel smoke passed: installed CLI, session_recall, catalog, memory runtime/fixture and entry points")
"""


def run(command: list[str], *, cwd: Path) -> None:
    completed = subprocess.run(command, cwd=cwd, check=False)
    if completed.returncode:
        raise SystemExit(f"Wheel smoke failed with exit code {completed.returncode}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="astra-wheel-") as temporary:
        root = Path(temporary)
        wheels = root / "wheels"
        installed = root / "installed"
        run([
            "uv", "build", "--wheel", "--offline", "--no-python-downloads",
            "--python", sys.executable, "--out-dir", str(wheels), str(PROJECT_ROOT),
        ], cwd=root)
        candidates = list(wheels.glob("*.whl"))
        if len(candidates) != 1:
            raise SystemExit("Wheel smoke expected exactly one freshly built wheel")
        run([
            "uv", "pip", "install", "--offline", "--no-deps", "--no-python-downloads",
            "--python", sys.executable, "--target", str(installed), str(candidates[0]),
        ], cwd=root)
        dependency_paths = sorted({sysconfig.get_paths()[key] for key in ("purelib", "platlib")})
        run([sys.executable, "-I", "-S", "-c", SMOKE_CODE, str(installed), *dependency_paths], cwd=root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
