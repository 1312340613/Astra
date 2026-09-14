from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Protocol


@dataclass
class AttachmentDirectory:
    """Pinned attachment directory used for race-safe relative operations."""

    path: Path
    posix_fd: int | None = None
    windows_handles: list[int] = field(default_factory=list)
    closed: bool = False


class AttachmentStorage(Protocol):
    def open_directory(self, root: Path, components: tuple[str, ...]) -> AttachmentDirectory: ...

    def open_file(self, directory: AttachmentDirectory, name: str, flags: int, mode: int) -> int: ...

    def entry_exists(self, directory: AttachmentDirectory, name: str) -> bool: ...

    def unlink(self, directory: AttachmentDirectory, name: str) -> None: ...

    def publish(self, directory: AttachmentDirectory, temporary_name: str, target_name: str) -> None: ...

    def secure_stream(self, descriptor: int) -> None: ...

    def current_path(self, directory: AttachmentDirectory) -> Path: ...

    def close_directory(self, directory: AttachmentDirectory) -> None: ...


class PosixAttachmentStorage:
    @staticmethod
    def _fchmod(descriptor: int, mode: int) -> None:
        fchmod = getattr(os, "fchmod", None)
        if fchmod is None:
            raise OSError("POSIX permission controls are unavailable")
        fchmod(descriptor, mode)

    def open_directory(self, root: Path, components: tuple[str, ...]) -> AttachmentDirectory:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if no_follow is None or directory_flag is None:
            raise OSError("safe attachment traversal is unavailable")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDONLY | no_follow | directory_flag
        directory_fd = os.open(root, flags)
        try:
            current = root
            for component in components:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(component, flags, dir_fd=directory_fd)
                try:
                    self._fchmod(child_fd, 0o700)
                except BaseException:
                    os.close(child_fd)
                    raise
                os.close(directory_fd)
                directory_fd = child_fd
                current /= component
            return AttachmentDirectory(path=current, posix_fd=directory_fd)
        except BaseException:
            os.close(directory_fd)
            raise

    @staticmethod
    def _descriptor(directory: AttachmentDirectory) -> int:
        if directory.closed or directory.posix_fd is None:
            raise OSError("attachment directory is closed")
        return directory.posix_fd

    def open_file(self, directory: AttachmentDirectory, name: str, flags: int, mode: int) -> int:
        return os.open(name, flags, mode, dir_fd=self._descriptor(directory))

    def entry_exists(self, directory: AttachmentDirectory, name: str) -> bool:
        try:
            os.stat(name, dir_fd=self._descriptor(directory), follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def unlink(self, directory: AttachmentDirectory, name: str) -> None:
        try:
            os.unlink(name, dir_fd=self._descriptor(directory))
        except FileNotFoundError:
            pass

    def publish(self, directory: AttachmentDirectory, temporary_name: str, target_name: str) -> None:
        descriptor = self._descriptor(directory)
        os.link(
            temporary_name,
            target_name,
            src_dir_fd=descriptor,
            dst_dir_fd=descriptor,
            follow_symlinks=False,
        )

    def secure_stream(self, descriptor: int) -> None:
        self._fchmod(descriptor, 0o600)

    @staticmethod
    def current_path(directory: AttachmentDirectory) -> Path:
        return directory.path

    @staticmethod
    def close_directory(directory: AttachmentDirectory) -> None:
        if directory.closed:
            return
        directory.closed = True
        if directory.posix_fd is not None:
            os.close(directory.posix_fd)
            directory.posix_fd = None


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_READ_ATTRIBUTES = 0x00000080
    _FILE_WRITE_ATTRIBUTES = 0x00000100
    _DELETE = 0x00010000
    _SYNCHRONIZE = 0x00100000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_DELETE = 0x00000004
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _FILE_OPEN = 1
    _FILE_CREATE = 2
    _FILE_NON_DIRECTORY_FILE = 0x00000040
    _FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    _OBJ_CASE_INSENSITIVE = 0x00000040
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
    _SE_FILE_OBJECT = 1
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _SDDL_REVISION_1 = 1
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER_CLASS = 1
    _ERROR_INSUFFICIENT_BUFFER = 122
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        ]

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [
            ("sid", wintypes.LPVOID),
            ("attributes", wintypes.DWORD),
        ]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("user", _SidAndAttributes)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    _kernel32.LocalFree.restype = wintypes.HLOCAL
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.SetFileInformationByHandle.restype = wintypes.BOOL

    _advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.ULONG),
    ]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.GetSecurityDescriptorDacl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    _advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    _advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD

    class _UnicodeString(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.USHORT),
            ("maximum_length", wintypes.USHORT),
            ("buffer", wintypes.LPWSTR),
        ]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.ULONG),
            ("root_directory", wintypes.HANDLE),
            ("object_name", ctypes.POINTER(_UnicodeString)),
            ("attributes", wintypes.ULONG),
            ("security_descriptor", wintypes.LPVOID),
            ("security_quality_of_service", wintypes.LPVOID),
        ]

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [
            ("status", wintypes.LPVOID),
            ("information", ctypes.c_size_t),
        ]

    class _FileDispositionInfo(ctypes.Structure):
        _fields_ = [("delete_file", ctypes.c_ubyte)]

    class _FileLinkInfo(ctypes.Structure):
        _fields_ = [
            ("replace_if_exists", ctypes.c_ubyte),
            ("root_directory", wintypes.HANDLE),
            ("file_name_length", wintypes.DWORD),
            ("file_name", wintypes.WCHAR * 1),
        ]

    _ntdll.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _ntdll.NtCreateFile.restype = wintypes.LONG
    _ntdll.NtSetInformationFile.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
    ]
    _ntdll.NtSetInformationFile.restype = wintypes.LONG
    _ntdll.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
    _ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG


def _windows_api_path(path: Path) -> str:
    absolute = os.path.abspath(os.fspath(path))
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    if absolute.startswith("\\\\?\\"):
        return absolute
    return "\\\\?\\" + absolute


def _windows_error() -> OSError:
    if os.name != "nt":
        return OSError("Windows attachment storage is unavailable")
    return ctypes.WinError(ctypes.get_last_error())


def _close_windows_handle(handle: int) -> None:
    if os.name == "nt" and not _kernel32.CloseHandle(wintypes.HANDLE(handle)):
        raise _windows_error()


def _open_pinned_windows_directory(path: Path) -> int:
    if os.name != "nt":
        raise OSError("Windows attachment storage is unavailable")
    handle = _kernel32.CreateFileW(
        _windows_api_path(path),
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    handle_value = int(handle) if handle is not None else 0
    if not handle_value or handle_value == _INVALID_HANDLE_VALUE:
        error = _windows_error()
        raise OSError(error.errno, error.strerror, str(path), error.winerror) from None
    info = _FileAttributeTagInfo()
    if not _kernel32.GetFileInformationByHandleEx(
        wintypes.HANDLE(handle_value),
        _FILE_ATTRIBUTE_TAG_INFO_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error = _windows_error()
        _kernel32.CloseHandle(wintypes.HANDLE(handle_value))
        raise error
    if info.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        _kernel32.CloseHandle(wintypes.HANDLE(handle_value))
        raise OSError("reparse points are not allowed in attachment paths")
    return handle_value


def _nt_open_relative(
    directory_handle: int,
    name: str,
    *,
    desired_access: int,
    disposition: int,
) -> int:
    if os.name != "nt":
        raise OSError("Windows attachment storage is unavailable")
    name_buffer = ctypes.create_unicode_buffer(name)
    name_length = len(name.encode("utf-16-le"))
    object_name = _UnicodeString(
        name_length,
        name_length + ctypes.sizeof(wintypes.WCHAR),
        ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        wintypes.HANDLE(directory_handle),
        ctypes.pointer(object_name),
        _OBJ_CASE_INSENSITIVE,
        None,
        None,
    )
    io_status = _IoStatusBlock()
    handle = wintypes.HANDLE()
    status = _ntdll.NtCreateFile(
        ctypes.byref(handle),
        desired_access,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        _FILE_ATTRIBUTE_NORMAL,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        disposition,
        _FILE_NON_DIRECTORY_FILE | _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
        0,
    )
    if status < 0:
        winerror = int(_ntdll.RtlNtStatusToDosError(status))
        raise ctypes.WinError(winerror)
    assert handle.value is not None
    return int(handle.value)


def _windows_directory_path(handle: int) -> Path:
    if os.name != "nt":
        raise OSError("Windows attachment storage is unavailable")
    size = 512
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = _kernel32.GetFinalPathNameByHandleW(
            wintypes.HANDLE(handle),
            buffer,
            size,
            0,
        )
        if not length:
            raise _windows_error()
        if length < size:
            value = buffer.value
            if value.startswith("\\\\?\\UNC\\"):
                value = "\\\\" + value[8:]
            elif value.startswith("\\\\?\\"):
                value = value[4:]
            return Path(value)
        size = length + 1


@lru_cache(maxsize=1)
def _current_windows_user_sid() -> str:
    if os.name != "nt":
        raise OSError("Windows attachment storage is unavailable")
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(
        _kernel32.GetCurrentProcess(),
        _TOKEN_QUERY,
        ctypes.byref(token),
    ):
        raise _windows_error()
    try:
        required = wintypes.DWORD()
        _advapi32.GetTokenInformation(
            token,
            _TOKEN_USER_CLASS,
            None,
            0,
            ctypes.byref(required),
        )
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER or not required.value:
            raise _windows_error()
        buffer = ctypes.create_string_buffer(required.value)
        if not _advapi32.GetTokenInformation(
            token,
            _TOKEN_USER_CLASS,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            raise _windows_error()
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        sid_text = wintypes.LPWSTR()
        if not _advapi32.ConvertSidToStringSidW(token_user.user.sid, ctypes.byref(sid_text)):
            raise _windows_error()
        try:
            return str(sid_text.value)
        finally:
            _kernel32.LocalFree(sid_text)
    finally:
        assert token.value is not None
        _close_windows_handle(int(token.value))


def _restrict_windows_path(path: Path, *, inherit: bool) -> None:
    """Protect a file or directory with an owner-and-SYSTEM-only DACL."""
    if os.name != "nt":
        raise OSError("Windows attachment storage is unavailable")
    security_descriptor = wintypes.LPVOID()
    descriptor_size = wintypes.ULONG()
    current_user_sid = _current_windows_user_sid()
    inheritance = "OICI" if inherit else ""
    if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        f"D:P(A;{inheritance};FA;;;{current_user_sid})(A;{inheritance};FA;;;SY)",
        _SDDL_REVISION_1,
        ctypes.byref(security_descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise _windows_error()
    try:
        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        dacl = wintypes.LPVOID()
        if not _advapi32.GetSecurityDescriptorDacl(
            security_descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ):
            raise _windows_error()
        if not dacl_present:
            raise OSError("owner-only Windows DACL is unavailable")
        result = _advapi32.SetNamedSecurityInfoW(
            _windows_api_path(path),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        if result:
            raise OSError(result, "failed to protect attachment path")
    finally:
        _kernel32.LocalFree(security_descriptor)


class WindowsAttachmentStorage:
    def open_directory(self, root: Path, components: tuple[str, ...]) -> AttachmentDirectory:
        if os.name != "nt":
            raise OSError("Windows attachment storage is unavailable")
        root.mkdir(parents=True, exist_ok=True)
        handles: list[int] = []
        paths: list[Path] = []
        current = root
        try:
            for component in (None, *components):
                if component is not None:
                    current /= component
                    current.mkdir(exist_ok=True)
                handle = _open_pinned_windows_directory(current)
                handles.append(handle)
                paths.append(current)
            for path in reversed(paths):
                _restrict_windows_path(path, inherit=True)
            return AttachmentDirectory(path=current, windows_handles=handles)
        except BaseException:
            for handle in reversed(handles):
                try:
                    _close_windows_handle(handle)
                except OSError:
                    pass
            raise

    @staticmethod
    def _handle(directory: AttachmentDirectory) -> int:
        if directory.closed or not directory.windows_handles:
            raise OSError("attachment directory is closed")
        return directory.windows_handles[-1]

    def open_file(self, directory: AttachmentDirectory, name: str, flags: int, mode: int) -> int:
        if os.name != "nt":
            raise OSError("Windows attachment storage is unavailable")
        del mode
        if not flags & os.O_CREAT or not flags & os.O_EXCL:
            raise OSError("Windows attachment files require exclusive creation")
        handle = _nt_open_relative(
            self._handle(directory),
            name,
            desired_access=_GENERIC_WRITE | _SYNCHRONIZE,
            disposition=_FILE_CREATE,
        )
        try:
            import msvcrt

            return msvcrt.open_osfhandle(handle, os.O_WRONLY | getattr(os, "O_BINARY", 0))
        except BaseException:
            try:
                _close_windows_handle(handle)
            except OSError:
                pass
            raise

    def entry_exists(self, directory: AttachmentDirectory, name: str) -> bool:
        if os.name != "nt":
            raise OSError("Windows attachment storage is unavailable")
        try:
            handle = _nt_open_relative(
                self._handle(directory),
                name,
                desired_access=_FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                disposition=_FILE_OPEN,
            )
        except FileNotFoundError:
            return False
        _close_windows_handle(handle)
        return True

    def unlink(self, directory: AttachmentDirectory, name: str) -> None:
        if os.name != "nt":
            raise OSError("Windows attachment storage is unavailable")
        try:
            handle = _nt_open_relative(
                self._handle(directory),
                name,
                desired_access=_DELETE | _SYNCHRONIZE,
                disposition=_FILE_OPEN,
            )
        except FileNotFoundError:
            return
        try:
            disposition = _FileDispositionInfo(True)
            if not _kernel32.SetFileInformationByHandle(
                wintypes.HANDLE(handle),
                4,
                ctypes.byref(disposition),
                ctypes.sizeof(disposition),
            ):
                raise _windows_error()
        finally:
            _close_windows_handle(handle)

    def publish(self, directory: AttachmentDirectory, temporary_name: str, target_name: str) -> None:
        if os.name != "nt":
            raise OSError("Windows attachment storage is unavailable")
        handle = _nt_open_relative(
            self._handle(directory),
            temporary_name,
            desired_access=_DELETE | _FILE_WRITE_ATTRIBUTES | _SYNCHRONIZE,
            disposition=_FILE_OPEN,
        )
        try:
            encoded_name = target_name.encode("utf-16-le")
            allocation_size = _FileLinkInfo.file_name.offset + len(encoded_name)
            buffer = ctypes.create_string_buffer(allocation_size)
            link = _FileLinkInfo.from_buffer(buffer)
            link.replace_if_exists = False
            link.root_directory = wintypes.HANDLE(self._handle(directory))
            link.file_name_length = len(encoded_name)
            ctypes.memmove(
                ctypes.addressof(buffer) + _FileLinkInfo.file_name.offset,
                encoded_name,
                len(encoded_name),
            )
            io_status = _IoStatusBlock()
            status = _ntdll.NtSetInformationFile(
                wintypes.HANDLE(handle),
                ctypes.byref(io_status),
                buffer,
                allocation_size,
                11,
            )
            if status < 0:
                winerror = int(_ntdll.RtlNtStatusToDosError(status))
                raise ctypes.WinError(winerror)
        finally:
            _close_windows_handle(handle)

    @staticmethod
    def secure_stream(descriptor: int) -> None:
        del descriptor

    def current_path(self, directory: AttachmentDirectory) -> Path:
        current = _windows_directory_path(self._handle(directory))
        directory.path = current
        return current

    @staticmethod
    def close_directory(directory: AttachmentDirectory) -> None:
        if directory.closed:
            return
        directory.closed = True
        failures: list[OSError] = []
        for handle in reversed(directory.windows_handles):
            try:
                _close_windows_handle(handle)
            except OSError as error:
                failures.append(error)
        directory.windows_handles.clear()
        if failures:
            raise failures[0]


def attachment_storage() -> AttachmentStorage:
    if os.name == "nt":
        return WindowsAttachmentStorage()
    if os.name == "posix":
        return PosixAttachmentStorage()
    raise OSError(f"unsupported attachment storage platform: {os.name}")
