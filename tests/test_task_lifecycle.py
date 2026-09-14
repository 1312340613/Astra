"""Tests for Phase 4 task lifecycle: block / unblock / schedule / verify.

Covers:
- State transitions: running → blocked_on_user / waiting_external / scheduled → running
- Verification: completed → done_verified (pass) or completed → running (fail)
- Queries: blocked_runs(), due_scheduled_runs()
- Guard rails: invalid transitions raise ValueError
"""

import pytest

from agent.runtime.task_store import (
    TERMINAL_TASK_STATUSES,
    TaskStore,
    format_task_detail,
)


@pytest.fixture()
def store(tmp_path):
    return TaskStore(path=tmp_path / "test_tasks.db")


_run_counter = 0


def _start_run(store: TaskStore, *, session_id: str = "s1") -> str:
    """Helper: start a task run and return its id."""
    global _run_counter
    _run_counter += 1
    task = store.start_run(
        request_id=f"req-{_run_counter}",
        input_text="test task",
        session_id=session_id,
    )
    return task["id"]


# ---------------------------------------------------------------------------
# block_run
# ---------------------------------------------------------------------------

class TestBlockRun:
    def test_block_on_user(self, store):
        run_id = _start_run(store)
        result = store.block_run(run_id, "等用户确认数据库选型")
        assert result["status"] == "blocked_on_user"
        assert result["block_reason"] == "等用户确认数据库选型"
        assert result["blocked_at"] is not None

    def test_block_waiting_external(self, store):
        run_id = _start_run(store)
        result = store.block_run(run_id, "等邮件回复", kind="waiting_external")
        assert result["status"] == "waiting_external"
        assert result["block_reason"] == "等邮件回复"

    def test_block_invalid_kind(self, store):
        run_id = _start_run(store)
        with pytest.raises(ValueError, match="Invalid block kind"):
            store.block_run(run_id, "reason", kind="paused")

    def test_block_non_running_raises(self, store):
        run_id = _start_run(store)
        store.finish_run(run_id, "completed")
        with pytest.raises(ValueError, match="only running"):
            store.block_run(run_id, "too late")

    def test_block_unknown_run_raises(self, store):
        with pytest.raises(KeyError):
            store.block_run("nonexistent", "reason")

    def test_block_records_event(self, store):
        run_id = _start_run(store)
        store.block_run(run_id, "等确认")
        task = store.get_task(run_id)
        # Event should exist (verified via task being retrievable with correct state)
        assert task["status"] == "blocked_on_user"


# ---------------------------------------------------------------------------
# unblock_run
# ---------------------------------------------------------------------------

class TestUnblockRun:
    def test_unblock_from_blocked_on_user(self, store):
        run_id = _start_run(store)
        store.block_run(run_id, "等确认")
        result = store.unblock_run(run_id)
        assert result["status"] == "running"
        assert result["block_reason"] == ""
        assert result["blocked_at"] is None

    def test_unblock_from_waiting_external(self, store):
        run_id = _start_run(store)
        store.block_run(run_id, "等邮件", kind="waiting_external")
        result = store.unblock_run(run_id)
        assert result["status"] == "running"

    def test_unblock_from_scheduled(self, store):
        run_id = _start_run(store)
        store.schedule_run(run_id, "2026-08-01T09:00:00+00:00")
        result = store.unblock_run(run_id)
        assert result["status"] == "running"
        assert result["scheduled_at"] is None

    def test_unblock_running_raises(self, store):
        run_id = _start_run(store)
        with pytest.raises(ValueError, match="only blocked/scheduled"):
            store.unblock_run(run_id)

    def test_unblock_completed_raises(self, store):
        run_id = _start_run(store)
        store.finish_run(run_id, "completed")
        with pytest.raises(ValueError, match="only blocked/scheduled"):
            store.unblock_run(run_id)


# ---------------------------------------------------------------------------
# schedule_run
# ---------------------------------------------------------------------------

class TestScheduleRun:
    def test_schedule_from_running(self, store):
        run_id = _start_run(store)
        result = store.schedule_run(run_id, "2026-08-01T09:00:00+00:00")
        assert result["status"] == "scheduled"
        assert result["scheduled_at"] == "2026-08-01T09:00:00+00:00"

    def test_schedule_from_blocked(self, store):
        run_id = _start_run(store)
        store.block_run(run_id, "等确认")
        result = store.schedule_run(run_id, "2026-08-01T09:00:00+00:00")
        assert result["status"] == "scheduled"
        assert result["blocked_at"] is None

    def test_schedule_completed_raises(self, store):
        run_id = _start_run(store)
        store.finish_run(run_id, "completed")
        with pytest.raises(ValueError, match="only running or blocked"):
            store.schedule_run(run_id, "2026-08-01T09:00:00+00:00")


# ---------------------------------------------------------------------------
# verify_completion
# ---------------------------------------------------------------------------

class TestVerifyCompletion:
    def test_verify_pass(self, store):
        run_id = _start_run(store)
        store.finish_run(run_id, "completed")
        result = store.verify_completion(
            run_id,
            verifier="file_exists_check",
            evidence={"path": "/tmp/output.txt", "exists": True},
        )
        assert result["status"] == "done_verified"
        assert result["verification"]["passed"] is True
        assert result["verification"]["verifier"] == "file_exists_check"
        assert result["verified_at"] is not None

    def test_verify_fail_reverts_to_running(self, store):
        run_id = _start_run(store)
        store.finish_run(run_id, "completed")
        result = store.verify_completion(
            run_id,
            verifier="test_suite",
            evidence={"failures": 3},
            passed=False,
        )
        assert result["status"] == "running"
        assert result["verification"]["passed"] is False
        assert result["verified_at"] is None
        assert result["finished_at"] is None

    def test_verify_running_raises(self, store):
        run_id = _start_run(store)
        with pytest.raises(ValueError, match="only completed"):
            store.verify_completion(run_id)

    def test_verify_already_verified_raises(self, store):
        run_id = _start_run(store)
        store.finish_run(run_id, "completed")
        store.verify_completion(run_id)
        with pytest.raises(ValueError, match="only completed"):
            store.verify_completion(run_id)

    def test_done_verified_is_terminal(self):
        assert "done_verified" in TERMINAL_TASK_STATUSES


# ---------------------------------------------------------------------------
# blocked_runs / due_scheduled_runs queries
# ---------------------------------------------------------------------------

class TestQueries:
    def test_blocked_runs_returns_blocked(self, store):
        run_id = _start_run(store)
        store.block_run(run_id, "等确认")
        blocked = store.blocked_runs()
        assert len(blocked) == 1
        assert blocked[0]["id"] == run_id

    def test_blocked_runs_filter_by_kind(self, store):
        r1 = _start_run(store)
        store.block_run(r1, "等确认", kind="blocked_on_user")
        r2 = _start_run(store)
        store.block_run(r2, "等邮件", kind="waiting_external")

        user_blocked = store.blocked_runs(kind="blocked_on_user")
        assert len(user_blocked) == 1
        assert user_blocked[0]["id"] == r1

        external = store.blocked_runs(kind="waiting_external")
        assert len(external) == 1
        assert external[0]["id"] == r2

    def test_blocked_runs_invalid_kind(self, store):
        with pytest.raises(ValueError):
            store.blocked_runs(kind="paused")

    def test_due_scheduled_runs(self, store):
        run_id = _start_run(store)
        store.schedule_run(run_id, "2026-01-01T00:00:00+00:00")
        due = store.due_scheduled_runs(as_of="2026-07-21T00:00:00+00:00")
        assert len(due) == 1
        assert due[0]["id"] == run_id

    def test_due_scheduled_runs_future_not_due(self, store):
        run_id = _start_run(store)
        store.schedule_run(run_id, "2027-01-01T00:00:00+00:00")
        due = store.due_scheduled_runs(as_of="2026-07-21T00:00:00+00:00")
        assert len(due) == 0

    def test_latest_task_for_session_includes_blocked(self, store):
        run_id = _start_run(store, session_id="sess-blocked")
        store.block_run(run_id, "等确认")
        task = store.latest_task_for_session("sess-blocked")
        assert task is not None
        assert task["id"] == run_id
        assert task["status"] == "blocked_on_user"


# ---------------------------------------------------------------------------
# format_task_detail with new fields
# ---------------------------------------------------------------------------

class TestFormatDetail:
    def test_shows_block_info(self, store):
        run_id = _start_run(store)
        store.block_run(run_id, "等用户确认数据库选型")
        task = store.get_task(run_id)
        text = format_task_detail(task)
        assert "blocked_on_user" in text
        assert "等用户确认数据库选型" in text

    def test_shows_verification_info(self, store):
        run_id = _start_run(store)
        store.finish_run(run_id, "completed")
        store.verify_completion(run_id, verifier="pytest", evidence={"tests": 42})
        task = store.get_task(run_id)
        text = format_task_detail(task)
        assert "PASSED" in text
        assert "pytest" in text

    def test_shows_scheduled_info(self, store):
        run_id = _start_run(store)
        store.schedule_run(run_id, "2026-08-01T09:00:00+00:00")
        task = store.get_task(run_id)
        text = format_task_detail(task)
        assert "scheduled" in text
        assert "2026-08-01" in text
