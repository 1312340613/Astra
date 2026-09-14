"""Offline tests for the SWE-bench runner pipeline."""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

import agent.evals.swebench as swebench


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _run_git(repo, "add", "base.txt")
    _run_git(repo, "commit", "-q", "-m", "base")
    return repo


def _passing_instance(commit: str) -> dict:
    patch = (
        "diff --git a/test_sample.py b/test_sample.py\n"
        "new file mode 100644\n"
        "index 0000000..7c2f7b5\n"
        "--- /dev/null\n"
        "+++ b/test_sample.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_ok():\n"
        "+    assert True\n"
    )
    return {
        "repo": "example/repo",
        "instance_id": "example__repo-1",
        "base_commit": commit,
        "problem_statement": "Make the tests pass.",
        "test_patch": patch,
        "FAIL_TO_PASS": ["test_sample.py::test_ok"],
        "PASS_TO_PASS": [],
    }


def test_load_instances_rejects_incomplete_manifest(tmp_path):
    manifest = tmp_path / "bad.json"
    manifest.write_text(json.dumps([{"instance_id": "x"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        swebench.load_instances(manifest)


def test_apply_test_patch_and_judge_pass(tmp_path):
    repo = _make_repo(tmp_path)
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    instance = _passing_instance(commit)

    swebench.apply_test_patch(instance, repo)
    assert (repo / "test_sample.py").exists()
    judge = swebench.judge_instance(instance, repo)
    assert judge["resolved"] is True
    assert judge["returncode"] == 0


def test_run_instance_dry_run_builds_judge_pipeline(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    instance = _passing_instance(commit)
    monkeypatch.setattr(
        swebench,
        "clone_and_checkout",
        lambda instance, work_root, force=False: repo,
    )

    result = asyncio.run(swebench.run_instance(
        instance,
        work_root=tmp_path / "work",
        model_key="fake",
        dry_run=True,
        install=False,
        force=False,
    ))

    assert result["status"] == "dry-run"
    assert result["judge_returncode"] == 0
