"""Independent Windows kernel identity; no helper stdout or caller-selected PID."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
from dataclasses import asdict, dataclass
from functools import lru_cache
import os
import sys

from agent.runtime.appshot_process import AppshotProcessIdentityError


@dataclass(frozen=True)
class WindowsAppshotProcessIdentity:
    pid: int
    process_start: str
    user_sid: str

    def wire(self) -> dict:
        return asdict(self)


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
        ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260)]


@lru_cache(maxsize=1)
def _api() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    if sys.platform != "win32":
        raise AppshotProcessIdentityError()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    specs = [
        (kernel.OpenProcess, [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        (kernel.CloseHandle, [w.HANDLE], w.BOOL),
        (kernel.WaitForSingleObject, [w.HANDLE, w.DWORD], w.DWORD),
        (kernel.GetProcessTimes, [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4, w.BOOL),
        (kernel.ProcessIdToSessionId, [w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
        (kernel.LocalFree, [ctypes.c_void_p], ctypes.c_void_p),
        (kernel.GetStdHandle, [w.DWORD], w.HANDLE),
        (kernel.GetFileType, [w.HANDLE], w.DWORD),
        (kernel.GetNamedPipeClientProcessId, [w.HANDLE, ctypes.POINTER(w.ULONG)], w.BOOL),
        (kernel.GetNamedPipeServerProcessId, [w.HANDLE, ctypes.POINTER(w.ULONG)], w.BOOL),
        (kernel.CreateToolhelp32Snapshot, [w.DWORD, w.DWORD], w.HANDLE),
        (kernel.Process32FirstW, [w.HANDLE, ctypes.POINTER(_ProcessEntry)], w.BOOL),
        (kernel.Process32NextW, [w.HANDLE, ctypes.POINTER(_ProcessEntry)], w.BOOL),
        (security.OpenProcessToken, [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL),
        (security.GetTokenInformation, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
        (security.ConvertSidToStringSidW, [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)], w.BOOL),
    ]
    for function, args, result in specs:
        function.argtypes, function.restype = args, result
    return kernel, security


def read_windows_process_identity(pid: int) -> WindowsAppshotProcessIdentity:
    if type(pid) is not int or not 0 < pid < 2**31:
        raise AppshotProcessIdentityError()
    kernel, security = _api()
    process = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not process:
        raise AppshotProcessIdentityError()
    token = w.HANDLE()
    try:
        if kernel.WaitForSingleObject(process, 0) != 258:
            raise AppshotProcessIdentityError()
        times = [w.FILETIME() for _ in range(4)]
        ours, theirs = w.DWORD(), w.DWORD()
        if (not kernel.GetProcessTimes(process, *(ctypes.byref(t) for t in times))
            or not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(ours))
            or not kernel.ProcessIdToSessionId(pid, ctypes.byref(theirs)) or ours.value != theirs.value
            or not security.OpenProcessToken(process, 8, ctypes.byref(token))):
            raise AppshotProcessIdentityError()
        size = w.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not ctypes.sizeof(ctypes.c_void_p) <= size.value <= 65536:
            raise AppshotProcessIdentityError()
        data = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 1, data, size.value, ctypes.byref(size)):
            raise AppshotProcessIdentityError()
        sid = w.LPWSTR()
        if not security.ConvertSidToStringSidW(ctypes.c_void_p.from_buffer(data), ctypes.byref(sid)):
            raise AppshotProcessIdentityError()
        try:
            user_sid = sid.value
        finally:
            kernel.LocalFree(ctypes.cast(sid, ctypes.c_void_p))
        start = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        if not start or not user_sid or kernel.WaitForSingleObject(process, 0) != 258:
            raise AppshotProcessIdentityError()
        return WindowsAppshotProcessIdentity(pid, str(start), user_sid)
    finally:
        if token.value:
            kernel.CloseHandle(token)
        kernel.CloseHandle(process)


def read_windows_parent_identity() -> WindowsAppshotProcessIdentity:
    """Prove the literal parent. With venv this is NOT necessarily the TUI;
    admission must use read_windows_recipient_identity instead.
    """
    pid = os.getppid()
    parent = read_windows_process_identity(pid)
    own = read_windows_process_identity(os.getpid())
    if (os.getppid() != pid or parent != read_windows_process_identity(pid)
        or parent.user_sid != own.user_sid or int(parent.process_start) >= int(own.process_start)):
        raise AppshotProcessIdentityError()
    return parent


def _parent_pid(pid: int) -> int:
    kernel, _ = _api()
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        raise AppshotProcessIdentityError()
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32ProcessID == pid:
                return entry.th32ParentProcessID
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        raise AppshotProcessIdentityError()
    finally:
        kernel.CloseHandle(snapshot)


def _stdio_owner() -> WindowsAppshotProcessIdentity:
    kernel, _ = _api()
    owners = []
    for which in (-10, -11):  # STD_INPUT_HANDLE, STD_OUTPUT_HANDLE
        handle = kernel.GetStdHandle(which)
        client, server = w.ULONG(), w.ULONG()
        if (not handle or kernel.GetFileType(handle) != 3
            or not kernel.GetNamedPipeClientProcessId(handle, ctypes.byref(client))
            or not kernel.GetNamedPipeServerProcessId(handle, ctypes.byref(server))
            or not client.value or client.value != server.value):
            raise AppshotProcessIdentityError()
        owners.append(read_windows_process_identity(client.value))
    if owners[0] != owners[1]:
        raise AppshotProcessIdentityError()
    return owners[0]


def read_windows_recipient_identity() -> WindowsAppshotProcessIdentity:
    """Prove both backend stdio pipes belong to the same live TUI ancestor.

    Windows venv redirectors insert a parent process; ancestry alone cannot
    identify which one owns the TUI. No untrusted PID or executable-name guess.
    """
    recipient = _stdio_owner()
    chain = [read_windows_process_identity(os.getpid())]
    for _ in range(2):
        child = chain[-1]
        parent = read_windows_process_identity(_parent_pid(child.pid))
        if parent.user_sid != child.user_sid or int(parent.process_start) >= int(child.process_start):
            raise AppshotProcessIdentityError()
        chain.append(parent)
        if parent == recipient:
            for index, process in enumerate(chain):
                if read_windows_process_identity(process.pid) != process:
                    raise AppshotProcessIdentityError()
                if index + 1 < len(chain) and _parent_pid(process.pid) != chain[index + 1].pid:
                    raise AppshotProcessIdentityError()
            if _stdio_owner() != recipient:
                raise AppshotProcessIdentityError()
            return recipient
    raise AppshotProcessIdentityError()
