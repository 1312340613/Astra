"""Exercise Windows interpreter discovery and the installed CMD entrypoint."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent.launcher.installation import discover
from agent.launcher.setup import install_command


pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows CMD bootstrap")
ROOT = Path(__file__).resolve().parents[1]
BASE_PYTHON = Path(getattr(sys, "_base_executable", sys.executable)).resolve()


@pytest.fixture
def bootstrap(tmp_path):
    root = tmp_path / "Astra 中文 & spaces!"
    root.mkdir()
    shutil.copy2(ROOT / "astra.bat", root / "astra.bat")
    (root / "astra.py").write_text(
        "import json, os, sys; "
        "print(json.dumps(dict(python=sys.executable, prefix=sys.prefix, "
        "cwd=os.getcwd(), args=sys.argv[1:]))); sys.exit(23)", encoding="utf-8",
    )
    env = {key: value for key, value in os.environ.items() if key.upper() in {
        "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE",
        "LOCALAPPDATA", "APPDATA", "SYSTEMDRIVE", "HOMEDRIVE", "HOMEPATH",
    }}
    # Do not depend on Python installations, aliases or PATH inherited by CI.
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    env["PYTHONUTF8"] = "1"
    return root, env


def fake_python(folder: Path, name: str, *, supported: bool, launcher: bool = False):
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "interpreter.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\nPath(__file__).with_suffix('.probed').touch()\n"
        + ("assert sys.argv.pop(1) == '-3'\n" if launcher else "")
        + ("sys.version_info = (3, 10, 11, 'final', 0)\n" if not supported else "")
        + "assert sys.argv[1] == '-c'\nexec(sys.argv[2])\n",
        encoding="utf-8",
    )
    (folder / name).write_text(
        f'@echo off\n"{BASE_PYTHON}" "%~dp0interpreter.py" %*\n', encoding="utf-8",
    )


def invoke(command: Path, cwd: Path, env: dict[str, str]):
    line = f'cmd.exe /d /s /c ""{command}" version "two words" "中文" "literal!bang" "a & b""'
    return subprocess.run(line, cwd=cwd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=30, check=False)


def assert_launched(result, cwd):
    assert result.returncode == 23, result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert Path(value["python"]).resolve() == BASE_PYTHON
    assert value["cwd"] == str(cwd)
    assert value["args"] == ["version", "two words", "中文", "literal!bang", "a & b"]
    return value


def test_bootstrap_accepts_newer_py_without_exact_311(bootstrap, tmp_path):
    root, env = bootstrap
    launcher = tmp_path / "py launcher"
    fake_python(launcher, "py.cmd", supported=True, launcher=True)
    env["PATH"] = str(launcher) + os.pathsep + env["PATH"]
    assert_launched(invoke(root / "astra.bat", tmp_path, env), tmp_path)


def test_bootstrap_skips_old_python_before_supported_path_entry(bootstrap, tmp_path):
    root, env = bootstrap
    old, new = tmp_path / "old python", tmp_path / "new python"
    fake_python(old, "python.cmd", supported=False)
    fake_python(new, "python.cmd", supported=True)
    env["PATH"] = os.pathsep.join((str(old), str(new), env["PATH"]))
    assert_launched(invoke(root / "astra.bat", tmp_path, env), tmp_path)
    assert (new / "interpreter.probed").exists()


def test_bootstrap_uses_venv_base_when_global_python_is_old(bootstrap, tmp_path):
    root, env = bootstrap
    old = tmp_path / "old python"
    fake_python(old, "python.cmd", supported=False)
    env["PATH"] = str(old) + os.pathsep + env["PATH"]
    subprocess.run([str(BASE_PYTHON), "-m", "venv", "--without-pip", str(root / ".venv")],
                   capture_output=True, check=True, timeout=30)
    value = assert_launched(invoke(root / "astra.bat", tmp_path, env), tmp_path)
    # Setup/update must not hold the environment they may need to replace.
    assert not Path(value["prefix"]).is_relative_to(root / ".venv")


def test_bootstrap_skips_broken_venv(bootstrap, tmp_path):
    root, env = bootstrap
    broken = root / ".venv/Scripts/python.exe"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"not a Windows executable")
    launcher = tmp_path / "py launcher"
    fake_python(launcher, "py.cmd", supported=True, launcher=True)
    env["PATH"] = str(launcher) + os.pathsep + env["PATH"]
    assert_launched(invoke(root / "astra.bat", tmp_path, env), tmp_path)


@pytest.mark.parametrize("valid", [False, True])
def test_bootstrap_explicit_python_override_is_authoritative(bootstrap, tmp_path, valid):
    root, env = bootstrap
    fallback = tmp_path / "fallback"
    fake_python(fallback, "python.cmd", supported=True)
    env["PATH"] = str(fallback) + os.pathsep + env["PATH"]
    env["PYTHON"] = str(BASE_PYTHON if valid else tmp_path / "missing Python.exe")
    result = invoke(root / "astra.bat", tmp_path, env)
    if valid:
        assert_launched(result, tmp_path)
    else:
        assert result.returncode == 1
        assert "Python 3.11 or newer is required" in result.stderr
        assert "missing Python.exe" in result.stderr
        assert not result.stdout.strip()


def test_bootstrap_reports_no_supported_python(bootstrap, tmp_path):
    root, env = bootstrap
    old = tmp_path / "old python"
    fake_python(old, "python.cmd", supported=False)
    env["PATH"] = str(old) + os.pathsep + env["PATH"]
    result = invoke(root / "astra.bat", tmp_path, env)
    assert result.returncode == 1
    assert "Python 3.11 or newer is required" in result.stderr
    assert not result.stdout.strip()


def test_registered_windows_command_handles_initial_non_utf8_codepage(bootstrap, tmp_path):
    root, env = bootstrap
    (root / "agent").mkdir()
    (root / "pyproject.toml").write_text('[project]\nname="agent-lab-local"\nversion="0.2.0"\n')
    (root / ".git").mkdir()
    command = install_command(discover(root), bin_dir=tmp_path / "bin", modify_path=False)
    before = command.read_bytes()
    assert install_command(discover(root), bin_dir=tmp_path / "bin", modify_path=False).read_bytes() == before
    caller = tmp_path / "caller.cmd"
    caller.write_text('@echo off\nchcp 936 >nul\n"%ASTRA_TEST_COMMAND%" %*\n', encoding="ascii")
    env["ASTRA_TEST_COMMAND"] = str(command)
    env["PYTHON"] = str(BASE_PYTHON)
    assert_launched(invoke(caller, tmp_path, env), tmp_path)
