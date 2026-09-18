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
from types import SimpleNamespace

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


# ---------------------------------------------------------------------------
# Windows branch: simulated WinAPI return values (no Windows host available).
# Query failure means "cannot confirm", not "the process exited".
# ---------------------------------------------------------------------------

class _FakeKernel32:
    """Minimal WinAPI stand-in that scripts the branch under test."""

    def __init__(self, *, handle: int = 123, exit_query_ok: bool = True, exit_code: int = 0) -> None:
        self.handle = handle
        self.exit_query_ok = exit_query_ok
        self.exit_code = exit_code
        self.closed: list[int] = []

    def OpenProcess(self, *_args):
        return self.handle

    def GetExitCodeProcess(self, _handle, exit_code_ptr):
        if not self.exit_query_ok:
            return 0  # query failed
        exit_code_ptr._obj.value = self.exit_code
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


def _probe_windows(monkeypatch, kernel: _FakeKernel32, *, last_error: int = 0) -> bool:
    import ctypes

    from agent.runtime import process_env

    monkeypatch.setattr(process_env, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)
    return process_env.pid_alive(4242)


def test_windows_probe_treats_a_running_process_as_alive(monkeypatch):
    kernel = _FakeKernel32(exit_code=259)  # STILL_ACTIVE
    assert _probe_windows(monkeypatch, kernel) is True
    assert kernel.closed  # the handle is released


def test_windows_probe_reports_a_confirmed_exit(monkeypatch):
    assert _probe_windows(monkeypatch, _FakeKernel32(exit_code=0)) is False


def test_windows_probe_keeps_an_unqueryable_process_alive(monkeypatch):
    """A failed GetExitCodeProcess proves nothing: stay conservative (F3)."""
    assert _probe_windows(monkeypatch, _FakeKernel32(exit_query_ok=False)) is True


def test_windows_probe_open_failure_is_uncertain_except_a_missing_pid(monkeypatch):
    kernel = _FakeKernel32(handle=0)
    assert _probe_windows(monkeypatch, kernel, last_error=5) is True    # access denied
    assert _probe_windows(monkeypatch, kernel, last_error=0) is True    # unknown failure
    assert _probe_windows(monkeypatch, kernel, last_error=87) is False  # invalid parameter
