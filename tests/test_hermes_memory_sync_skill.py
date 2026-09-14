from pathlib import Path

from agent.runtime.skills import SkillStore

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / ".astra" / "skills"


def test_hermes_memory_sync_skill_is_discoverable_and_safe():
    store = SkillStore(SKILLS_ROOT)
    listed = {item["name"]: item for item in store.list()}

    assert "hermes-memory-sync" in listed
    assert listed["hermes-memory-sync"]["category"] == "operations"
    description = str(listed["hermes-memory-sync"]["description"])
    assert description.startswith("Use when")
    assert "Hermes" in description
    assert "sync" in description.lower() or "migrat" in description.lower()

    content = store.view("hermes-memory-sync")
    assert "scripts/import_hermes_history.py --dry-run" in content
    assert "SQLite online backup" in content
    assert "scripts/import_hermes_history.py" in content
    assert "PRAGMA integrity_check" in content
    assert "hermes_sync_messages" in content
    assert "messages_fts" in content
    assert "Do not print message content" in content
    assert "Do not use `--full` for routine sync" in content
