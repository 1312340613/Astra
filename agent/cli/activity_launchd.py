"""Private, shell-free lifecycle management for the activity sync LaunchAgent."""

from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_LABEL = "com.astra.activity-sync"


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise OSError("LaunchAgent management requires POSIX user identity")
    return int(getuid())


def launch_agent_path(home: Path | None = None) -> Path:
    base = (home or Path.home()).expanduser()
    return base / "Library" / "LaunchAgents" / f"{_LABEL}.plist"


def render_launch_agent(
    *,
    python_executable: Path,
    project_root: Path,
    log_dir: Path,
    event_root: Path | None = None,
) -> bytes:
    environment = {"ASTRA_ACTIVITY_LOG_DIR": str(log_dir)}
    if event_root is not None:
        # M5 source switch: sync imports from Astra's own recorder archive
        # instead of the ChatGPT/Codex Computer History container.
        environment["ASTRA_COMPUTER_HISTORY_ROOT"] = str(event_root)
    payload = {
        "EnvironmentVariables": environment,
        "Label": _LABEL,
        "ProgramArguments": [
            str(python_executable),
            "-m",
            "agent.cli.main",
            "activity",
            "sync",
            "--scheduled",
        ],
        "RunAtLoad": True,
        "StandardErrorPath": "/dev/null",
        "StandardOutPath": "/dev/null",
        "StartInterval": 300,
        "WorkingDirectory": str(project_root),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def _atomic_private_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        fchmod = getattr(os, "fchmod", None)
        if fchmod is None:
            raise OSError("Private LaunchAgent installation requires descriptor permissions")
        fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _run_launchctl(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, capture_output=True, text=True, check=False)


def _missing_job(completed: subprocess.CompletedProcess[str]) -> bool:
    stderr = completed.stderr.casefold()
    return any(marker in stderr for marker in ("could not find service", "no such process", "service not found"))


def install_launch_agent(
    *,
    python_executable: Path,
    project_root: Path,
    home: Path | None = None,
    event_root: Path | None = None,
) -> dict[str, Any]:
    log_dir = project_root / ".astra" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_dir.chmod(0o700)
    plist_path = launch_agent_path(home)
    _atomic_private_write(
        plist_path,
        render_launch_agent(
            python_executable=python_executable,
            project_root=project_root,
            log_dir=log_dir,
            event_root=event_root,
        ),
    )
    try:
        completed = _run_launchctl(["launchctl", "bootstrap", f"gui/{_current_uid()}", str(plist_path)])
    except OSError as exc:
        return {
            "scheduler_status": "error",
            "plist_path": str(plist_path),
            "error_type": type(exc).__name__,
        }
    if completed.returncode != 0:
        return {
            "scheduler_status": "error",
            "plist_path": str(plist_path),
            "error_type": "LaunchctlBootstrapError",
        }
    return {"scheduler_status": "loaded", "plist_path": str(plist_path)}


def uninstall_launch_agent(*, home: Path | None = None) -> dict[str, Any]:
    plist_path = launch_agent_path(home)
    launchctl_error = ""
    try:
        completed = _run_launchctl(["launchctl", "bootout", f"gui/{_current_uid()}/{_LABEL}"])
        if completed.returncode != 0 and not _missing_job(completed):
            launchctl_error = "LaunchctlBootoutError"
    except OSError as exc:
        launchctl_error = type(exc).__name__
    plist_path.unlink(missing_ok=True)
    result: dict[str, Any] = {
        "scheduler_status": "unloaded" if not launchctl_error else "error",
        "plist_path": str(plist_path),
    }
    if launchctl_error:
        result["error_type"] = launchctl_error
    return result


def launch_agent_status() -> dict[str, Any]:
    try:
        completed = _run_launchctl(["launchctl", "print", f"gui/{_current_uid()}/{_LABEL}"])
    except OSError as exc:
        return {"scheduler_status": "unavailable", "error_type": type(exc).__name__}
    if completed.returncode == 0:
        return {"scheduler_status": "loaded"}
    if _missing_job(completed):
        return {"scheduler_status": "unloaded"}
    return {"scheduler_status": "error", "error_type": "LaunchctlPrintError"}
