"""Task resume and lifecycle evaluation suite.

Tests TaskRun → Step/Event lifecycle, blocked/waiting states,
verify_completion, and interrupted recovery.

Run with:
    pytest tests/test_task_lifecycle_eval.py -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.runtime.task_store import TaskStore


@pytest.fixture()
def store(tmp_path):
    return TaskStore(str(tmp_path / "eval_tasks.db"))


# ---------------------------------------------------------------------------
# 1. TaskRun lifecycle
# ---------------------------------------------------------------------------

class TestTaskRunLifecycle:
    def test_start_and_finish_run(self, store):
        run = store.start_run("req-build", "implement module A", session_id="s1")
        assert run["id"]
        assert run["status"] == "running"

        store.finish_run(run["id"], "completed")
        task = store.get_task(run["id"])
        assert task["status"] == "completed"

    def test_run_with_steps(self, store):
        run = store.start_run("req-steps", "do it", session_id="s1")

        step = store.start_step(run["id"], "step-1", "llm", name="read docs")
        assert step["id"]
        store.finish_step(step["id"], status="completed")

        step2 = store.start_step(run["id"], "step-2", "tool", name="write code")
        store.finish_step(step2["id"], status="completed")

        store.finish_run(run["id"], "completed")
        task = store.get_task(run["id"])
        assert task["status"] == "completed"

    def test_failed_run(self, store):
        run = store.start_run("req-risky", "try it", session_id="s1")
        store.finish_run(run["id"], "failed", error="something broke")
        task = store.get_task(run["id"])
        assert task["status"] == "failed"
        assert "something broke" in task.get("error", "")

    def test_checkpoint_and_resume(self, store):
        """Checkpoint data must survive for resume."""
        run = store.start_run("req-long", "process data", session_id="s1")

        store.checkpoint(run["id"], {"progress": 42, "last_file": "data.csv"})

        # Simulate interruption
        store.finish_run(run["id"], "interrupted")

        # Resume
        resume_data = store.prepare_resume(run["id"])
        assert resume_data["checkpoint"]["progress"] == 42
        assert resume_data["checkpoint"]["last_file"] == "data.csv"


# ---------------------------------------------------------------------------
# 2. Blocked / waiting states
# ---------------------------------------------------------------------------

class TestBlockedStates:
    def test_block_and_unblock(self, store):
        run = store.start_run("req-wait", "need approval", session_id="s1")

        blocked = store.block_run(run["id"], "waiting for user approval")
        assert blocked["status"] == "blocked_on_user"

        # Must appear in blocked list
        blocked_list = store.blocked_runs()
        assert any(r["id"] == run["id"] for r in blocked_list)

        # Unblock
        unblocked = store.unblock_run(run["id"])
        assert unblocked["status"] == "running"

        # No longer in blocked list
        blocked_list2 = store.blocked_runs()
        assert not any(r["id"] == run["id"] for r in blocked_list2)

    def test_block_kinds(self, store):
        run = store.start_run("req-external", "wait for API", session_id="s1")

        store.block_run(run["id"], "waiting for external API", kind="waiting_external")
        blocked = store.blocked_runs(kind="waiting_external")
        assert any(r["id"] == run["id"] for r in blocked)

        # Should NOT appear in blocked_on_user
        user_blocked = store.blocked_runs(kind="blocked_on_user")
        assert not any(r["id"] == run["id"] for r in user_blocked)

    def test_schedule_run(self, store):
        run = store.start_run("req-scheduled", "check later", session_id="s1")

        scheduled = store.schedule_run(run["id"], "2026-08-01T09:00:00Z")
        assert scheduled["status"] == "scheduled"

        # Not due yet
        due = store.due_scheduled_runs(as_of="2026-07-22T00:00:00Z")
        assert not any(r["id"] == run["id"] for r in due)

        # Due after scheduled time
        due2 = store.due_scheduled_runs(as_of="2026-08-01T10:00:00Z")
        assert any(r["id"] == run["id"] for r in due2)


# ---------------------------------------------------------------------------
# 3. Verify completion
# ---------------------------------------------------------------------------

class TestVerifyCompletion:
    def test_verify_pass(self, store):
        run = store.start_run("req-verify", "build it", session_id="s1")
        store.finish_run(run["id"], "completed")

        result = store.verify_completion(
            run["id"], passed=True, evidence={"tests": "all pass"}
        )
        assert result["status"] == "done_verified"

    def test_verify_fail_keeps_completed(self, store):
        run = store.start_run("req-flaky", "try again", session_id="s1")
        store.finish_run(run["id"], "completed")

        result = store.verify_completion(
            run["id"], passed=False, evidence={"tests": "2 failed"}
        )
        # Should NOT be done_verified
        assert result["status"] != "done_verified"

# ---------------------------------------------------------------------------
# 4. Interrupted recovery
# ---------------------------------------------------------------------------

class TestInterruptedRecovery:
    def test_recover_interrupted_runs(self, store):
        r1 = store.start_run("req-crash-1", "will crash", session_id="s1")
        r2 = store.start_run("req-crash-2", "will also crash", session_id="s1")

        # Simulate crash: runs are left in 'running' state (no finish_run call)
        count = store.recover_interrupted()
        assert count >= 2

        # Both should now be 'interrupted'
        t1 = store.get_task(r1["id"])
        t2 = store.get_task(r2["id"])
        assert t1["status"] == "interrupted"
        assert t2["status"] == "interrupted"

    def test_cancel_run(self, store):
        run = store.start_run("req-cancel", "cancel me", session_id="s1")

        result = store.request_cancel(run["id"])
        assert result is True

        task = store.get_task(run["id"])
        # Two-phase cancel: request sets 'cancelling', runtime confirms 'cancelled'
        assert task["status"] == "cancelling"


# ---------------------------------------------------------------------------
# 5. Events and evidence
# ---------------------------------------------------------------------------

class TestEventsAndEvidence:
    def test_append_event(self, store):
        run = store.start_run("req-events", "do stuff", session_id="s1")

        seq1 = store.append_event(
            run["id"],
            "evt-1",
            "tool_result",
            {"tool": "execute_shell", "exit_code": 0, "summary": "42 passed"},
        )
        seq2 = store.append_event(
            run["id"],
            "evt-2",
            "artifact",
            {"path": "/tmp/report.pdf", "summary": "Generated report"},
        )
        # Events should get incrementing sequence numbers
        assert seq1 >= 1
        assert seq2 > seq1

        # Idempotent: same event_key returns same seq
        seq1_again = store.append_event(
            run["id"],
            "evt-1",
            "tool_result",
            {"tool": "execute_shell", "exit_code": 0},
        )
        assert seq1_again == seq1
