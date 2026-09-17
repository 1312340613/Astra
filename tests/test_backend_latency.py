"""Real IPC with a local model fixture and contended task persistence."""

import json
import sqlite3
import threading
import time

import pytest

from agent.runtime.task_store import TaskStore
from test_backend_task_protocol import (
    ThreadingHTTPServer,
    _ApprovalOpenAIHandler,
    _start_question_protocol_backend,
    _stop_protocol_backend,
)


def test_cancel_and_yolo_remain_reachable_during_task_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_PROFILE_QUERY", "1")
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ApprovalOpenAIHandler)
    server.call_number = 1  # If reached, the fixture emits plain text.
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc, _, seen, wait = _start_question_protocol_backend(tmp_path, server.server_port, "latency-test")
    lock = None
    try:
        wait(lambda e: e.get("type") == "model_info")
        lock = sqlite3.connect(tmp_path / "tasks.db")
        lock.execute("BEGIN IMMEDIATE")
        proc.stdin.write(json.dumps({"type": "message", "text": "PRIVATE REQUEST"}) + "\n")
        proc.stdin.flush()
        time.sleep(0.15)  # Let task creation reach the held writer lock.
        started = time.monotonic()
        proc.stdin.write(json.dumps({"type": "command", "cmd": "/yolo on"}) + "\n")
        proc.stdin.flush()
        wait(lambda e: e.get("type") == "yolo_status" and e.get("yolo") is True, timeout=2)
        assert time.monotonic() - started < 2
        proc.stdin.write(json.dumps({"type": "command", "cmd": "/cancel"}) + "\n")
        proc.stdin.flush()
        wait(lambda e: e.get("type") == "tool_result" and e.get("name") == "cancel", timeout=2)
        proc.stdin.write(json.dumps({"type": "command", "cmd": "/yolo off"}) + "\n")
        proc.stdin.flush()
        wait(lambda e: e.get("type") == "yolo_status" and e.get("yolo") is False, timeout=2)
        lock.rollback()
        lock.close()
        lock = None
        terminal = wait(lambda e: e.get("type") == "task_status" and e.get("task", {}).get("status") == "cancelled")
        wait(lambda e: e.get("type") == "done")
        assert server.call_number == 1  # No late provider call after the claim settles.
        assert TaskStore(tmp_path / "tasks.db").get_task(terminal["task"]["id"])["status"] == "cancelled"
        traced = [event for event in seen if "performance_trace_id" in event]
        assert traced
        proc.stdin.write(json.dumps({"type": "performance_ack", "samples": [
            {"trace_id": traced[-1]["performance_trace_id"], "handle_ms": 1, "react_commit_ms": 2},
        ]}) + "\n")
        proc.stdin.flush()
    finally:
        if lock is not None:
            lock.rollback()
            lock.close()
        _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()
    raw = (tmp_path / "state" / "runtime-profile.jsonl").read_text()
    assert "PRIVATE REQUEST" not in raw
    records = list(map(json.loads, raw.splitlines()))
    assert any(r["kind"] == "frontend" for r in records)
    assert any(r["kind"] == "io" for r in records)


@pytest.mark.parametrize("blocked_phase", ["create", "resolve"])
def test_cancellation_during_approval_io_cannot_dispatch_tool(tmp_path, blocked_phase):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ApprovalOpenAIHandler)
    server.call_number = 0
    server.target_path = tmp_path / "never-written.py"
    server.target_content = "must not be written"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # Measure the real approval database contention after entry, not tool
    # preparation or unrelated replay-journal fsync. Windows runners can spend
    # several seconds committing earlier progress events in the output queue;
    # that does not mean the control loop is blocked. Event persistence and
    # ordered delivery have their own slow-I/O tests in test_event_writer.py.
    bootstrap = f"""
from agent.cli import backend
from agent.runtime.approval_inbox import ApprovalInbox
backend.RuntimeEventStream = lambda: None
original = ApprovalInbox.{blocked_phase}
def observe_entry(self, *args, **kwargs):
    backend._send({{"type": "approval_io_entered", "phase": {blocked_phase!r}}})
    return original(self, *args, **kwargs)
ApprovalInbox.{blocked_phase} = observe_entry
raise SystemExit(backend.run())
"""
    proc, _, _, wait = _start_question_protocol_backend(
        tmp_path, server.server_port, "approval-latency", bootstrap_code=bootstrap,
    )
    lock = None

    def send(payload):
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    try:
        wait(lambda e: e.get("type") == "model_info")
        if blocked_phase == "create":
            lock = sqlite3.connect(tmp_path / "approvals.db")
            lock.execute("BEGIN IMMEDIATE")
        send({"type": "message", "text": "write the fixture"})
        if blocked_phase == "resolve":
            approval = wait(lambda e: e.get("type") == "tool_approval_request")
            lock = sqlite3.connect(tmp_path / "approvals.db")
            lock.execute("BEGIN IMMEDIATE")
            send({"type": "tool_approval_response", "request_id": approval["request_id"], "decision": "once"})
        wait(lambda e: e.get("type") == "approval_io_entered" and e.get("phase") == blocked_phase)
        send({"type": "command", "cmd": "/yolo status"})
        wait(lambda e: e.get("type") == "yolo_status", timeout=2)
        send({"type": "command", "cmd": "/cancel"})
        wait(lambda e: e.get("type") == "tool_result" and e.get("name") == "cancel", timeout=2)
        assert not server.target_path.exists()
        lock.rollback()
        lock.close()
        lock = None
        wait(lambda e: e.get("type") == "task_status" and e.get("task", {}).get("status") == "cancelled")
        assert not server.target_path.exists()
        assert server.call_number == 1
    finally:
        if lock is not None:
            lock.rollback()
            lock.close()
        _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()
