from concurrent.futures import ThreadPoolExecutor
import subprocess

import pytest

from agent.runtime.context_index import workspace
from agent.runtime.context_index.models import (
    HandleEntry,
    RecommendationCandidate,
    SourceLocator,
)
from agent.runtime.context_index.workspace import WorkspaceIdentity, resolve_workspace


@pytest.mark.parametrize("kind", ["directory", "relative", "absolute", "worktree"])
def test_workspace_discovers_checkout_without_launching_git(monkeypatch, tmp_path, kind):
    def forbidden_process(*args, **kwargs):
        raise AssertionError("Workspace discovery must not launch Git")

    monkeypatch.setattr(subprocess, "Popen", forbidden_process)
    root = tmp_path / "研究 repo"
    child = root / "nested" / "source"
    child.mkdir(parents=True)
    git_dir = root / ".git" if kind == "directory" else tmp_path / "metadata"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    if kind == "worktree":
        (git_dir / "commondir").write_text("../common\n")
    else:
        (git_dir / "objects").mkdir()
    if kind != "directory":
        target = "../metadata" if kind == "relative" else str(git_dir)
        (root / ".git").write_text(f"gitdir: {target}\n", encoding="utf-8")
    workspace.clear_workspace_cache()

    assert resolve_workspace(child).root == str(root.resolve())


@pytest.mark.parametrize("marker", ["invalid", "gitdir: ", "gitdir: ../missing", "gitdir: bad\x00path"])
def test_workspace_ignores_invalid_git_pointers(tmp_path, marker):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").write_text(marker)
    workspace.clear_workspace_cache()

    assert resolve_workspace(root).root == str(root.resolve())
    assert workspace._git_top_level(root) is None


def test_workspace_prefers_nearest_checkout(tmp_path):
    root = tmp_path / "outer"
    nested = root / "inner"
    for checkout in (root, nested):
        (checkout / ".git" / "objects").mkdir(parents=True)
        (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    assert workspace._git_top_level(nested) == nested


def test_workspace_prefers_git_root(monkeypatch, tmp_path):
    repo = tmp_path / "astra-master"
    child = repo / "agent" / "runtime"
    child.mkdir(parents=True)
    monkeypatch.setattr(workspace, "_git_top_level", lambda _cwd: repo)

    assert resolve_workspace(child) == WorkspaceIdentity(
        key=str(repo.resolve()).casefold(),
        root=str(repo.resolve()),
        label="astra-master",
    )


def test_workspace_resolution_caches_git_probe_and_invalidates_on_git_metadata(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    child = root / "nested"
    git_dir = root / ".git"
    git_dir.mkdir(parents=True)
    head = git_dir / "HEAD"
    head.write_text("ref: refs/heads/main\n")
    child.mkdir()
    calls = []
    monkeypatch.setattr(workspace, "_git_top_level", lambda cwd: calls.append(cwd) or root)
    workspace.clear_workspace_cache()

    assert resolve_workspace(child).root == str(root.resolve())
    assert resolve_workspace(child).root == str(root.resolve())
    assert len(calls) == 1
    # Change size as well as content: rapid equal-length writes can share a
    # timestamp on Windows, leaving the metadata cache key unchanged.
    head.write_text("ref: refs/heads/next-branch\n")

    assert resolve_workspace(child).root == str(root.resolve())
    assert len(calls) == 2


def test_workspace_git_miss_is_not_cached_so_new_repository_is_observed(monkeypatch, tmp_path):
    root = tmp_path / "future-repo"
    child = root / "nested"
    child.mkdir(parents=True)
    workspace.clear_workspace_cache()
    monkeypatch.setattr(
        workspace,
        "_git_top_level",
        lambda cwd: root if (root / ".git").exists() else None,
    )

    assert resolve_workspace(child).root == str(child)
    (root / ".git").mkdir()

    assert resolve_workspace(child).root == str(root)
    assert len(workspace._workspace_cache) == 1


def test_workspace_cache_is_safe_under_concurrent_resolvers(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    child = root / "nested"
    (root / ".git").mkdir(parents=True)
    child.mkdir()
    workspace.clear_workspace_cache()
    monkeypatch.setattr(workspace, "_git_top_level", lambda _cwd: root)

    with ThreadPoolExecutor(max_workers=8) as workers:
        identities = list(workers.map(resolve_workspace, [child] * 32))

    assert identities == [WorkspaceIdentity(str(root).casefold(), str(root), "repo")] * 32
    assert len(workspace._workspace_cache) == 1


def test_workspace_uses_normalized_directory_when_git_is_unavailable(monkeypatch, tmp_path):
    workspace_dir = tmp_path / "Astra Project!"
    workspace_dir.mkdir()
    monkeypatch.setattr(workspace, "_git_top_level", lambda _cwd: None)

    assert resolve_workspace(workspace_dir) == WorkspaceIdentity(
        key=str(workspace_dir.resolve()).casefold(),
        root=str(workspace_dir.resolve()),
        label="Astra-Project-",
    )


def test_workspace_label_keeps_unicode_letters_and_replaces_symbol_runs(
    monkeypatch,
    tmp_path,
):
    workspace_dir = tmp_path / "研究 项目!＆"
    workspace_dir.mkdir()
    monkeypatch.setattr(workspace, "_git_top_level", lambda _cwd: None)

    assert resolve_workspace(workspace_dir).label == "研究-项目-"


def test_context_model_repr_redacts_raw_locator_values():
    locator = SourceLocator(
        kind="session_message",
        primary="session-secret-locator",
        secondary=4821,
    )
    candidate = RecommendationCandidate(
        source="session",
        identity="session-1",
        description="A session recommendation",
        timestamp=1.0,
        workspace_tier=1,
        native_query_rank=1.0,
        topic_key="topic",
        trust_label="historical_context",
        locator=locator,
    )
    entry = HandleEntry(
        request_id="request-1",
        source="session",
        locator=locator,
    )

    for rendered in (repr(candidate), repr(entry)):
        assert "session-secret-locator" not in rendered
        assert "4821" not in rendered
