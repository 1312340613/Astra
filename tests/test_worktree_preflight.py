import json
from pathlib import Path
import subprocess
import sys

from scripts.worktree_preflight import _check_shared_dependencies, check_worktree, main


def repository(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "fixture_project.py").write_text("value = 1\n")
    return path


def test_preflight_verifies_real_import_path_and_missing_packages(tmp_path):
    root = repository(tmp_path / "project")
    result = check_worktree(root, python=sys.executable, modules=["fixture_project"], packages=["pytest"])
    assert result["ok"], result
    assert result["git_root"] == str(root.resolve())
    assert Path(result["python"]["modules"]["fixture_project"][0]) == root / "fixture_project.py"
    missing = check_worktree(root, python=sys.executable, modules=["fixture_project"],
                             packages=["astra-preflight-nonexistent-fixture"])
    assert not missing["ok"]
    assert any("Missing Python package" in error for error in missing["errors"])


def test_preflight_rejects_import_from_another_checkout_and_wrong_root(tmp_path):
    root = repository(tmp_path / "project")
    foreign = check_worktree(root, python=sys.executable, modules=["agent.runtime.worker"], packages=[])
    assert not foreign["ok"]
    assert any("outside the worktree" in error for error in foreign["errors"])
    nested = root / "nested"
    nested.mkdir()
    (nested / "fixture_project.py").write_text("")
    result = check_worktree(nested, python=sys.executable, modules=["fixture_project"], packages=[])
    assert not result["ok"]
    assert any("Git resolves" in error for error in result["errors"])


def test_shared_environment_checks_dependency_requirements_and_lock(tmp_path):
    source, worktree = tmp_path / "source", tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()
    for root in (source, worktree):
        (root / "pyproject.toml").write_text('[project]\ndependencies = ["pytest>=9"]\n')
        (root / "uv.lock").write_text("same")
    errors = []
    _check_shared_dependencies(worktree, source, node=False, errors=errors)
    assert errors == []
    (worktree / "uv.lock").write_text("changed")
    _check_shared_dependencies(worktree, source, node=False, errors=errors)
    assert any("lock differs" in error for error in errors)
    (worktree / "pyproject.toml").write_text('[project]\ndependencies = ["pytest>=10"]\n')
    _check_shared_dependencies(worktree, source, node=False, errors=errors)
    assert any("Shared dependencies differ" in error for error in errors)


def test_preflight_cli_reports_missing_tui_without_creating_dependencies(tmp_path, capsys):
    root = repository(tmp_path / "project")
    tui = root / "ui-tui"
    tui.mkdir()
    (tui / "package.json").write_text(json.dumps({"dependencies": {"tsx": "*"}}))
    code = main(["--workspace", str(root), "--python", sys.executable, "--module", "fixture_project", "--check-tui"])
    result = json.loads(capsys.readouterr().out)
    assert code == 1
    assert any("Missing TUI dependencies" in error for error in result["errors"])
    assert not (root / ".venv").exists()
    assert not (tui / "node_modules").exists()
