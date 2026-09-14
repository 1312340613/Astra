from pathlib import Path

from agent.runtime.prompts import AGENT_CORE_PROMPT
from agent.runtime.task_store import TaskStore
from agent.runtime.tools.goals import register_goal_tools
from agent.runtime.tools.registry import ToolRegistry


def _setup(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    registry = ToolRegistry()
    events: list[dict] = []
    register_goal_tools(
        registry,
        store,
        session_id=lambda: "s1",
        on_goal_event=events.append,
    )
    return store, registry, events


def test_goal_tool_set_show_pause_resume_clear(tmp_path: Path):
    _store, registry, events = _setup(tmp_path)
    tool = registry.get("goal")
    assert tool is not None

    set_result = tool.fn(action="set", objective="make pytest pass", criteria="exit code 0")
    assert "Goal set" in set_result
    assert events and events[-1]["status"] == "active"

    show_result = tool.fn(action="show")
    assert "make pytest pass" in show_result

    pause_result = tool.fn(action="pause")
    assert "paused" in pause_result
    assert events[-1]["status"] == "paused"

    resume_result = tool.fn(action="resume")
    assert "resumed" in resume_result

    clear_result = tool.fn(action="clear")
    assert "cleared" in clear_result
    assert "No live goal to clear." in tool.fn(action="clear")


def test_goal_tool_set_validates_objective(tmp_path: Path):
    _, registry, _ = _setup(tmp_path)
    tool = registry.get("goal")
    result = tool.fn(action="set", objective="   ")
    assert "Could not set goal" in result


def test_goal_tool_complete_is_a_verified_claim_not_a_state_bypass(tmp_path: Path):
    store, registry, events = _setup(tmp_path)
    tool = registry.get("goal")
    goal = store.set_goal("s1", "mua")

    result = tool.fn(action="complete", evidence="Delivered a matching affectionate reply")

    assert "Completion claimed" in result
    assert "independent verifier" in result
    persisted = store.get_goal(goal["id"])
    assert persisted is not None
    assert persisted["status"] == "active"
    assert persisted["round"] == 0
    assert events == []


def test_goal_tool_complete_requires_active_goal(tmp_path: Path):
    _, registry, _ = _setup(tmp_path)
    tool = registry.get("goal")
    assert "No active goal" in tool.fn(action="complete", evidence="done")


def test_goal_tool_handles_missing_store():
    registry = ToolRegistry()
    register_goal_tools(registry, None, session_id=lambda: "s1")
    tool = registry.get("goal")
    assert "unavailable" in tool.fn(action="show")


def test_goal_tool_description_and_core_policy_require_safe_replacement_guidance(tmp_path: Path):
    _, registry, _ = _setup(tmp_path)
    tool = registry.get("goal")
    assert tool is not None
    description = tool.description.lower()
    assert all(action in description for action in ("show", "set", "resume"))
    assert "question answer can replace" not in description
    assert "设置 Goal 前先用 show 检查现有目标" in AGENT_CORE_PROMPT
    assert "替换无关目标前必须询问用户" in AGENT_CORE_PROMPT
    assert "若原始请求已要求实施且工作并非简单单步" in AGENT_CORE_PROMPT
    assert "问题答案不授予高风险工具权限" in AGENT_CORE_PROMPT


def test_goal_tool_unknown_action(tmp_path: Path):
    _, registry, _ = _setup(tmp_path)
    tool = registry.get("goal")
    assert "Unknown goal action" in tool.fn(action="explode")


def test_goal_tool_history_reports_no_rounds_yet(tmp_path: Path):
    store, registry, _ = _setup(tmp_path)
    tool = registry.get("goal")
    store.set_goal("s1", "make pytest pass")
    result = tool.fn(action="history")
    assert "no verification rounds yet" in result


def test_goal_tool_history_lists_verification_rounds(tmp_path: Path):
    store, registry, _ = _setup(tmp_path)
    tool = registry.get("goal")
    goal = store.set_goal("s1", "make pytest pass")
    store.record_goal_round(goal["id"], {"met": False, "evidence": "tests failed", "next_step": "fix the bug"})
    store.record_goal_round(goal["id"], {"met": True, "evidence": "all green", "next_step": ""})

    result = tool.fn(action="history")

    # The second round completed the goal; history must still be visible.
    assert "Goal [completed] history" in result
    assert "2 verification round(s)" in result
    assert "make pytest pass" in result
    assert "round 1: not met" in result
    assert "tests failed" in result
    assert "next: fix the bug" in result
    assert "round 2: MET" in result
    assert "all green" in result


def test_goal_tool_history_without_goal(tmp_path: Path):
    _, registry, _ = _setup(tmp_path)
    tool = registry.get("goal")
    assert "No live goal" in tool.fn(action="history")
