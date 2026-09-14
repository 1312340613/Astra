import json
from pathlib import Path

from agent.runtime.bounded_artifacts import append_bounded_text
from agent.runtime.hook_config import install_project_hooks
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def test_bounded_append_rotates_jsonl_without_splitting_utf8(tmp_path):
    path = tmp_path / "events.jsonl"
    for index in range(12):
        append_bounded_text(
            path,
            json.dumps({"index": index, "text": "中文" * 8}, ensure_ascii=False) + "\n",
            max_bytes=150,
            backup_count=2,
        )

    assert path.stat().st_size <= 150
    assert Path(f"{path}.1").is_file()
    assert not Path(f"{path}.3").exists()
    for candidate in (path, Path(f"{path}.1"), Path(f"{path}.2")):
        if candidate.exists():
            candidate.read_text(encoding="utf-8")


def test_project_hook_audit_is_bounded(tmp_path, monkeypatch):
    astra = tmp_path / ".astra"
    astra.mkdir()
    (astra / "hooks.json").write_text(
        json.dumps({"rules": [{"event": "before_tool", "tool": "demo", "action": "audit"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("ASTRA_HOOK_AUDIT_MAX_BYTES", "180")
    monkeypatch.setenv("ASTRA_HOOK_AUDIT_BACKUPS", "1")
    registry = ToolRegistry()
    install_project_hooks(registry, tmp_path)
    tool = ToolDef(name="demo", description="demo", parameters={}, fn=lambda: "ok")

    for _ in range(20):
        registry.hooks.dispatch_before_tool("demo", {}, tool)

    audit = astra / "hook-events.jsonl"
    assert audit.stat().st_size <= 180
    assert Path(f"{audit}.1").is_file()


def test_oversized_record_is_dropped_instead_of_corrupting_jsonl(tmp_path):
    path = tmp_path / "events.jsonl"
    append_bounded_text(path, '{"ok": true}\n', max_bytes=32)
    append_bounded_text(path, "x" * 100, max_bytes=32)
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
