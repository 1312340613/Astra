"""Check a worktree's Git root, Python imports and optional TUI dependencies.

No installs or symlinks are created. Run before edits, then run the relevant
baseline tests from the same directory with the reported interpreter via -m.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib


_PROBE = """
import importlib, importlib.metadata, json, sys
request = json.loads(sys.argv[1])
result = {"executable": sys.executable, "prefix": sys.prefix, "modules": {}, "packages": {}, "errors": []}
for name in request["modules"]:
    try:
        module = importlib.import_module(name)
        result["modules"][name] = list(getattr(module, "__path__", [])) or [getattr(module, "__file__", None)]
    except Exception as exc:
        result["errors"].append(f"Cannot import {name}: {type(exc).__name__}: {exc}")
for name in request["packages"]:
    try:
        result["packages"][name] = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        result["errors"].append(f"Missing Python package: {name}")
print("ASTRA_PREFLIGHT=" + json.dumps(result))
"""


def _run(command: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if completed.returncode:
        raise ValueError(f"{command[0]} exited {completed.returncode}: {completed.stderr[-1200:]}")
    return completed.stdout.strip()


def _check_shared_dependencies(workspace: Path, source: Path, *, node: bool, errors: list[str]) -> None:
    if workspace.resolve() == source.resolve():
        return
    manifest = "package.json" if node else "pyproject.toml"
    left, right = workspace / manifest, source / manifest
    if not left.exists():
        return
    if not right.exists():
        errors.append(f"Cannot verify shared environment: source manifest missing at {right}")
        return
    if node:
        keys = ("dependencies", "devDependencies", "engines", "overrides")
        expected, actual = json.loads(left.read_text()), json.loads(right.read_text())
    else:
        keys = ("dependencies", "optional-dependencies", "requires-python")
        expected = tomllib.loads(left.read_text()).get("project", {})
        actual = tomllib.loads(right.read_text()).get("project", {})
    if any(expected.get(key) != actual.get(key) for key in keys):
        errors.append(f"Shared dependencies differ from worktree: {left} versus {right}; use an isolated environment")
    for filename in (("package-lock.json",) if node else ("uv.lock", "requirements.txt")):
        expected_lock, actual_lock = workspace / filename, source / filename
        if expected_lock.exists() and (
            not actual_lock.exists() or expected_lock.read_bytes() != actual_lock.read_bytes()
        ):
            errors.append(f"Shared dependency lock differs or is missing: {filename}")


def check_worktree(
    workspace: Path, *, python: str, modules: list[str], packages: list[str], check_tui: bool = False,
) -> dict:
    root = workspace.expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    result: dict = {"workspace_root": str(root), "errors": errors, "warnings": warnings}
    try:
        git_root = Path(_run(["git", "rev-parse", "--show-toplevel"], root)).resolve()
        result["git_root"] = str(git_root)
        if git_root != root:
            errors.append(f"Expected worktree root {root}, but Git resolves to {git_root}")
        output = _run([python, "-c", _PROBE, json.dumps({"modules": modules, "packages": packages})], root)
        probe = json.loads(next(line.removeprefix("ASTRA_PREFLIGHT=") for line in reversed(output.splitlines())
                                if line.startswith("ASTRA_PREFLIGHT=")))
        result["python"] = probe
        errors.extend(probe["errors"])
        for module, locations in probe["modules"].items():
            if not locations or any(not path or not Path(path).resolve().is_relative_to(root) for path in locations):
                errors.append(f"Project module {module} resolves outside the worktree: {locations}")
        environment = Path(probe["prefix"]).resolve()
        if (environment / "pyvenv.cfg").exists():
            source = environment.parent
            _check_shared_dependencies(root, source, node=False, errors=errors)
            if source != root:
                warnings.append("Shared Python environment: verified cwd/-m imports only. Console entry points may use the original checkout; do not modify shared dependencies.")
        else:
            warnings.append("Selected interpreter has no virtual environment; requested packages and imports were checked, but dependency isolation was not established.")
        if check_tui:
            tui = root / "ui-tui"
            manifest = json.loads((tui / "package.json").read_text())
            modules_path = tui / "node_modules"
            if not modules_path.is_dir():
                errors.append(f"Missing TUI dependencies: {modules_path}")
            else:
                source = modules_path.resolve().parent
                _check_shared_dependencies(tui, source, node=True, errors=errors)
                required = {**manifest.get("dependencies", {}), **manifest.get("devDependencies", {})}
                missing = [name for name in required if not (modules_path / name / "package.json").is_file()]
                if missing:
                    errors.append(f"Missing TUI packages: {', '.join(sorted(missing))}")
                result["tui"] = {"node_modules": str(modules_path.resolve()), "node": _run(["node", "--version"], tui)}
    except (OSError, ValueError, StopIteration, subprocess.TimeoutExpired) as exc:
        errors.append(f"Preflight failed: {type(exc).__name__}: {exc}")
    result["ok"] = not errors
    result["launch_scope"] = "Run the selected Python with -m from workspace_root; verify Team stat_file paths separately."
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--python", help="Exact interpreter used for the baseline and worker test commands")
    parser.add_argument("--module", action="append", help="Project module whose imported path must stay inside the worktree")
    parser.add_argument("--require-package", action="append", default=[])
    parser.add_argument("--check-tui", action="store_true")
    args = parser.parse_args(argv)
    args.workspace = args.workspace.expanduser().resolve()
    local_python = args.workspace / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python = args.python or (str(local_python.absolute()) if local_python.exists() else sys.executable)
    result = check_worktree(args.workspace, python=python, modules=args.module or ["agent"],
                            packages=args.require_package, check_tui=args.check_tui)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
