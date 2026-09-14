"""Unsupported native entrypoints fail before touching files or Win32 APIs."""

import os
import sys

import pytest

from agent.mail163.attachment_storage import (
    AttachmentDirectory,
    WindowsAttachmentStorage,
    _windows_directory_path,
)
from agent.runtime.activity_recorder import windows
from agent.sandbox.windows_job import WindowsJob


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="non-Windows boundary checks")


def test_windows_attachment_directory_rejects_before_creating_path(tmp_path):
    root = tmp_path / "must-not-exist"
    with pytest.raises(OSError, match="Windows attachment storage is unavailable"):
        WindowsAttachmentStorage().open_directory(root, ("child",))
    assert not root.exists()


@pytest.mark.parametrize("operation", ["open_file", "entry_exists", "unlink", "publish"])
def test_windows_attachment_io_reports_platform_error(tmp_path, operation):
    storage = WindowsAttachmentStorage()
    directory = AttachmentDirectory(tmp_path, windows_handles=[123])
    arguments = {
        "open_file": (directory, "file.txt", os.O_CREAT | os.O_EXCL, 0o600),
        "entry_exists": (directory, "file.txt"),
        "unlink": (directory, "file.txt"),
        "publish": (directory, "temporary.txt", "file.txt"),
    }
    with pytest.raises(OSError, match="Windows attachment storage is unavailable"):
        getattr(storage, operation)(*arguments[operation])


def test_windows_directory_path_reports_platform_error():
    with pytest.raises(OSError, match="Windows attachment storage is unavailable"):
        _windows_directory_path(123)


def test_windows_probe_and_runner_report_platform_error_before_native_calls(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="Windows only"):
        windows.WindowsProbe()
    probe = object.__new__(windows.WindowsProbe)
    with pytest.raises(RuntimeError, match="Windows only"):
        probe.sample(120)
    state = tmp_path / "must-not-exist"
    monkeypatch.setattr(windows, "STATE", state)
    with pytest.raises(RuntimeError, match="Windows only"):
        windows.run(None)
    assert not state.exists()


def test_windows_job_reports_platform_error_without_changing_handle():
    with pytest.raises(OSError, match="Windows Job Objects are unavailable"):
        WindowsJob.create(process_handle=123, memory_mb=None, cpu_seconds=None, max_processes=1)
    job = WindowsJob(123)
    with pytest.raises(OSError, match="Windows Job Objects are unavailable"):
        job.close()
    assert job.handle == 123
    WindowsJob(0).close()
