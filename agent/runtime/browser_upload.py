"""Bounded, identity-checked local files for an explicitly authorized upload.

Only metadata leaves this module in receipts. File bytes are private transport
payloads, never model arguments, trace events or exception messages.
"""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
import base64
import json
import mimetypes
import os
from pathlib import Path
import stat

MAX_FILES = 10
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 256 * 1024


def identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@dataclass(frozen=True)
class UploadFile:
    path: Path
    original: Path
    fingerprint: tuple[int, ...]
    name: str
    size: int
    mime: str

    def metadata(self) -> dict:
        return {"name": self.name, "size": self.size, "type": self.mime}

    def unchanged(self) -> bool:
        return self.original.resolve() == self.path and identity(self.path.stat()) == self.fingerprint


def inspect_files(paths: list[str], workspace: Path) -> list[UploadFile]:
    if not isinstance(paths, list) or len(paths) > MAX_FILES:
        raise ValueError("paths must be an array of at most 10 files")
    result = []
    total = 0
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
            raise ValueError("Each path must be a nonempty local file path")
        original = Path(raw).expanduser()
        if not original.is_absolute():
            original = workspace / original
        path = original.resolve(strict=True)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Upload accepts ordinary files only, not directories or special files")
        total += info.st_size
        if info.st_size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("Upload exceeds the 32 MiB per-file or 64 MiB total limit")
        if len(path.name) > 255 or any(ord(c) < 32 for c in path.name):
            raise ValueError("Unsupported upload filename")
        result.append(UploadFile(path, original, identity(info), path.name, info.st_size,
                                 mimetypes.guess_type(path.name)[0] or ""))
    return result


def read_files(files: list[UploadFile]) -> list[bytes]:
    """Open no-follow/nonblocking, check identity before and after a bounded read."""
    contents = []
    for file in files:
        if not file.unchanged():
            raise ValueError("Upload file changed since authorization; authorize the new file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(file.path, flags), "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or identity(info) != file.fingerprint:
                raise ValueError("Upload file identity changed before reading")
            data = handle.read(file.size + 1)
            if len(data) != file.size or identity(os.fstat(handle.fileno())) != file.fingerprint or not file.unchanged():
                raise ValueError("Upload file changed during reading")
            contents.append(data)
    return contents


async def upload(backend, selector: str, files: list[UploadFile], *, tab_id: str, url: str, frame_ref: str = "") -> str:
    """A single public write; never retry a submitted commit."""
    from .browser_control_transport import BrowserUnsupportedOperation
    from .extension_browser_backend import _origin_string

    operations = ("upload_prepare", "upload_chunk", "upload_commit", "upload_abort")
    if any(backend._operation_support(op) is not True for op in operations):
        return json.dumps(BrowserUnsupportedOperation("browser_upload").result())
    if not selector.startswith("ref:") or len(selector) <= 4:
        return json.dumps({"status": "invalid_arguments", "dispatch_state": "not_dispatched",
                           "message": "Use a fresh file input ref from browser_snapshot"})
    transfer_id = None
    committed = False
    expected = _origin_string(url)
    generation = backend.transport.generation

    async def request(operation, args):
        if backend.transport.generation != generation:
            raise ConnectionError("Browser connection changed during upload")
        return await backend.transport.request(operation, tab_id=backend._bound(tab_id),
                                               args={**args, "expectedOrigin": expected})

    try:
        await backend.assert_origin(tab_id=tab_id, expected_url=url)
        result = await request("upload_prepare", {"ref": selector[4:], "frame_ref": frame_ref,
                               "files": [file.metadata() for file in files]})
        if result.get("status") != "prepared":
            return json.dumps(result)
        transfer_id = result.get("transferId")
        if not isinstance(transfer_id, str) or not transfer_id or len(transfer_id) > 128:
            raise ValueError("Invalid upload transfer receipt")
        # Capability and the exact DOM target have been verified before bytes are read.
        contents = await asyncio.to_thread(read_files, files)
        for index, data in enumerate(contents):
            for offset in range(0, len(data), CHUNK_BYTES):
                result = await request("upload_chunk", {"transferId": transfer_id, "index": index,
                    "offset": offset, "data": base64.b64encode(data[offset:offset + CHUNK_BYTES]).decode("ascii")})
                if result.get("status") != "buffered":
                    return json.dumps(result)
        if not all(file.unchanged() for file in files):
            raise ValueError("Upload file changed before commit")
        committed = True
        result = await request("upload_commit", {"transferId": transfer_id})
        return json.dumps(backend._observe_result(tab_id, result), ensure_ascii=False)
    except (OSError, ValueError, RuntimeError):
        # Transport or page exceptions must never echo a chunk or file content.
        return json.dumps({"status": "unknown_outcome" if committed else "error",
            "dispatch_state": "unknown" if committed else "not_dispatched", "repeat_input": False,
            "message": "Upload commit may have executed; observe selected files before continuing" if committed else
                       "Upload preparation/transfer failed; no file selection was dispatched. Check file identity, connection and target."})
    finally:
        if transfer_id and backend.transport.connected and backend.transport.generation == generation:
            try:
                async with asyncio.timeout(2):
                    await request("upload_abort", {"transferId": transfer_id})
            except (OSError, ValueError, RuntimeError):
                pass
