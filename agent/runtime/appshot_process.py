"""Read-only macOS process identity for Appshot's independently bound recipient.

Importing this module is portable. Native libraries and POSIX UID APIs are used
only by supported-host calls; nothing here captures UI or requests permission.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from functools import lru_cache
import os
import sys


class AppshotProcessIdentityError(ValueError):
    def __init__(self) -> None:
        super().__init__("recipient_identity_unavailable")


@dataclass(frozen=True)
class AppshotProcessIdentity:
    pid: int
    uid: int
    process_start: str


# SDK sys/proc_info.h proc_bsdinfo. Fixed-width ABI on supported macOS; do not
# substitute proc_bsdshortinfo (it contains no process start timestamp).
class _ProcBSDInfo(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint32) for name in (
            "pbi_flags", "pbi_status", "pbi_xstatus", "pbi_pid", "pbi_ppid",
            "pbi_uid", "pbi_gid", "pbi_ruid", "pbi_rgid", "pbi_svuid", "pbi_svgid", "rfu_1",
        )
    ] + [
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
    ] + [
        (name, ctypes.c_uint32) for name in (
            "pbi_nfiles", "pbi_pgid", "pbi_pjobc", "e_tdev", "e_tpgid",
        )
    ] + [
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


_platform = sys.platform
_parent_pid = os.getppid


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise AppshotProcessIdentityError()
    return int(getuid())


def _require_supported() -> None:
    if _platform != "darwin":
        raise AppshotProcessIdentityError()


@lru_cache(maxsize=1)
def _load_proc_pidinfo():
    _require_supported()
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    function = library.proc_pidinfo
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    function.restype = ctypes.c_int
    return function


def _read_bsd_info(pid: int) -> _ProcBSDInfo:
    try:
        size = ctypes.sizeof(_ProcBSDInfo)
        if size != 136:
            raise AppshotProcessIdentityError()
        value = _ProcBSDInfo()
        if _load_proc_pidinfo()(pid, 3, 0, ctypes.byref(value), size) != size:  # PROC_PIDTBSDINFO
            raise AppshotProcessIdentityError()
        return value
    except (OSError, AttributeError, ctypes.ArgumentError) as exc:
        raise AppshotProcessIdentityError() from exc


def read_process_identity(pid: int) -> AppshotProcessIdentity:
    """Read one kernel snapshot; failure never falls back to a guessed identity."""
    _require_supported()
    if type(pid) is not int or not 0 < pid < 2**31:
        raise AppshotProcessIdentityError()
    value = _read_bsd_info(pid)
    seconds, micros = value.pbi_start_tvsec, value.pbi_start_tvusec
    start = seconds * 1_000_000 + micros
    if value.pbi_pid != pid or micros >= 1_000_000 or not 0 < start < 2**64:
        raise AppshotProcessIdentityError()
    return AppshotProcessIdentity(pid=pid, uid=value.pbi_uid, process_start=str(start))


def read_parent_identity() -> AppshotProcessIdentity:
    """Prove the current parent twice, rejecting reparenting and observed PID reuse.

    The TUI spawns the backend directly. Calling the helper's client-identity
    mode from Python would prove Python instead of the TUI, so it is not used.
    This is an observation, not a lease: intake must revalidate after its work.
    """
    _require_supported()
    pid = _parent_pid()
    first = read_process_identity(pid)
    second = read_process_identity(pid)
    uid = _current_uid()
    if _parent_pid() != pid or first != second or first.uid != uid:
        raise AppshotProcessIdentityError()
    return first
