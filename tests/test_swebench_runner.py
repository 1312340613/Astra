"""Offline tests for the SWE-bench runner pipeline."""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

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


@pytest.mark.parametrize("failure", ["checkout", "install", "judge"])
def test_dry_run_cli_reports_pipeline_failures(tmp_path, monkeypatch, failure):
    instance = _passing_instance("unused-in-fixture")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([instance]), encoding="utf-8")

    def checkout(*args, **kwargs):
        if failure == "checkout":
            raise RuntimeError("checkout unavailable")
        return tmp_path

    monkeypatch.setattr(swebench, "clone_and_checkout", checkout)
    monkeypatch.setattr(swebench, "install_repo", lambda workspace: "install failed" if failure == "install" else "")
    monkeypatch.setattr(swebench, "apply_test_patch", lambda *args: None)
    monkeypatch.setattr(swebench, "judge_instance", lambda *args: {
        "resolved": False, "returncode": 2, "stdout": "", "stderr": "collection failed",
    })
    reports = tmp_path / "reports"
    monkeypatch.setattr(swebench, "RESULTS_ROOT", reports)

    assert swebench.main([
        "--manifest", str(manifest), "--all", "--dry-run", "--work-root", str(tmp_path / "work"),
    ]) == 1
    report = json.loads(next(reports.glob("*.json")).read_text(encoding="utf-8"))
    assert report[0]["status"] == "error"
    assert report[0]["error"]


@pytest.mark.parametrize("returncode", [0, 1])
def test_dry_run_preserves_executed_baseline_results(tmp_path, monkeypatch, returncode):
    monkeypatch.setattr(swebench, "clone_and_checkout", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(swebench, "apply_test_patch", lambda *args: None)
    monkeypatch.setattr(swebench, "judge_instance", lambda *args: {
        "resolved": returncode == 0, "returncode": returncode, "stdout": "baseline result", "stderr": "",
    })
    result = asyncio.run(swebench.run_instance(
        _passing_instance("fixture"), work_root=tmp_path, model_key="fake", dry_run=True, install=False,
    ))
    assert result["status"] == "dry-run"
    assert result["judge_returncode"] == returncode
    assert result["resolved"] is False


def test_model_round_keeps_judge_hidden_until_after_reply(tmp_path, monkeypatch):
    from agent.evals import coding_agent

    repo = _make_repo(tmp_path)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    instance = _passing_instance(commit)
    replies = []

    async def reply(message):
        assert not (repo / "test_sample.py").exists()
        replies.append(message)

    async def build(workdir, model_key):
        assert workdir == repo
        assert model_key == "fake"
        return SimpleNamespace(reply=reply, context=SimpleNamespace(
            iteration_count=1, total_prompt_tokens=20, total_completion_tokens=10,
            total_cache_hit_tokens=0, total_cache_miss_tokens=20,
        ))

    monkeypatch.setattr(coding_agent, "build_coding_agent", build)
    monkeypatch.setattr(swebench, "clone_and_checkout", lambda *args, **kwargs: repo)
    result = asyncio.run(swebench.run_instance(
        instance, work_root=tmp_path / "work", model_key="fake", dry_run=False, install=False,
    ))
    assert len(replies) == 1
    assert result["status"] == "pass"
    assert result["resolved"] is True
    assert result["prompt_tokens"] == 20
    assert result["completion_tokens"] == 10
