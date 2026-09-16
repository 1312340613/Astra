"""A lost terminal interrupts/saves a real backend without dispatching pending tools."""

import json
import threading
import subprocess
import sys
from pathlib import Path

from agent.runtime.task_store import TaskStore
from test_backend_task_protocol import (
    ThreadingHTTPServer,
    _ApprovalOpenAIHandler,
    _start_question_protocol_backend,
    _stop_protocol_backend,
)


def test_terminal_failure_persists_interruption_and_restores_without_tool_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ApprovalOpenAIHandler)
    server.call_number = 0
    server.target_path = tmp_path / "must-not-be-written.txt"
    server.target_content = "not authorized"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc, _, _, wait = _start_question_protocol_backend(tmp_path, server.server_port, "terminal-interruption")
    restored = None
    try:
        wait(lambda e: e.get("type") == "model_info")
        proc.stdin.write(json.dumps({"type": "message", "text": "Check a local fixture"}) + "\n")
        proc.stdin.flush()
        wait(lambda e: e.get("type") == "tool_approval_request")
        proc.stdin.write(json.dumps({"type": "exit", "reason": "terminal_output_failure"}) + "\n")
        proc.stdin.flush()
        proc.wait(timeout=8)
        assert proc.returncode == 0, proc.stderr.read()
        tasks = TaskStore(tmp_path / "tasks.db").list_tasks()
        assert len(tasks) == 1 and tasks[0]["status"] == "interrupted"
        assert not server.target_path.exists() and server.call_number == 1
        journal = [json.loads(line) for path in (tmp_path / "sessions").glob("*.jsonl")
                   for line in path.read_text().splitlines()]
        assert any(e.get("type") == "session_ended" and e.get("reason") == "terminal_output_failure" for e in journal)
        resume_root = tmp_path / "resume"
        resume_root.mkdir()
        restored, _, _, wait_restored = _start_question_protocol_backend(
            resume_root, server.server_port, "terminal-interruption",
            env_overrides={"AGENT_SESSION_DIR": str(tmp_path / "sessions"),
                           "AGENT_TASK_DB": str(tmp_path / "tasks.db")},
        )
        history = wait_restored(lambda e: e.get("type") == "history")
        assert any("Check a local fixture" in str(m.get("content")) for m in history["messages"])
        restored.stdin.write(json.dumps({"type": "command", "cmd": "/yolo status"}) + "\n")
        restored.stdin.flush()
        wait_restored(lambda e: e.get("type") == "yolo_status")
        assert not server.target_path.exists() and server.call_number == 1
    finally:
        if restored:
            _stop_protocol_backend(restored)
        _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()


def test_writer_timeout_does_not_leave_interpreter_waiting_on_stdout_lock():
    source = """
import asyncio, sys
from agent.cli.backend import _write_event
from agent.runtime.event_writer import OrderedEventWriter
async def run():
    writer = OrderedEventWriter(None, _write_event)
    writer.send({'type': 'chunk', 'content': 'x' * (1024 * 1024)})
    await asyncio.sleep(0.05)
    try:
        await writer.close(timeout=0.05)
    except TimeoutError:
        print('bounded shutdown', file=sys.stderr, flush=True)
asyncio.run(run())
"""
    proc = subprocess.Popen([sys.executable, "-u", "-c", source], cwd=Path(__file__).resolve().parents[1],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # Deliberately do not read stdout, even during interpreter shutdown.
        proc.wait(timeout=5)
        assert proc.returncode == 0, proc.stderr.read()
        assert b"bounded shutdown" in proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
        proc.stdout.close()
        proc.stderr.close()
