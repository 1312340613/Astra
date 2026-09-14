"""Run wrappers against harmless fixtures, never mail or companion services."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHON_WRAPPERS = ["scripts/astra-migrate", "scripts/checkmail"]
SERVICE_WRAPPERS = ["scripts/activity-control.bat", "scripts/embedding-control.bat"]
PYTHON_PROGRAMS = {
    "astra-migrate": "migrate_legacy_astra_state.py",
    "checkmail": "check_163.py",
}
SERVICE_PROGRAMS = {
    "activity-control.bat": "windows-activity.ps1",
    "embedding-control.bat": "start-embedding-windows.ps1",
}
ARGS = ["two words", "中文", "literal!bang", "a & b", "100%literal"]


def _installation(tmp_path: Path, wrapper: str) -> tuple[Path, Path]:
    root = tmp_path / "Astra 中文 & spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    canonical = Path("scripts") / Path(wrapper).name
    shutil.copy2(ROOT / canonical, root / canonical)
    if Path(wrapper) != canonical:
        shutil.copy2(ROOT / wrapper, root / wrapper)
    return root, scripts


def _python_fixture(scripts: Path, program: str) -> None:
    (scripts / program).write_text(
        'import json,os,sys\n'
        'print(json.dumps({"args":sys.argv[1:],"cwd":os.getcwd()}))\n'
        'sys.exit(37)\n', encoding="utf-8",
    )


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="POSIX shell acceptance")
@pytest.mark.parametrize("wrapper", PYTHON_WRAPPERS)
@pytest.mark.parametrize("python_override", [False, True])
def test_posix_helpers_forward_arguments_and_failure_from_another_directory(
    tmp_path: Path, wrapper: str, python_override: bool,
) -> None:
    wrapper += ".sh"
    root, scripts = _installation(tmp_path, wrapper)
    _python_fixture(scripts, PYTHON_PROGRAMS[Path(wrapper).stem])
    env = os.environ.copy()
    env.pop("PYTHON", None)
    if python_override:
        env["PYTHON"] = sys.executable
    else:
        interpreter = root / ".venv/bin/python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
    result = subprocess.run(
        ["bash", str(root / wrapper), *ARGS], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 37, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"args": ARGS, "cwd": str(root.resolve())}


def _cmd(script: Path, arguments: list[str]) -> str:
    # CMD's /s /c outer quotes enclose a quoted executable plus quoted arguments.
    return 'cmd.exe /d /v:off /s /c "' + " ".join(f'"{value}"' for value in [str(script), *arguments]) + '"'


@pytest.mark.skipif(os.name != "nt", reason="native Windows CMD acceptance")
@pytest.mark.parametrize("wrapper", PYTHON_WRAPPERS)
def test_windows_helpers_forward_arguments_and_failure_from_another_directory(
    tmp_path: Path, wrapper: str,
) -> None:
    wrapper += ".bat"
    root, scripts = _installation(tmp_path, wrapper)
    _python_fixture(scripts, PYTHON_PROGRAMS[Path(wrapper).stem])
    env = {
        **os.environ,
        "PYTHON": str(Path(getattr(sys, "_base_executable", sys.executable)).resolve()),
        "PYTHONUTF8": "1",
    }
    result = subprocess.run(
        _cmd(root / wrapper, ARGS), cwd=tmp_path, env=env,
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=20,
    )
    assert result.returncode == 37, result.stdout + result.stderr
    expected_cwd = root if Path(wrapper).stem == "astra-migrate" else tmp_path
    assert json.loads(result.stdout) == {"args": ARGS, "cwd": str(expected_cwd)}


@pytest.mark.skipif(os.name != "nt", reason="native Windows PowerShell acceptance")
@pytest.mark.parametrize("wrapper", SERVICE_WRAPPERS)
def test_windows_service_helpers_use_sibling_script_and_return_failure(
    tmp_path: Path, wrapper: str,
) -> None:
    root, scripts = _installation(tmp_path, wrapper)
    program = SERVICE_PROGRAMS[Path(wrapper).name]
    (scripts / program).write_text(
        'param([string]$Action)\n'
        '[Console]::OutputEncoding = [Text.UTF8Encoding]::new()\n'
        '@{action=$Action; root=$PSScriptRoot; cwd=$PWD.Path} | ConvertTo-Json -Compress\n'
        'exit 29\n', encoding="utf-8",
    )
    result = subprocess.run(
        _cmd(root / wrapper, ["status"]), cwd=tmp_path,
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=20,
    )
    assert result.returncode == 29, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"action": "status", "root": str(scripts), "cwd": str(tmp_path)}
