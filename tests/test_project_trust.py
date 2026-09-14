import asyncio
from pathlib import Path

import pytest

from agent.runtime.mcp import MCPManager
from agent.runtime.project_instructions import ProjectInstructions
from agent.runtime.project_trust import ProjectTrust
from agent.runtime.skills import SkillStore
from agent.runtime.tools.registry import ToolRegistry


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    return root


def test_project_instructions_are_default_deny(monkeypatch, tmp_path):
    root = _project(tmp_path)
    (root / "AGENTS.md").write_text("untrusted instruction", encoding="utf-8")
    monkeypatch.delenv("ASTRA_PROJECT_TRUST", raising=False)
    monkeypatch.delenv("ASTRA_TRUSTED_PROJECTS", raising=False)

    assert ProjectInstructions(root).base_prompt == ""
    assert ProjectInstructions(root, trusted=True).base_prompt.endswith("untrusted instruction")


def test_trusted_project_allowlist_uses_resolved_root(monkeypatch, tmp_path):
    root = _project(tmp_path)
    nested = root / "src"
    nested.mkdir()
    monkeypatch.setenv("ASTRA_TRUSTED_PROJECTS", str(root))
    monkeypatch.delenv("ASTRA_PROJECT_TRUST", raising=False)

    decision = ProjectTrust.for_path(nested)

    assert decision.trusted is True
    assert decision.root == root.resolve()


def test_default_project_skill_catalog_is_hidden_when_untrusted(monkeypatch, tmp_path):
    root = _project(tmp_path)
    skills = root / ".astra" / "skills"
    skill_dir = skills / "local-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: local-skill\ndescription: local\n---\nDo something",
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_SKILLS_PATH", str(skills))
    monkeypatch.delenv("ASTRA_PROJECT_TRUST", raising=False)
    monkeypatch.delenv("ASTRA_TRUSTED_PROJECTS", raising=False)

    store = SkillStore()

    assert [item["name"] for item in store.list()] == ["astra-core"]
    assert "local-skill" not in store.catalog_prompt()
    assert "astra-core" in store.catalog_prompt()
    assert "工作原则：" in store.view("astra-core")
    with pytest.raises(PermissionError):
        store.view("local-skill")


def test_default_project_mcp_config_is_not_started_when_untrusted(monkeypatch, tmp_path):
    root = _project(tmp_path)
    config = root / ".astra" / "mcp.json"
    config.parent.mkdir()
    config.write_text('{"servers": {"danger": {"command": "bad"}}}', encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.delenv("AGENT_MCP_CONFIG", raising=False)
    monkeypatch.delenv("ASTRA_PROJECT_TRUST", raising=False)
    monkeypatch.delenv("ASTRA_TRUSTED_PROJECTS", raising=False)
    manager = MCPManager()

    asyncio.run(manager.load(ToolRegistry()))

    assert manager.statuses[0].state == "disabled"
    assert "until the project is trusted" in manager.statuses[0].error
