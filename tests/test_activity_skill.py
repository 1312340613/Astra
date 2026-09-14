from pathlib import Path
import subprocess

from agent.runtime.skills import SkillStore

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / ".astra" / "skills"
SKILL_PATH = SKILLS_ROOT / "operations" / "activity-history" / "SKILL.md"


def test_activity_history_skill_is_discoverable_and_only_exposes_skill_file():
    listed = {item["name"]: item for item in SkillStore(SKILLS_ROOT).list()}
    assert listed["activity-history"]["category"] == "operations"
    assert listed["activity-history"]["files"] == 1
    assert SkillStore(SKILLS_ROOT).view("activity-history").startswith(
        "---\nname: activity-history\n"
    )


def test_activity_history_skill_is_visible_to_git_but_neighbors_remain_ignored():
    visible = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", str(SKILL_PATH)],
        cwd=REPO_ROOT,
        check=False,
    )
    hidden = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet",
         str(SKILL_PATH.parent / "private-note.md")],
        cwd=REPO_ROOT,
        check=False,
    )
    assert visible.returncode == 1
    assert hidden.returncode == 0


def test_activity_history_skill_teaches_modes_fallback_and_trust_boundary():
    content = SkillStore(SKILLS_ROOT).view("activity-history").lower()
    for required in {
        "activity_search",
        "browse",
        "discovery",
        "expand",
        "cache_fallback",
        "stale",
        "untrusted",
        "never",
        "authority",
    }:
        assert required in content
    assert "execute_shell" not in content
    assert "do not run `activity sync`" in content


def test_activity_docs_recommend_on_demand_and_keep_scheduler_optional():
    content = (REPO_ROOT / "docs" / "activity-history.md").read_text(encoding="utf-8").lower()
    assert "on-demand" in content
    assert "cache_fallback" in content
    assert "optional" in content and "launchagent" in content
    assert "uninstall" in content and "preserves" in content
    assert "not installed by default" in content
