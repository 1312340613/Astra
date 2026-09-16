"""Real POSIX PTYs, stopped readers and detach; no user terminal is touched."""

import json
import os
import queue
import selectors
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix" or not shutil.which("node"), reason="POSIX PTY and Node required")


@pytest.mark.parametrize("mode", ["recover", "blocked", "detach"])
def test_terminal_output_keeps_control_loop_live_and_saves_on_shutdown(tmp_path, mode):
    import fcntl
    import pty
    import resource
    import struct
    import termios
    import tty

    root = Path(__file__).resolve().parents[1]
    master, slave = pty.openpty()
    # The complete suite may already hold more than FD_SETSIZE descriptors.
    # Exercise that case directly instead of relying on test ordering.
    if resource.getrlimit(resource.RLIMIT_NOFILE)[0] > 1100:
        high_master = fcntl.fcntl(master, fcntl.F_DUPFD, 1024)
        os.close(master)
        master = high_master
    reader = selectors.DefaultSelector()
    reader.register(master, selectors.EVENT_READ)
    tty.setraw(slave)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    saved = tmp_path / "saved.json"
    proc = subprocess.Popen(
        [shutil.which("node"), "--import", "tsx", "src/fixtures/terminal-output-pty.ts", mode, str(saved)],
        cwd=root / "ui-tui", stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    events, seen = queue.Queue(), []

    def read_control():
        for line in proc.stdout:
            events.put(json.loads(line))

    threading.Thread(target=read_control, daemon=True).start()

    def wait(kind, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = events.get(timeout=max(0.01, deadline - time.monotonic()))
            seen.append(event)
            if event.get("type") == kind:
                return event
        pytest.fail(f"Missing {kind}: {seen}")

    try:
        wait("ready")
        # A synchronous TTY writer would not read ping until we drained master.
        time.sleep(0.1)
        proc.stdin.write(b"ping\n")
        proc.stdin.flush()
        wait("pong", timeout=1)
        assert any(e["type"] == "heartbeat" for e in seen)
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 96, 0, 0))
        os.kill(proc.pid, signal.SIGWINCH)
        resize = wait("resize", timeout=1)
        assert (resize["rows"], resize["columns"]) == (28, 96)
        if mode == "recover":
            data = bytearray()
            deadline = time.monotonic() + 3
            while len(data) < 1024 * 1024 + 6 and time.monotonic() < deadline:
                if reader.select(0.1):
                    data.extend(os.read(master, 4096))
            assert data == b"\x1b[?25l" + b"a" * (1024 * 1024)
            assert wait("written")["ok"]
            proc.stdin.write(b"close\n")
            proc.stdin.flush()
        elif mode == "detach":
            os.close(master)
            master = -1
        result = wait("closed")
        assert not result["forced"] and result["exited"]
        assert result["peakPendingBytes"] <= 4 * 1024 * 1024
        assert result["drained"] is (mode == "recover")
        if mode != "recover":
            assert result["code"] in {"ETIMEDOUT", "EIO", "EPIPE"}
        proc.wait(timeout=3)
        assert proc.returncode == 0
        assert json.loads(saved.read_text()) == {
            "type": "exit", "reason": "user_exit" if mode == "recover" else "terminal_output_failure",
        }
    finally:
        reader.close()
        if master != -1:
            os.close(master)
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=3)
        proc.stdin.close()
        proc.stdout.close()
