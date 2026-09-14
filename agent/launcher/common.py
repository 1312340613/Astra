"""Small IO primitives shared by setup, diagnostics and the updater."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


class LauncherError(RuntimeError):
    """An actionable installation error, safe to display without a traceback."""


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value
    except (OSError, ValueError) as exc:
        raise LauncherError(f"Cannot read {path}: {exc}") from exc


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def run(
    args: list[str], *, cwd: Path, env: dict[str, str] | None = None,
    timeout: int = 120, capture: bool = True,
) -> str:
    try:
        result = subprocess.run(
            args, cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
            capture_output=capture, stdout=None if capture else sys.stderr, check=False, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LauncherError(f"Could not run {Path(args[0]).name}: {exc}") from exc
    if result.returncode:
        # Do not echo arguments: remote URLs and environment values may contain secrets.
        raise LauncherError(f"{Path(args[0]).name} failed (exit {result.returncode})."
                            + ("\n" + (result.stderr or result.stdout or "")[-4000:] if capture else ""))
    return (result.stdout or "").strip()


def git_environment() -> dict[str, str]:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    # An inherited Git override must never redirect an update to another repository.
    for key in tuple(env):
        if key in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"}:
            env.pop(key)
    return env


def git_bytes(root: Path, *args: str, data: bytes | None = None) -> bytes:
    """Keep NUL-delimited paths and binary content lossless."""
    try:
        result = subprocess.run(["git", "--literal-pathspecs", "-C", str(root), *args],
                                cwd=root, env=git_environment(), input=data, capture_output=True,
                                check=False, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LauncherError(f"Could not run Git: {exc}") from exc
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace")[-4000:]
        raise LauncherError(f"Git failed (exit {result.returncode}).\n{message}")
    return result.stdout


def git(root: Path, *args: str) -> str:
    return run(["git", "-C", str(root), *args], cwd=root, env=git_environment())
