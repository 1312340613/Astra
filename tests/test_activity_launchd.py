from __future__ import annotations

import json
import logging
import os
import plistlib
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.cli import activity_commands, activity_launchd
from agent.cli.activity_launchd import (
    install_launch_agent,
    launch_agent_path,
    launch_agent_status,
    render_launch_agent,
    uninstall_launch_agent,
)
from agent.runtime.activity_sync import SyncReport

requires_posix_permissions = pytest.mark.skipif(os.name == "nt", reason="verifies private POSIX file modes")


def test_rendered_launch_agent_is_short_lived_and_shell_free(tmp_path: Path):
    payload = plistlib.loads(
        render_launch_agent(
            python_executable=Path("/opt/astra/.venv/bin/python"),
            project_root=Path("/opt/astra"),
            log_dir=tmp_path / "logs",
        )
    )
    assert payload["Label"] == "com.astra.activity-sync"
    assert payload["RunAtLoad"] is True
    assert payload["StartInterval"] == 300
    assert payload["ProgramArguments"] == [
        str(Path("/opt/astra/.venv/bin/python")),
        "-m",
        "agent.cli.main",
        "activity",
        "sync",
        "--scheduled",
    ]
    assert payload["WorkingDirectory"] == str(Path("/opt/astra"))
    assert payload.get("Program") != "/bin/sh"
    assert payload["StandardOutPath"] == "/dev/null"
    assert payload["StandardErrorPath"] == "/dev/null"
    assert payload["EnvironmentVariables"]["ASTRA_ACTIVITY_LOG_DIR"] == str(tmp_path / "logs")


@requires_posix_permissions
def test_install_writes_atomic_private_plist_and_bootstraps_exact_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[list[str], dict[str, object]]] = []
    replacements: list[tuple[Path, Path, int]] = []
    real_replace = os.replace

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def observed_replace(source, destination):
        source_path = Path(source)
        replacements.append((source_path, Path(destination), stat.S_IMODE(source_path.stat().st_mode)))
        real_replace(source, destination)

    monkeypatch.setattr(activity_launchd.subprocess, "run", fake_run)
    monkeypatch.setattr(activity_launchd.os, "replace", observed_replace)
    monkeypatch.setattr(activity_launchd.os, "getuid", lambda: 502)

    result = install_launch_agent(
        python_executable=Path("/opt/astra/.venv/bin/python"),
        project_root=tmp_path / "project",
        home=tmp_path / "home",
    )

    plist_path = launch_agent_path(tmp_path / "home")
    assert result["scheduler_status"] == "loaded"
    assert plist_path.exists()
    installed_payload = plistlib.loads(plist_path.read_bytes())
    assert installed_payload["ProgramArguments"][0] == "/opt/astra/.venv/bin/python"
    assert stat.S_IMODE(plist_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "project" / ".astra" / "logs").stat().st_mode) == 0o700
    assert len(replacements) == 1
    assert replacements[0][0].parent == plist_path.parent
    assert replacements[0][1] == plist_path
    assert replacements[0][2] == 0o600
    assert calls == [
        (
            ["launchctl", "bootstrap", "gui/502", str(plist_path)],
            {"capture_output": True, "text": True, "check": False},
        )
    ]


def test_event_root_switches_sync_source_only_when_pinned(tmp_path: Path):
    common = {
        "python_executable": Path("/opt/astra/.venv/bin/python"),
        "project_root": Path("/opt/astra"),
        "log_dir": tmp_path / "logs",
    }
    default_payload = plistlib.loads(render_launch_agent(**common))
    assert "ASTRA_COMPUTER_HISTORY_ROOT" not in default_payload["EnvironmentVariables"]

    pinned = plistlib.loads(
        render_launch_agent(**common, event_root=Path("/opt/astra-native/activity"))
    )
    assert pinned["EnvironmentVariables"]["ASTRA_COMPUTER_HISTORY_ROOT"] == str(Path("/opt/astra-native/activity"))


def test_install_command_preserves_absolute_invoked_virtualenv_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    real_python = tmp_path / "python3.11"
    real_python.write_bytes(b"")
    invoked_python = tmp_path / "project" / ".venv" / "bin" / "python"
    invoked_python.parent.mkdir(parents=True)
    try:
        invoked_python.symlink_to(real_python)
    except OSError as exc:
        pytest.skip(f"host cannot create virtualenv interpreter symlinks: {type(exc).__name__}")
    captured_python: list[Path] = []

    def fake_install(*, python_executable: Path, project_root: Path, event_root: Path | None = None):
        captured_python.append(python_executable)
        rendered = plistlib.loads(
            render_launch_agent(
                python_executable=python_executable,
                project_root=project_root,
                log_dir=project_root / ".astra" / "logs",
            )
        )
        assert rendered["ProgramArguments"][0] == str(invoked_python)
        return {"scheduler_status": "loaded", "plist_path": "/tmp/test.plist"}

    monkeypatch.setattr(activity_commands.sys, "executable", str(invoked_python))
    monkeypatch.setattr(activity_commands, "install_launch_agent", fake_install)

    assert activity_commands.execute_activity_command(["install"]) == 0
    assert captured_python == [invoked_python]
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_uninstall_tolerates_missing_job_and_preserves_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    plist_path = launch_agent_path(home)
    plist_path.parent.mkdir(parents=True)
    plist_path.write_bytes(b"fixture plist")
    database = home / ".astra" / "activity-history.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"permanent archive")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=113, stdout="", stderr="Could not find service")

    monkeypatch.setattr(activity_launchd.subprocess, "run", fake_run)
    monkeypatch.setattr(activity_launchd.os, "getuid", lambda: 502, raising=False)

    result = uninstall_launch_agent(home=home)

    assert result["scheduler_status"] == "unloaded"
    assert not plist_path.exists()
    assert database.read_bytes() == b"permanent archive"
    assert calls == [
        (
            ["launchctl", "bootout", "gui/502/com.astra.activity-sync"],
            {"capture_output": True, "text": True, "check": False},
        )
    ]


def test_uninstall_reports_non_missing_launchctl_failure_without_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    secret = "FIXTURE PRIVATE LAUNCHCTL ERROR"

    def fake_run(_argv, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr=secret)

    monkeypatch.setattr(activity_launchd.subprocess, "run", fake_run)
    monkeypatch.setattr(activity_launchd, "_current_uid", lambda: 502)
    result = uninstall_launch_agent(home=tmp_path)
    assert result["scheduler_status"] == "error"
    assert result["error_type"] == "LaunchctlBootoutError"
    assert secret not in repr(result)


@pytest.mark.parametrize(
    ("returncode", "stderr", "expected"),
    [(0, "", "loaded"), (113, "Could not find service", "unloaded")],
)
def test_launch_agent_status_uses_exact_print_argv_and_bounded_result(
    returncode: int, stderr: str, expected: str, monkeypatch: pytest.MonkeyPatch
):
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=returncode, stdout="FIXTURE SERVICE CONTENT", stderr=stderr)

    monkeypatch.setattr(activity_launchd.subprocess, "run", fake_run)
    monkeypatch.setattr(activity_launchd.os, "getuid", lambda: 502, raising=False)

    assert launch_agent_status() == {"scheduler_status": expected}
    assert calls == [
        (
            ["launchctl", "print", "gui/502/com.astra.activity-sync"],
            {"capture_output": True, "text": True, "check": False},
        )
    ]


def test_scheduled_log_rotates_metadata_only_and_omits_report_error_content(tmp_path: Path):
    secret = "PrivateWindowTitle"
    logger = activity_commands.scheduled_activity_logger(tmp_path)
    report = SyncReport(
        status="partial",
        events_imported=23,
        events_duplicate=2,
        summaries_imported=3,
        summaries_ignored=1,
        malformed_lines=4,
        deferred_lines=1,
        source_active=True,
        error=secret,
    )
    try:
        for _ in range(25_000):
            activity_commands.log_scheduled_report(logger, report)
    finally:
        for handler in logger.handlers[:]:
            handler.flush()
            handler.close()
            logger.removeHandler(handler)

    files = {path.name for path in tmp_path.iterdir()}
    assert files == {
        "activity-sync.log",
        "activity-sync.log.1",
        "activity-sync.log.2",
        "activity-sync.log.3",
    }
    assert all(path.stat().st_size <= 1_000_000 for path in tmp_path.iterdir())
    assert all(secret not in path.read_text(encoding="utf-8") for path in tmp_path.iterdir())
    assert all("window_title" not in path.read_text(encoding="utf-8") for path in tmp_path.iterdir())
    if os.name == "posix":
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in tmp_path.iterdir())


def test_scheduled_logger_uses_required_rotation_limits(tmp_path: Path):
    logger = activity_commands.scheduled_activity_logger(tmp_path)
    try:
        [handler] = logger.handlers
        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        assert handler.maxBytes == 1_000_000
        assert handler.backupCount == 3
    finally:
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)


@requires_posix_permissions
def test_scheduled_logger_hardens_only_retained_log_files(tmp_path: Path):
    retained = [tmp_path / "activity-sync.log", *(tmp_path / f"activity-sync.log.{index}" for index in range(1, 4))]
    untouched = [tmp_path / "activity-sync.log.4", tmp_path / "unrelated.log"]
    for path in [*retained, *untouched]:
        path.write_text("seed", encoding="utf-8")
        path.chmod(0o644)

    logger = activity_commands.scheduled_activity_logger(tmp_path)
    try:
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in retained)
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o644 for path in untouched)
    finally:
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)
