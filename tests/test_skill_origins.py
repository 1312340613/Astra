import asyncio
import json

import pytest

from agent.cli.skill_commands import execute_skill_command
from agent.runtime.learning import LearningStore
from agent.runtime.skill_curation import SkillCurator
from agent.runtime.skill_learning import LearnedSkills
from agent.runtime.skills import SkillStore
from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.tools.skills import register_skill_tools


def skill(name, text="Check the actual output before reporting success."):
    return f"---\nname: {name}\ndescription: {text}\n---\n\n{text}\n"


def test_user_install_and_create_are_distinct_from_automatic_summaries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = SkillStore(tmp_path / "skills")
    learned = LearnedSkills(store)
    registry = ToolRegistry()
    register_skill_tools(registry, store)
    output, error = execute_skill_command(store, ["create", "manual", "USER_ONLY_TRIGGER"])
    assert not error and "Excluded from /learn review" in output
    assert (store.root / "user/manual/SKILL.md").is_file()
    LearningStore(tmp_path / "learning.db").set_mode("off")
    result = asyncio.run(registry.execute("skill_manage", {"action": "create", "origin": "user",
                         "name": "installed", "content": skill("installed", "USER_INSTALL_TRIGGER")}))
    assert not result["error"]
    assert json.loads(result["output"])["origin"] == "user"
    learned.manage("create", "automatic", content=skill("automatic"))
    origins = {item["name"]: item["origin"] for item in store.list()}
    assert origins == {"astra-core": "builtin", "automatic": "auto", "installed": "user", "manual": "user"}
    assert set(learned._state()["skills"]) == {"automatic"}
    output, _ = execute_skill_command(store, [])
    assert "[auto] [learned] automatic" in output
    assert "[user] [user] manual" in output

    class Reviewer:
        async def chat_limited(self, messages, **kwargs):
            text = json.dumps(messages)
            assert "USER_ONLY_TRIGGER" not in text and "USER_INSTALL_TRIGGER" not in text
            return {"content": json.dumps({"actions": [{"action": "archive", "names": ["automatic"], "reason": "Fixture archive"}]})}

    before = store.view("manual"), store.view("installed")
    asyncio.run(SkillCurator(Reviewer(), learned).review())
    assert (store.view("manual"), store.view("installed")) == before


def test_folder_and_frontmatter_cannot_claim_automatic_ownership(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    learned = LearnedSkills(SkillStore(tmp_path / "skills"))
    learned.skills.create("copied", skill("copied").replace("description:", "origin: auto\ndescription:"), "learned")
    learned.manage("create", "unknown", content=skill("unknown"))
    state = learned._state()
    state["skills"]["unknown"].pop("origin")
    learned._json(learned.home / "index.json", state)
    assert all(item["origin"] == "user" for item in learned.skills.list() if item["name"] != "astra-core")
    assert SkillCurator(None, learned).prepare()["items"] == {}
    for name in ("copied", "unknown"):
        with pytest.raises(ValueError, match="model-owned"):
            learned.owned(name, state)


def test_review_cannot_target_a_user_skill_even_if_model_requests_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    learned = LearnedSkills(SkillStore(tmp_path / "skills"))
    learned.skills.create("manual", skill("manual"), "user")
    learned.manage("create", "automatic", content=skill("automatic"))
    curator = SkillCurator(None, learned)
    before = learned.skills.view("manual")
    with learned.locked(), pytest.raises(ValueError, match="unread"):
        curator.apply(curator.prepare(), {"actions": [{"action": "archive", "names": ["manual"], "reason": "Invalid target"}]})
    assert learned.skills.view("manual") == before


@pytest.mark.parametrize("another_workspace", [False, True])
def test_tool_requires_explicit_origin_and_cannot_change_it(tmp_path, monkeypatch, another_workspace):
    store = SkillStore(tmp_path / "skills")
    registry = ToolRegistry()
    register_skill_tools(registry, store)
    missing = asyncio.run(registry.execute("skill_manage", {"action": "create", "name": "sample", "content": skill("sample")}))
    assert "origin is required" in missing["error"]
    LearnedSkills(store).manage("create", "sample", content=skill("sample"))
    if another_workspace:
        monkeypatch.chdir(tmp_path)
        assert "sample" not in {item["name"] for item in store.list()}
    changed = asyncio.run(registry.execute("skill_manage", {"action": "patch", "origin": "user", "name": "SAMPLE",
                          "old_string": "\nCheck", "new_string": "\nSkip"}))
    assert "origins are fixed" in changed["error"]
    assert store.raw_file("sample", "SKILL.md").count("Check") == 2
