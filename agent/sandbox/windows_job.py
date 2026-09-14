"""Opt-in Windows Job Object containment for host subprocess trees.

This is process containment, not a filesystem or credential sandbox. It keeps
an execution tree together, kills descendants when the job closes, and can
enforce aggregate process-count, memory, and CPU-time limits.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any


JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def job_limit_flags(*, memory_mb: int | None, cpu_seconds: int | None, max_processes: int) -> int:
    flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    if memory_mb is not None:
        flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
    if cpu_seconds is not None:
        flags |= JOB_OBJECT_LIMIT_JOB_TIME
    if max_processes < 1:
        raise ValueError("max_processes must be positive")
    return flags


@dataclass
class WindowsJob:
    """Owned Job Object handle. Closing it terminates remaining descendants."""

    handle: int

    @classmethod
    def create(
        cls,
        *,
        process_handle: int,
        memory_mb: int | None,
        cpu_seconds: int | None,
        max_processes: int,
    ) -> "WindowsJob":
        if sys.platform != "win32":
            raise OSError("Windows Job Objects are unavailable on this platform")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        owned = cls(int(handle))
        try:
            limits = _EXTENDED_LIMIT_INFORMATION()
            limits.BasicLimitInformation.LimitFlags = job_limit_flags(
                memory_mb=memory_mb,
                cpu_seconds=cpu_seconds,
                max_processes=max_processes,
            )
            limits.BasicLimitInformation.ActiveProcessLimit = max_processes
            if memory_mb is not None:
                limits.JobMemoryLimit = memory_mb * 1024 * 1024
            if cpu_seconds is not None:
                limits.BasicLimitInformation.PerJobUserTimeLimit = cpu_seconds * 10_000_000
            if not kernel32.SetInformationJobObject(
                wintypes.HANDLE(owned.handle),
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel32.AssignProcessToJobObject(
                wintypes.HANDLE(owned.handle), wintypes.HANDLE(process_handle)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            return owned
        except Exception:
            owned.close()
            raise

    def close(self) -> None:
        if not self.handle:
            return
        if sys.platform != "win32":
            raise OSError("Windows Job Objects are unavailable on this platform")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle(wintypes.HANDLE(self.handle))
        self.handle = 0


def asyncio_process_handle(process: Any) -> int:
    """Extract the native handle from an asyncio Windows subprocess."""
    transport = getattr(process, "_transport", None)
    popen = transport.get_extra_info("subprocess") if transport is not None else None
    handle = getattr(popen, "_handle", None)
    if handle is None:
        raise RuntimeError("asyncio subprocess does not expose a Windows process handle")
    return int(handle)
