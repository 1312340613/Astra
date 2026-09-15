import json

from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend

from agent.cli.session_lifecycle import RESTART_EXIT_CODE
from agent.runtime.session_store import SessionStore


def send(proc, value):
    proc.stdin.write(json.dumps(value) + "\n")
    proc.stdin.flush()


def start(tmp_path):
    return _start_question_protocol_backend(
        tmp_path, 9, "lifecycle_session", env_overrides={
            "ASTRA_TUI_RESTART": "1",
            "ASTRA_CHANNEL_CONFIG": str(tmp_path / "no-channels.json"),
            "ASTRA_CONTEXT_INDEX_EMBEDDING": "off",
        },
    )


def test_real_backend_exits_only_after_ready_receipt_and_saves_session(tmp_path):
    proc, _, _, wait_for = start(tmp_path)
    try:
        wait_for(lambda e: e.get("type") == "startup_banner", timeout=30)
        send(proc, {"type": "command", "cmd": "/restart"})
        ready = wait_for(lambda e: e.get("type") == "restart_ready")
        assert ready["session"] == "lifecycle_session"
        assert ready["replayable"] is False
        send(proc, {"type": "restart_ack", "request_id": "stale"})
        send(proc, {"type": "command", "cmd": "/session other"})
        rejected = wait_for(lambda e: e.get("type") == "restart_status" and "changing" in e.get("message", ""))
        assert rejected["state"] == "awaiting_ack"
        assert proc.poll() is None
        send(proc, {"type": "restart_ack", "request_id": ready["request_id"]})
        assert proc.wait(timeout=15) == RESTART_EXIT_CODE, proc.stderr.read()
        assert SessionStore(tmp_path / "sessions/lifecycle_session.json").exists
        assert not SessionStore(tmp_path / "sessions/other.json").exists
    finally:
        _stop_protocol_backend(proc)


def test_real_backend_cancel_restores_command_admission(tmp_path):
    proc, _, _, wait_for = start(tmp_path)
    try:
        wait_for(lambda e: e.get("type") == "startup_banner", timeout=30)
        send(proc, {"type": "command", "cmd": "/restart"})
        ready = wait_for(lambda e: e.get("type") == "restart_ready")
        send(proc, {"type": "command", "cmd": "/restart cancel"})
        wait_for(lambda e: e.get("type") == "restart_status" and e.get("state") == "cancelled")
        send(proc, {"type": "restart_ack", "request_id": ready["request_id"]})
        send(proc, {"type": "command", "cmd": "/tasks"})
        wait_for(lambda e: e.get("type") == "tool_result" and e.get("name") == "tasks")
        assert proc.poll() is None
    finally:
        _stop_protocol_backend(proc)
