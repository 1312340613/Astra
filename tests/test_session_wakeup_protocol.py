"""Real backend + fake model, with the scheduler's clock advanced in the fixture."""

import json
import threading
from http.server import ThreadingHTTPServer

import pytest
from test_backend_task_protocol import (
    _SlowOpenAIHandler,
    _start_question_protocol_backend,
    _stop_protocol_backend,
)
from test_session_lifecycle_protocol import send


BOOTSTRAP = '''
import time
from agent.cli import backend
from agent.runtime.session_wakeup import SessionWakeups
class FastWakeups(SessionWakeups):
    def __init__(self):
        super().__init__(clock=lambda: time.monotonic() * 60)
backend.SessionWakeups = FastWakeups
raise SystemExit(backend.run())
'''


@pytest.fixture
def model():
    requests = []
    gate = threading.Event()
    gate.set()

    class Handler(_SlowOpenAIHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if "messages" not in payload:
                self._json({"total_tokens": 100})
                return
            requests.append(payload)
            messages = payload["messages"]
            user = next(m for m in reversed(messages) if m["role"] == "user")
            scheduled = "Scheduled session wakeup" in str(user["content"])
            if not scheduled and user["content"] == "Hold this normal turn":
                gate.wait(timeout=10)
            delta = {"content": "normal reply"}
            finish = "stop"
            if scheduled and messages[-1]["role"] != "tool":
                count = sum("Scheduled session wakeup" in str(m.get("content", "")) for m in messages if m["role"] == "user")
                outcome = "unchanged" if count == 1 else "completed"
                summary = "" if outcome == "unchanged" else "CI passed: https://example.test/run/1"
                delta = {"content": "internal narration", "tool_calls": [{
                    "index": 0, "id": f"report-{count}", "type": "function",
                    "function": {"name": "report_wakeup", "arguments": json.dumps({"outcome": outcome, "summary": summary})},
                }]}
                finish = "tool_calls"
                gate.wait(timeout=10)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunk = {"id": "fixture", "object": "chat.completion.chunk", "model": "Qwen3.6-35B-A3B",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            try:
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, requests, gate
    gate.set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def start(tmp_path, model):
    return _start_question_protocol_backend(
        tmp_path, model[0], "wakeup_session", bootstrap_code=BOOTSTRAP,
        env_overrides={"ASTRA_CHANNEL_CONFIG": str(tmp_path / "no-channels.json"),
                       "ASTRA_CONTEXT_INDEX_EMBEDDING": "off", "ASTRA_TUI_RESTART": "1"},
    )


def test_repetition_stays_quiet_then_reports_completion_once(tmp_path, model):
    proc, _, seen, wait_for = start(tmp_path, model)
    try:
        wait_for(lambda e: e.get("type") == "startup_banner", timeout=30)
        send(proc, {"type": "command", "cmd": "/wakeup every 60 Check CI and stop when finished"})
        wait_for(lambda e: e.get("type") == "wakeup_status" and e.get("plan", {}).get("outcome") == "unchanged")
        assert not [e for e in seen if e.get("type") in {"chunk", "reasoning", "steering"}]
        wait_for(lambda e: e.get("type") == "chunk" and "CI passed" in e.get("content", ""))
        wait_for(lambda e: e.get("type") == "done")
        chunks = [e["content"] for e in seen if e.get("type") == "chunk"]
        assert chunks == ["CI passed: https://example.test/run/1"]
        send(proc, {"type": "command", "cmd": "/wakeup"})
        status = wait_for(lambda e: e.get("type") == "wakeup_status" and bool(e.get("message")))
        assert status["plan"]["state"] == "completed"
        assert status["plan"]["runs"] == 2
        assert len(model[1]) == 4  # report + final reply for each tick
    finally:
        _stop_protocol_backend(proc)


def test_cancel_stops_running_check_and_drops_late_output(tmp_path, model):
    model[2].clear()
    proc, _, seen, wait_for = start(tmp_path, model)
    try:
        wait_for(lambda e: e.get("type") == "startup_banner", timeout=30)
        send(proc, {"type": "command", "cmd": "/wakeup every 60 Check CI"})
        wait_for(lambda e: e.get("type") == "task_started")
        send(proc, {"type": "command", "cmd": "/wakeup cancel"})
        status = wait_for(lambda e: e.get("type") == "wakeup_status" and e.get("plan", {}).get("state") == "cancelled")
        assert status["plan"]["runs"] == 1
        model[2].set()
        send(proc, {"type": "message", "text": "Continue normal work"})
        wait_for(lambda e: e.get("type") == "chunk" and e.get("content") == "normal reply")
        assert all("internal narration" not in e.get("content", "") for e in seen)
    finally:
        model[2].set()
        _stop_protocol_backend(proc)


@pytest.mark.parametrize(("command", "state"), [("/session another", "session_changed"), ("/reset", "session_reset")])
def test_switching_or_resetting_session_stops_pending_plan(tmp_path, model, command, state):
    proc, _, _, wait_for = start(tmp_path, model)
    try:
        wait_for(lambda e: e.get("type") == "startup_banner", timeout=30)
        send(proc, {"type": "command", "cmd": "/wakeup after 1200 Check later"})
        wait_for(lambda e: e.get("type") == "wakeup_status")
        send(proc, {"type": "command", "cmd": command})
        status = wait_for(lambda e: e.get("type") == "wakeup_status" and e.get("plan", {}).get("state") == state)
        assert status["plan"]["runs"] == 0
        assert not model[1]
    finally:
        _stop_protocol_backend(proc)


def test_busy_turn_drains_before_restart_and_due_wakeup_does_not_start(tmp_path, model):
    model[2].clear()
    proc, _, seen, wait_for = start(tmp_path, model)
    try:
        wait_for(lambda e: e.get("type") == "startup_banner", timeout=30)
        send(proc, {"type": "message", "text": "Hold this normal turn"})
        wait_for(lambda e: e.get("type") == "task_started")
        send(proc, {"type": "command", "cmd": "/wakeup after 60 Check CI"})
        wait_for(lambda e: e.get("type") == "wakeup_status")
        send(proc, {"type": "command", "cmd": "/restart"})
        wait_for(lambda e: e.get("type") == "restart_status" and e.get("state") == "draining")
        assert not [e for e in seen if e.get("type") == "restart_ready"]
        model[2].set()
        wait_for(lambda e: e.get("type") == "done")
        ready = wait_for(lambda e: e.get("type") == "restart_ready")
        assert len([e for e in seen if e.get("type") == "task_started"]) == 1
        send(proc, {"type": "restart_ack", "request_id": ready["request_id"]})
        assert proc.wait(timeout=15) == 42
    finally:
        model[2].set()
        _stop_protocol_backend(proc)
