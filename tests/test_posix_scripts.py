from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
POSIX_SCRIPTS = (
    PROJECT_ROOT / "astra.sh",
    PROJECT_ROOT / "start-ink.sh",
    PROJECT_ROOT / "astra-migrate.sh",
    PROJECT_ROOT / "checkmail.sh",
    PROJECT_ROOT / "scripts" / "phase_t_gate.sh",
    PROJECT_ROOT / "scripts" / "build-sandbox-image.sh",
)
requires_posix_shell = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="executes POSIX launchers with POSIX interpreter paths and executable permissions",
)


def test_posix_entry_points_exist() -> None:
    for script in POSIX_SCRIPTS:
        assert script.is_file(), f"missing POSIX entry point: {script.relative_to(PROJECT_ROOT)}"


@requires_posix_shell
def test_posix_entry_points_are_executable() -> None:
    for script in POSIX_SCRIPTS:
        assert script.stat().st_mode & stat.S_IXUSR, f"not executable: {script.relative_to(PROJECT_ROOT)}"


def test_posix_entry_points_have_bash_shebangs_and_lf_endings() -> None:
    for script in POSIX_SCRIPTS:
        payload = script.read_bytes()
        assert payload.startswith(b"#!/usr/bin/env bash\n")
        assert b"\r\n" not in payload


@requires_posix_shell
def test_posix_entry_points_are_valid_bash() -> None:
    for script in POSIX_SCRIPTS:
        completed = subprocess.run(
            ["bash", "-n", str(script)],
            cwd=Path("/"),
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr


def test_posix_entry_points_resolve_paths_from_their_own_location() -> None:
    root_wrappers = POSIX_SCRIPTS[:4]
    maintenance_wrappers = POSIX_SCRIPTS[4:]
    for script in root_wrappers:
        content = script.read_text(encoding="utf-8")
        assert "BASH_SOURCE[0]" in content
        assert 'cd -- "$SCRIPT_DIR"' in content or 'ROOT="$SCRIPT_DIR"' in content
    for script in maintenance_wrappers:
        content = script.read_text(encoding="utf-8")
        assert "BASH_SOURCE[0]" in content
        assert 'ROOT="$(cd -- "$SCRIPT_DIR/.."' in content


@requires_posix_shell
def test_astra_rejects_python_older_than_311(tmp_path: Path) -> None:
    fake_python = tmp_path / "python-old-99"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ ${1-} == -c ]]; then exit 1; fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = os.environ.copy()
    env["PYTHON"] = str(fake_python)

    completed = subprocess.run(
        ["bash", str(PROJECT_ROOT / "astra.sh"), "--setup-only"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "Python 3.11" in completed.stderr
    assert str(fake_python) in completed.stderr


@pytest.mark.parametrize("entry_point", ["astra.sh", "start-ink.sh"])
@pytest.mark.parametrize("tui_args", [(), ("alpha", "two words")])
@requires_posix_shell
def test_tui_entry_points_handoff_to_shared_router_with_exact_arguments(
    tmp_path: Path, entry_point: str, tui_args: tuple[str, ...],
) -> None:
    import json
    root = tmp_path / "Astra 中文 checkout"
    root.mkdir()
    for name in ("astra.sh", "start-ink.sh"):
        shutil.copy2(PROJECT_ROOT / name, root / name)
    (root / "astra.py").write_text(
        'import json,os,sys\nprint(json.dumps({"args":sys.argv[1:],"cwd":os.getcwd()}))\nsys.exit(17)\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(root / entry_point), *tui_args], cwd=tmp_path,
        env={**os.environ, "PYTHON": sys.executable}, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 17
    assert json.loads(result.stdout) == {"args": list(tui_args), "cwd": str(tmp_path)}


def test_wrappers_target_repository_owned_programs() -> None:
    windows_wrapper = (PROJECT_ROOT / "checkmail.bat").read_text(encoding="utf-8")
    assert '"%~dp0scripts\\check_163.py" %*' in windows_wrapper
    assert ".astra" not in windows_wrapper
    assert 'scripts/check_163.py" "$@"' in (PROJECT_ROOT / "checkmail.sh").read_text(encoding="utf-8")
    assert 'scripts/migrate_legacy_astra_state.py" "$@"' in (
        PROJECT_ROOT / "astra-migrate.sh"
    ).read_text(encoding="utf-8")
    assert 'scripts/phase_t_gate.py" "$@"' in (
        PROJECT_ROOT / "scripts" / "phase_t_gate.sh"
    ).read_text(encoding="utf-8")


def test_astra_setup_contract_and_final_handoff() -> None:
    content = (PROJECT_ROOT / "astra.sh").read_text(encoding="utf-8")
    assert '"$ROOT/astra.py" "$@"' in content
    assert "python3.11" in content
    for script in POSIX_SCRIPTS:
        assert "rm " not in script.read_text(encoding="utf-8")


def test_shell_line_endings_are_declared() -> None:
    attributes = (PROJECT_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in attributes.splitlines()


def test_windows_wrappers_delegate_without_changing_workspace_or_pausing() -> None:
    for name in ("astra.bat", "start-ink.bat", "astra-path.bat"):
        content = (PROJECT_ROOT / name).read_text(encoding="utf-8").lower()
        assert "cd /d" not in content
        assert "pause" not in content
    content = (PROJECT_ROOT / "astra.bat").read_text(encoding="utf-8")
    assert '"%~dp0astra.py" %*' in content
    assert "exit /b" in content
    assert "sys._base_executable" in content
