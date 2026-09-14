import asyncio
from types import SimpleNamespace

import pytest

from agent.sandbox import local
from agent.sandbox.local import LocalSandbox, SandboxError
from agent.sandbox.windows_job import (
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
    JOB_OBJECT_LIMIT_JOB_MEMORY,
    JOB_OBJECT_LIMIT_JOB_TIME,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    asyncio_process_handle,
    job_limit_flags,
)


def test_job_flags_are_explicit_and_bounded():
    flags = job_limit_flags(memory_mb=256, cpu_seconds=10, max_processes=8)
    assert flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert flags & JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    assert flags & JOB_OBJECT_LIMIT_JOB_MEMORY
    assert flags & JOB_OBJECT_LIMIT_JOB_TIME
    with pytest.raises(ValueError, match="positive"):
        job_limit_flags(memory_mb=None, cpu_seconds=None, max_processes=0)


def test_asyncio_process_handle_uses_subprocess_transport():
    popen = SimpleNamespace(_handle=1234)
    transport = SimpleNamespace(get_extra_info=lambda name: popen if name == "subprocess" else None)
    assert asyncio_process_handle(SimpleNamespace(_transport=transport)) == 1234


def test_opt_in_job_assignment_is_fail_closed(monkeypatch, tmp_path):
    sandbox = LocalSandbox(workdir=str(tmp_path), windows_job_containment=True)
    async def wait():
        waited.append(True)

    proc = SimpleNamespace(returncode=None, kill=lambda: killed.append(True), wait=wait)
    killed = []
    waited = []
    monkeypatch.setattr(local.sys, "platform", "win32")
    monkeypatch.setattr(local, "asyncio_process_handle", lambda process: 77)
    monkeypatch.setattr(local.WindowsJob, "create", lambda **kwargs: (_ for _ in ()).throw(OSError("no job")))

    with pytest.raises(SandboxError, match="could not be applied"):
        asyncio.run(sandbox._attach_windows_job(proc))
    assert killed == [True]
    assert waited == [True]


def test_wsl_is_rejected_when_windows_containment_is_enabled(monkeypatch, tmp_path):
    sandbox = LocalSandbox(workdir=str(tmp_path), windows_job_containment=True)
    monkeypatch.setattr(local.sys, "platform", "win32")

    with pytest.raises(SandboxError, match="does not isolate Linux processes"):
        asyncio.run(sandbox.execute_shell_stream("pwd", environment="wsl"))
