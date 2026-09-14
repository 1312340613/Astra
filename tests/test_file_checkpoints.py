import asyncio
import json
import shutil

import pytest

from agent.runtime.file_checkpoints import FileCheckpointStore
from agent.runtime.tools.files import FilesystemPolicy, FilesystemRoot, register_file_tools
from agent.runtime.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


def test_checkpoint_restore_reverts_agent_edit(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("before", encoding="utf-8")
    store = FileCheckpointStore(tmp_path)

    pending = store.capture([target], operation="test")
    target.write_text("after", encoding="utf-8")
    checkpoint_id = store.finalize(pending)

    result = store.restore(checkpoint_id)
    assert result["restored"] == ["note.txt"]
    assert target.read_text(encoding="utf-8") == "before"


def test_checkpoint_restore_refuses_later_user_change(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("before", encoding="utf-8")
    store = FileCheckpointStore(tmp_path)
    pending = store.capture([target], operation="test")
    target.write_text("agent", encoding="utf-8")
    checkpoint_id = store.finalize(pending)
    target.write_text("user", encoding="utf-8")

    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        store.restore(checkpoint_id)
    assert target.read_text(encoding="utf-8") == "user"


def test_file_tools_create_and_restore_checkpoint(tmp_path):
    registry = ToolRegistry()
    register_file_tools(registry, workdir=str(tmp_path))
    registry.yolo = True
    target = tmp_path / "created.txt"

    written = run(registry.execute("write_file", {"path": "created.txt", "content": "hello"}))
    payload = json.loads(written["output"])
    checkpoint_id = payload["checkpoint_id"]
    assert checkpoint_id
    assert target.read_text(encoding="utf-8") == "hello"

    listed = run(registry.execute("checkpoint_list", {"limit": 5}))
    assert checkpoint_id in listed["output"]
    restored = run(registry.execute("checkpoint_restore", {"checkpoint_id": checkpoint_id}))
    assert json.loads(restored["output"])["status"] == "restored"
    assert not target.exists()


def test_checkpoint_failure_does_not_turn_successful_write_into_failure(tmp_path, monkeypatch):
    registry = ToolRegistry()
    register_file_tools(registry, workdir=str(tmp_path))
    registry.yolo = True

    def fail_finalize(_pending):
        raise OSError("checkpoint disk unavailable")

    monkeypatch.setattr(FileCheckpointStore, "finalize", fail_finalize)
    result = run(registry.execute("write_file", {"path": "ok.txt", "content": "saved"}))
    assert result["error"] == ""
    assert (tmp_path / "ok.txt").read_text(encoding="utf-8") == "saved"
    assert json.loads(result["output"])["checkpoint_id"] == ""



def test_checkpoint_skips_outside_paths_without_raising(tmp_path):
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside"
    outside_dir.mkdir(exist_ok=True)
    outside = outside_dir / "outside.txt"
    try:
        outside.write_text("outside", encoding="utf-8")
        store = FileCheckpointStore(tmp_path)
        pending = store.capture([outside], operation="approved write")
        assert pending is not None
        assert pending.files == []
        assert pending.skipped_paths == [str(outside)]
        assert store.finalize(pending) == ""
    finally:
        shutil.rmtree(outside_dir, ignore_errors=True)


def test_checkpoint_mixed_paths_captures_only_workspace_files(tmp_path):
    inside = tmp_path / "inside.txt"
    inside.write_text("before", encoding="utf-8")
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside"
    outside_dir.mkdir(exist_ok=True)
    outside = outside_dir / "outside.txt"
    try:
        outside.write_text("outside", encoding="utf-8")
        store = FileCheckpointStore(tmp_path)
        pending = store.capture([inside, outside], operation="mixed write")
        assert pending is not None
        assert [captured.relative for captured in pending.files] == ["inside.txt"]
        assert pending.skipped_paths == [str(outside)]

        inside.write_text("after", encoding="utf-8")
        checkpoint_id = store.finalize(pending)
        assert checkpoint_id
        store.restore(checkpoint_id)
        assert inside.read_text(encoding="utf-8") == "before"
    finally:
        shutil.rmtree(outside_dir, ignore_errors=True)


def test_outside_write_does_not_fail_checkpoint_and_still_succeeds(tmp_path):
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside"
    outside_dir.mkdir(exist_ok=True)
    outside = outside_dir / "outside.txt"

    registry = ToolRegistry()
    policy = FilesystemPolicy(tmp_path, roots=[FilesystemRoot(outside_dir, "rw")])
    register_file_tools(registry, workdir=str(tmp_path), policy=policy)
    registry.yolo = True

    try:
        result = run(registry.execute(
            "write_file",
            {"path": str(outside), "content": "approved outside write"},
        ))
        assert result["error"] == ""
        assert outside.read_text(encoding="utf-8") == "approved outside write"
        assert json.loads(result["output"])["checkpoint_id"] == ""
    finally:
        shutil.rmtree(outside_dir, ignore_errors=True)
