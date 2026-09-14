"""Windows session media using the helper's shared, handle-pinned native backend.

No screenshot paths or client-supplied identities cross this interface. Calls are
synchronous so cancellation cannot abandon a worker after it publishes a file.
The native API owns each reservation until it returns an exact cleanup receipt.
"""
from __future__ import annotations

import base64
import ctypes as C
import hashlib
import sys
import uuid
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from agent.runtime.appshot_media import AppshotMediaError


class FileProof(C.Structure):
    _fields_ = [
        ("volume", C.c_uint64), ("file_id", C.c_ubyte * 16), ("size", C.c_uint64),
        ("modified", C.c_uint64), ("changed", C.c_uint64), ("links", C.c_uint32),
        ("owner_sid", C.c_char * 192), ("sha256", C.c_char * 65),
    ]


class NativeBytes(C.Structure):
    _fields_ = [("data", C.c_void_p), ("size", C.c_size_t), ("error", C.c_int32)]


def check(code):
    if code:
        name = {10: "authority", 12: "exists", 13: "limit", 14: "missing"}.get(code, "unavailable")
        raise AppshotMediaError("session_media_" + name)


@lru_cache(maxsize=4)
def load_library(path: str):
    if sys.platform != "win32":
        raise AppshotMediaError("session_media_unsupported")
    try:
        # Absolute, installed-helper sibling only; no current-directory/PATH DLL
        # search. LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | DEFAULT_DIRS.
        library = C.CDLL(path, winmode=0x1100)
        signatures = {
            "as_media_storage_abi": (C.c_uint32, []),
            "as_private_media_open": (C.c_void_p, [C.c_wchar_p, C.c_int32, C.c_int32, C.POINTER(C.c_int32)]),
            "as_private_directory_close": (None, [C.c_void_p]),
            "as_private_publish": (C.c_int32, [C.c_void_p, C.c_char_p, C.c_char_p, C.c_void_p, C.c_size_t, C.POINTER(FileProof)]),
            "as_private_read": (NativeBytes, [C.c_void_p, C.c_char_p, C.c_uint32, C.POINTER(FileProof)]),
            "as_bytes_free": (None, [NativeBytes]),
            "as_private_remove": (C.c_int32, [C.c_void_p, C.c_char_p, C.POINTER(FileProof)]),
            "as_private_media_rename": (C.c_int32, [C.c_void_p, C.c_wchar_p]),
            "as_private_media_delete": (C.c_int32, [C.c_void_p]),
        }
        for name, (result, arguments) in signatures.items():
            function = getattr(library, name)
            function.restype, function.argtypes = result, arguments
        if library.as_media_storage_abi() != 1:
            raise AppshotMediaError("session_media_native_version")
        return library
    except (OSError, AttributeError) as exc:
        raise AppshotMediaError("session_media_native_unavailable") from exc


def native_library():
    from agent.runtime.appshot_windows_config import resolve_windows_appshot_helper

    path = resolve_windows_appshot_helper().parent / "AstraWindowsNative.dll"
    try:
        if path.resolve(strict=True) != path or not path.is_file():
            raise AppshotMediaError("session_media_native_unavailable")
    except OSError as exc:
        raise AppshotMediaError("session_media_native_unavailable") from exc
    return load_library(str(path))


class WindowsMediaStore:
    def __init__(self, path):
        # Do not resolve() and silently follow a junction before native checks.
        self.path = Path(path).absolute()
        self.library = native_library()
        self.owned: dict[str, FileProof] = {}

    @contextmanager
    def directory(self, *, create=False, mutate=False, missing_ok=False):
        error = C.c_int32()
        pointer = self.library.as_private_media_open(str(self.path), int(create), int(mutate), C.byref(error))
        if not pointer and error.value == 14 and missing_ok:
            yield None
            return
        check(error.value)
        if not pointer:
            raise AppshotMediaError("session_media_unavailable")
        try:
            yield pointer
        finally:
            self.library.as_private_directory_close(pointer)

    def _read(self, pointer, name):
        proof = FileProof()
        value = self.library.as_private_read(pointer, name.encode("ascii"), 10485760, C.byref(proof))
        try:
            check(value.error)
            if not value.data or not 0 < value.size <= 10485760:
                raise AppshotMediaError("session_media_limit")
            raw = C.string_at(value.data, value.size)
            if hashlib.sha256(raw).hexdigest().encode("ascii") != proof.sha256:
                raise AppshotMediaError("session_media_tampered")
            return raw
        finally:
            self.library.as_bytes_free(value)

    def put(self, raw, width, height):
        from agent.cli.appshots import validate_png

        validate_png(raw, width, height)
        key = uuid.uuid4().hex
        name = (key + ".png").encode("ascii")
        ref = dict(media_id=key, sha256=hashlib.sha256(raw).hexdigest(), width=width, height=height)
        proof = FileProof()
        with self.directory(create=True) as pointer:
            check(self.library.as_private_publish(pointer, (key + ".tmp").encode("ascii"), name, raw, len(raw), C.byref(proof)))
            self.owned[key] = proof
            try:
                if proof.sha256.decode("ascii") != ref["sha256"] or self._read(pointer, key + ".png") != raw:
                    raise AppshotMediaError("session_media_tampered")
            except BaseException:
                # A replaced/tampered file is not ours to remove. Keep its proof
                # if cleanup failed, allowing a later exact retry, never force.
                if self.library.as_private_remove(pointer, name, C.byref(proof)) == 0:
                    self.owned.pop(key, None)
                raise
        return ref

    def hydrate(self, ref):
        from agent.cli.appshots import AppshotValidationError, validate_png
        from agent.runtime.appshot_media import AppshotMediaStore

        AppshotMediaStore.validate_reference(ref)
        with self.directory() as pointer:
            raw = self._read(pointer, ref["media_id"] + ".png")
        if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise AppshotMediaError("session_media_tampered")
        try:
            validate_png(raw, ref["width"], ref["height"])
        except AppshotValidationError as exc:
            raise AppshotMediaError("session_media_tampered") from exc
        return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(raw).decode("ascii")}}

    def rollback(self, references):
        from agent.runtime.appshot_media import AppshotMediaStore

        for ref in references:
            AppshotMediaStore.validate_reference(ref)
        with self.directory(missing_ok=True) as pointer:
            if pointer is None:
                return
            for ref in references:
                key = ref["media_id"]
                proof = self.owned.get(key)
                if proof is not None:
                    check(self.library.as_private_remove(pointer, (key + ".png").encode("ascii"), C.byref(proof)))
                    self.owned.pop(key, None)

    def delete(self):
        with self.directory(mutate=True, missing_ok=True) as pointer:
            if pointer is not None:
                check(self.library.as_private_media_delete(pointer))
        self.owned.clear()

    def rename_to(self, destination):
        with self.directory(mutate=True, missing_ok=True) as pointer:
            if pointer is not None:
                check(self.library.as_private_media_rename(pointer, str(Path(destination).absolute())))
