from pathlib import Path

import pytest

from agent.runtime.task_store import TaskStore


def test_set_goal_creates_active_goal(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    goal = store.set_goal("s1", "  make   pnpm test pass ", criteria="exit code 0", max_rounds=5)
    assert goal["status"] == "active"
    assert goal["objective"] == "make pnpm test pass"
    assert goal["criteria"] == "exit code 0"
    assert goal["round"] == 0
    assert goal["max_rounds"] == 5
    assert store.active_goal_for_session("s1")["id"] == goal["id"]
    assert store.active_goal_for_session("other") is None


def test_set_goal_replaces_previous_live_goal(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    first = store.set_goal("s1", "objective one")
    second = store.set_goal("s1", "objective two")
    assert second["id"] != first["id"]
    live = store.active_goal_for_session("s1")
    assert live["id"] == second["id"]
    assert store.get_goal(first["id"])["status"] == "cleared"


def test_set_goal_rejects_empty_and_huge_objective(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    with pytest.raises(ValueError):
        store.set_goal("s1", "   ")
    with pytest.raises(ValueError):
        store.set_goal("s1", "x" * 2001)


def test_goal_budget_clamped_and_default_from_env(tmp_path: Path, monkeypatch):
    store = TaskStore(tmp_path / "tasks.db")
    monkeypatch.delenv("GOAL_MAX_ROUNDS", raising=False)
    defaulted = store.set_goal("s1", "objective")
    assert defaulted["max_rounds"] == 20
    monkeypatch.setenv("GOAL_MAX_ROUNDS", "3")
    goal = store.set_goal("s1", "objective")
    assert goal["max_rounds"] == 3
    clamped = store.set_goal("s1", "objective", max_rounds=9999)
    assert clamped["max_rounds"] == 50


def test_pause_resume_clear_lifecycle(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    store.set_goal("s1", "objective")

    paused = store.pause_goal("s1")
    assert paused["status"] == "paused"
    assert store.active_goal_for_session("s1") is not None  # paused is still live
    assert store.pause_goal("s1") is None  # not active anymore

    resumed = store.resume_goal("s1")
    assert resumed["status"] == "active"
    assert store.resume_goal("s1") is None  # not paused anymore

    cleared = store.clear_goal("s1")
    assert cleared["status"] == "cleared"
    assert cleared["finished_at"]
    assert store.active_goal_for_session("s1") is None
    assert store.clear_goal("s1") is None


def test_clear_also_works_from_paused(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    store.set_goal("s1", "objective")
    store.pause_goal("s1")
    cleared = store.clear_goal("s1")
    assert cleared["status"] == "cleared"


def test_record_round_not_met_advances_counter(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    goal = store.set_goal("s1", "objective", max_rounds=3)
    updated = store.record_goal_round(goal["id"], {
        "met": False,
        "evidence": "no test output yet",
        "next_step": "run the test suite",
    })
    assert updated["status"] == "active"
    assert updated["round"] == 1
    assert updated["last_verdict"]["met"] is False
    assert updated["last_verdict"]["next_step"] == "run the test suite"
    assert updated["history"][-1]["round"] == 1


def test_record_round_met_completes_goal(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    goal = store.set_goal("s1", "objective", max_rounds=3)
    store.record_goal_round(goal["id"], {"met": False, "next_step": "step"})
    updated = store.record_goal_round(goal["id"], {"met": True, "evidence": "tests pass"})
    assert updated["status"] == "completed"
    assert updated["round"] == 2
    assert updated["finished_at"]
    assert store.active_goal_for_session("s1") is None


def test_record_round_exhausts_budget(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    goal = store.set_goal("s1", "objective", max_rounds=2)
    store.record_goal_round(goal["id"], {"met": False})
    updated = store.record_goal_round(goal["id"], {"met": False})
    assert updated["status"] == "exhausted"
    assert updated["finished_at"]
    assert store.active_goal_for_session("s1") is None


def test_record_round_rejects_non_active_goal(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    goal = store.set_goal("s1", "objective")
    store.pause_goal("s1")
    with pytest.raises(ValueError):
        store.record_goal_round(goal["id"], {"met": False})
    with pytest.raises(KeyError):
        store.record_goal_round("missing", {"met": False})


def test_history_capped_at_twenty_rounds(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    goal = store.set_goal("s1", "objective", max_rounds=50)
    for index in range(25):
        updated = store.record_goal_round(goal["id"], {"met": False, "next_step": f"step {index}"})
    assert len(updated["history"]) == 20
    assert updated["history"][-1]["next_step"] == "step 24"
    assert updated["round"] == 25


def test_format_goal_status(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    assert "No active session goal" in store.format_goal_status("s1")
    store.set_goal("s1", "make tests pass", criteria="pytest green")
    text = store.format_goal_status("s1")
    assert "make tests pass" in text
    assert "pytest green" in text
    assert "round 0/20" in text
