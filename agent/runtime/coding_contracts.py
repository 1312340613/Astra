"""Durable evidence contracts for source-changing coding tasks.

The contract is deliberately data-only: it can be persisted in ``TaskStore``
without making a successful command equivalent to a task completion verdict.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def recommended_checks(workdir: str | Path) -> list[str]:
    """Return a small deterministic, project-local verification shortlist."""
    root = Path(workdir)
    checks: list[str] = []
    if (root / "pyproject.toml").is_file() or (root / "pytest.ini").is_file():
        checks.append("python -m pytest -q")
    if (root / "package.json").is_file():
        checks.append("npm test")
    if (root / "Cargo.toml").is_file():
        checks.append("cargo test")
    if (root / "go.mod").is_file():
        checks.append("go test ./...")
    return checks[:3]


def new_contract(*, paths: set[str], workdir: str | Path) -> dict[str, Any]:
    return {
        "version": 2,
        "required": True,
        "status": "pending",
        "mutated_paths": sorted(paths),
        "recommended_checks": recommended_checks(workdir),
        "checks": [],
    }


def append_check(
    contract: dict[str, Any],
    *,
    tool: str,
    command: str,
    execution: dict[str, Any],
    error: str = "",
    output: str = "",
) -> dict[str, Any]:
    """Record execution, never infer test relevance or correctness from it."""
    result = dict(contract)
    checks = list(result.get("checks") or [])
    checks.append({
        "tool": tool,
        "command": command[:1000],
        "execution_status": execution.get("status", "unknown"),
        "exit_code": execution.get("exit_code"),
        "process_id": execution.get("process_id", ""),
        "error": error[:1000],
        "output": output if len(output) <= 4000 else output[:1900] + "\n…[truncated]\n" + output[-2000:],
    })
    result["checks"] = checks[-20:]
    result["version"] = 2
    result["status"] = "unverified"
    return result
