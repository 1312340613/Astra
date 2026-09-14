"""Exercise real stdin/HTTP cancellation while manual skill review is running."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent.runtime.skill_learning import LearnedSkills
from agent.runtime.skills import SkillStore
from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.reply({"data": [{"id": "Qwen3.6-35B-A3B"}]})

    def reply(self, value):
        content = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not body.get("stream"):
            self.server.review_started.set()
            self.server.release_review.wait(6)
            try:
                self.reply({"id": "review", "object": "chat.completion", "model": "test", "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": json.dumps({"actions": [
                        {"action": "archive", "names": ["debug-one"], "reason": "This is a fixture."}
                    ]})}, "finish_reason": "stop"}]})
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in ({"choices": [{"index": 0, "delta": {"content": "New task accepted."}, "finish_reason": None}]},
                          {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}):
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()


@pytest.mark.parametrize("interrupt", ["cancel", "message", "exit"])
def test_manual_review_remains_interruptible_and_never_commits_late(tmp_path, monkeypatch, interrupt):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    learned = LearnedSkills(SkillStore(tmp_path / "skills"))
    learned.manage("create", "debug-one", content="---\nname: debug-one\ndescription: Debug a failure\n---\nRead the error and check evidence.")
    before = learned._snapshot("debug-one")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.review_started = threading.Event()
    server.release_review = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc, _, seen, wait = _start_question_protocol_backend(tmp_path, server.server_port, "skill-curation")

    def send(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    try:
        banner = wait(lambda e: e.get("type") == "startup_banner")
        assert banner["learning"]["auto"] is False
        assert banner["learning"]["learned"] == 1
        send({"type": "command", "cmd": "/learn REVIEW" if interrupt == "cancel" else "/learn review"})
        wait(lambda e: e.get("type") == "learning_review_status" and e.get("status") == "running")
        assert server.review_started.wait(3)
        send({"type": "command", "cmd": "/YOLO on"})
        wait(lambda e: e.get("type") == "yolo_status" and e.get("yolo") is True, timeout=2)
        assert not any(e.get("type") == "learning_review_status" and e.get("status") == "cancelled" for e in seen)
        if interrupt == "exit":
            send({"type": "exit"})
            proc.wait(timeout=5)
        elif interrupt == "cancel":
            send({"type": "command", "cmd": "/cancel"})
            wait(lambda e: e.get("type") == "tool_result" and e.get("name") == "cancel", timeout=2)
        else:
            send({"type": "message", "text": "Start a new ordinary task"})
            wait(lambda e: e.get("type") == "learning_review_status" and e.get("status") == "cancelled", timeout=2)
            wait(lambda e: e.get("type") == "done" and any(x.get("type") == "chunk" for x in seen), timeout=5)
        server.release_review.set()
        if interrupt != "exit":
            send({"type": "command", "cmd": "/learn history"})
            wait(lambda e: e.get("type") == "tool_result" and e.get("name") == "learn" and '"kind": "save"' in e.get("output", ""))
        assert learned._snapshot("debug-one") == before
        assert all(item["kind"] == "save" for item in learned.history())
        with learned.locked():
            pass
    finally:
        server.release_review.set()
        _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()
