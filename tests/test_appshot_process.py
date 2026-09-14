"""Read-only kernel identity, separate from any capture or permission process."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent.runtime import appshot_process as identity


def info(pid=123, uid=501, seconds=100, micros=42):
    value = identity._ProcBSDInfo()
    value.pbi_pid, value.pbi_uid = pid, uid
    value.pbi_start_tvsec, value.pbi_start_tvusec = seconds, micros
    return value


def fake_kernel(monkeypatch, value):
    monkeypatch.setattr(identity, "_platform", "darwin")
    monkeypatch.setattr(identity, "_read_bsd_info", lambda pid: value)


def test_kernel_start_is_canonical_exact_microseconds(monkeypatch):
    fake_kernel(monkeypatch, info())
    assert identity.read_process_identity(123) == identity.AppshotProcessIdentity(123, 501, "100000042")


@pytest.mark.parametrize("pid", [True, 0, -1, 2**31, "123", None])
def test_invalid_pid_refused_before_native_call(monkeypatch, pid):
    monkeypatch.setattr(identity, "_read_bsd_info", lambda pid: pytest.fail("native call"))
    with pytest.raises(identity.AppshotProcessIdentityError, match="^recipient_identity_unavailable$"):
        identity.read_process_identity(pid)


@pytest.mark.parametrize("value", [
    (124, 501, 100, 42), (123, 501, 0, 0), (123, 501, 100, 1_000_000),
    (123, 501, 2**64 - 1, 0),
])
def test_mismatched_pid_or_invalid_start_fails_closed(monkeypatch, value):
    fake_kernel(monkeypatch, info(*value))
    with pytest.raises(identity.AppshotProcessIdentityError):
        identity.read_process_identity(123)


def test_native_struct_abi_and_partial_read_rejected(monkeypatch):
    # Verified against SDK sys/proc_info.h: proc_bsdinfo (not proc_bsdshortinfo).
    assert ctypes.sizeof(identity._ProcBSDInfo) == 136
    assert identity._ProcBSDInfo.pbi_pid.offset == 12
    assert identity._ProcBSDInfo.pbi_uid.offset == 20
    assert identity._ProcBSDInfo.pbi_start_tvsec.offset == 120
    assert identity._ProcBSDInfo.pbi_start_tvusec.offset == 128
    monkeypatch.setattr(identity, "_platform", "darwin")
    def partial(pid, flavor, arg, pointer, size):
        assert (pid, flavor, arg, size) == (123, 3, 0, 136)
        return size - 1
    monkeypatch.setattr(identity, "_load_proc_pidinfo", lambda: partial)
    with pytest.raises(identity.AppshotProcessIdentityError):
        identity.read_process_identity(123)


def test_unsupported_host_never_loads_native_library(monkeypatch):
    monkeypatch.setattr(identity, "_platform", "win32")
    monkeypatch.setattr(identity, "_load_proc_pidinfo", lambda: pytest.fail("native library loaded"))
    monkeypatch.setattr(identity, "_current_uid", lambda: pytest.fail("POSIX UID called"))
    for call in (lambda: identity.read_process_identity(123), identity.read_parent_identity):
        with pytest.raises(identity.AppshotProcessIdentityError):
            call()


def test_parent_identity_rechecked_and_current_uid_bound(monkeypatch):
    fake_kernel(monkeypatch, info())
    monkeypatch.setattr(identity, "_parent_pid", lambda: 123)
    monkeypatch.setattr(identity, "_current_uid", lambda: 501)
    assert identity.read_parent_identity() == identity.AppshotProcessIdentity(123, 501, "100000042")
    monkeypatch.setattr(identity, "_current_uid", lambda: 502)
    with pytest.raises(identity.AppshotProcessIdentityError):
        identity.read_parent_identity()


@pytest.mark.parametrize("change", ["parent", "start"])
def test_parent_reparent_or_pid_reuse_during_probe_rejected(monkeypatch, change):
    monkeypatch.setattr(identity, "_platform", "darwin")
    monkeypatch.setattr(identity, "_current_uid", lambda: 501)
    pids = iter([123, 124] if change == "parent" else [123, 123])
    starts = iter([info(), info(seconds=101) if change == "start" else info()])
    monkeypatch.setattr(identity, "_parent_pid", lambda: next(pids))
    monkeypatch.setattr(identity, "_read_bsd_info", lambda pid: next(starts))
    with pytest.raises(identity.AppshotProcessIdentityError):
        identity.read_parent_identity()


@pytest.mark.skipif(sys.platform != "darwin", reason="read-only native ABI integration")
def test_actual_kernel_identity_matches_swift_helper_parent_identity():
    binary = Path(__file__).resolve().parents[1] / "native/macos-computer-helper/.build/debug/AstraMacComputerHelper"
    if not binary.is_file():
        pytest.skip("build native helper to compare Swift/Python process identities")
    result = subprocess.run([str(binary), "--appshot-client-identity"], input="", text=True,
                            capture_output=True, timeout=5, check=True)
    proof = json.loads(result.stdout)
    actual = identity.read_process_identity(os.getpid())
    assert actual == identity.AppshotProcessIdentity(proof["pid"], proof["uid"], proof["process_start"])
    assert actual.uid == os.getuid() and actual.pid == os.getpid()
    parent = identity.read_parent_identity()
    assert parent.pid == os.getppid() and parent.uid == os.getuid()
    assert int(parent.process_start) > 0 and result.stderr == ""


def test_native_loader_error_is_bounded(monkeypatch):
    monkeypatch.setattr(identity, "_platform", "darwin")
    def unavailable():
        raise OSError("private native diagnostic must not become a UI message")
    monkeypatch.setattr(identity, "_load_proc_pidinfo", unavailable)
    with pytest.raises(identity.AppshotProcessIdentityError) as exc:
        identity.read_process_identity(123)
    assert str(exc.value) == "recipient_identity_unavailable"
