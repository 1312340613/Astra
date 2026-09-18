"""Platform-safe process liveness probes (review R8).

``os.kill(pid, 0)`` is a harmless existence probe only on POSIX. On Windows,
CPython routes non-console signals through ``TerminateProcess``, so the shared
probe must query the process handle instead. These tests pin the semantics the
turn-change cleanup and the process manager both rely on: a live target stays
untouched, a dead pid reads as dead, and invalid pids are rejected.
"""

from __future__ import annotations

import sys
import subprocess
import time

from agent.runtime.process_env import pid_alive


def _spawn_sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])


def test_pid_alive_reports_live_and_dead_processes():
    proc = _spawn_sleeper()
    try:
        assert pid_alive(proc.pid) is True
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    # A reaped process must read as not alive.
    assert pid_alive(proc.pid) is False


def test_pid_alive_probe_sends_no_signal_to_the_target():
    """The probe must never disturb the process it is asking about (R8)."""
    proc = _spawn_sleeper()
    try:
        for _ in range(3):
            assert pid_alive(proc.pid) is True
        time.sleep(0.05)
        assert proc.poll() is None  # still running: no signal was delivered
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_pid_alive_rejects_invalid_pids():
    assert pid_alive(0) is False
    assert pid_alive(-1) is False
