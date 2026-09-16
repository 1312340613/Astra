"""Exercise real stdin/HTTP cancellation while conversational review is running."""

import json
import re
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
        if "messages" not in body:
            self.reply({"data": []})
            return
        self.server.requests.append(body)
        if len(self.server.requests) == 1:
            self.server.review_started.set()
            self.server.release_review.wait(6)
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
    server.requests = []
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
        wait(lambda e: e.get("type") == "task_started")
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
            wait(lambda e: e.get("type") == "steering", timeout=2)
        server.release_review.set()
        assert all(body.get("stream") for body in server.requests), [
            (body.get("stream"), body.get("max_tokens"), [(m["role"], str(m.get("content", ""))[:100]) for m in body["messages"]])
            for body in server.requests
        ]
        if interrupt == "message":
            wait(lambda e: e.get("type") == "done" and any(x.get("type") == "chunk" for x in seen), timeout=5)
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


class ConversationHandler(Handler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "messages" not in body:
            self.reply({"data": []})
            return
        index = len(self.server.requests)
        self.server.requests.append(body)
        tool, args = "", {}
        if index == 0:
            tool = "skill_review_snapshot"
        elif index in {1, 3}:
            evidence = next(m["content"] for m in body["messages"]
                            if m["role"] == "tool" and '"snapshot_id"' in m["content"])
            token = re.search(r'"snapshot_id":\s*"([^"]+)"', evidence)[1]
            tool = "skill_review_apply"
            args = {"snapshot_id": token, "actions": [{"action": "rewrite", "names": ["debug-one"],
                    "reason": "Clarify validation scope.", "patches": [{
                        "old_string": "Read the error.", "new_string": "Read the error and validate a temporary copy.",
                    }]}]}
        if tool:
            delta = {"tool_calls": [{"index": 0, "id": f"call_{index}", "type": "function", "function": {
                "name": tool, "arguments": json.dumps(args),
            }}]}
        else:
            delta = {"content": "Review proposal ready." if index == 2 else "Selected change saved."}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in ({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}]}):
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def test_conversational_review_keeps_evidence_for_authorized_followup(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    learned = LearnedSkills(SkillStore(tmp_path / "skills"))
    learned.manage("create", "debug-one", content="---\nname: debug-one\ndescription: Debug a failure\n---\nRead the error.\n")
    before = learned._snapshot("debug-one")
    server = ThreadingHTTPServer(("127.0.0.1", 0), ConversationHandler)
    server.requests = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc, _, seen, wait = _start_question_protocol_backend(tmp_path, server.server_port, "review-conversation")

    def send(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    try:
        wait(lambda e: e.get("type") == "startup_banner")
        send({"type": "command", "cmd": "/yolo on"})
        wait(lambda e: e.get("type") == "yolo_status" and e.get("yolo"))
        send({"type": "command", "cmd": "/learn review"})
        wait(lambda e: e.get("type") == "chunk" and "Review proposal" in e.get("content", ""), timeout=15)
        wait(lambda e: e.get("type") == "task_status" and e.get("task", {}).get("status") == "completed")
        assert learned._snapshot("debug-one") == before
        assert any(e.get("name") == "skill_review_apply" and "ToolDisabled" in e.get("error", "") for e in seen)
        names = {t["function"]["name"] for t in server.requests[0]["tools"]}
        assert "skill_review_snapshot" in names and "skill_review_apply" not in names
        send({"type": "message", "text": "Apply the proposed change; skip execution testing."})
        wait(lambda e: e.get("type") == "chunk" and "Selected change saved" in e.get("content", ""), timeout=15)
        assert "temporary copy" in learned._snapshot("debug-one")["SKILL.md"]
        assert all(b.get("stream") for b in server.requests)
        assert any("Review proposal" in str(m.get("content", "")) for m in server.requests[3]["messages"])
        assert any(r["kind"] == "review" for r in learned.history())
        send({"type": "command", "cmd": "/session review-conversation"})
        restored = wait(lambda e: e.get("type") == "history" and any(m.get("content") == "/learn review" for m in e.get("messages", [])))
        assert not any("Astra command workflow" in str(m.get("content", "")) for m in restored["messages"] if m["role"] == "user")
    finally:
        _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()
