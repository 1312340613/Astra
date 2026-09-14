"""Authenticated lifecycle for this checkout's shared macOS embedding workers."""

from __future__ import annotations

import ctypes
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

from .common import LauncherError
from .installation import Installation

MODULE = "agent.runtime.context_index.embedding_worker"
TIMEOUT = 30.0


def _decode_argv(raw: bytes) -> list[str]:
    count = int.from_bytes(raw[:4], byteorder="little", signed=True)
    if not 0 < count <= 1024:
        raise LauncherError("Invalid native worker argument count.")
    position = raw.index(b"\x00", 4) + 1  # saved executable path precedes argv
    while position < len(raw) and raw[position] == 0:
        position += 1
    return [part.decode("utf-8") for part in raw[position:].split(b"\x00", count)[:count]]


def _argv(pid: int) -> list[str]:
    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN / KERN_PROCARGS2
    size = ctypes.c_size_t()
    libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                           ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    libc.sysctl.restype = ctypes.c_int
    try:
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) or not 0 < size.value <= 1024 * 1024:
            return []  # Process exit or permission loss: never guess argv from ps text.
        buffer = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0):
            return []
        return _decode_argv(buffer.raw[:size.value])
    except (ValueError, UnicodeError):
        return []


def _candidates() -> list[int]:
    try:
        result = subprocess.run(["ps", "-axo", "pid=,uid=,command="], capture_output=True,
                                text=True, check=False, timeout=10)
        if result.returncode:
            raise LauncherError("Cannot inspect embedding worker processes.")
        return [int(parts[0]) for line in result.stdout.splitlines()
                if len(parts := line.strip().split(maxsplit=2)) == 3
                and parts[1] == str(os.getuid()) and MODULE in parts[2]]
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise LauncherError("Cannot inspect embedding worker processes.") from exc


def _key(directory: str) -> str:
    return "embedding-" + hashlib.sha256(directory.encode()).hexdigest()[:16]


def _state(directory: Path, pid: int) -> dict | None:
    for index, path in enumerate(directory.glob("*.json")):
        if index >= 64:
            raise LauncherError("Too many embedding runtime records; inspect this directory.")
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or info.st_size > 4096):
                    continue
                value = json.loads(stream.read(4097))
            if (isinstance(value, dict) and value.get("protocol") == 1 and value.get("pid") == pid
                    and isinstance(value.get("identity"), str) and value["identity"].startswith("mlx:")
                    and type(value.get("port")) is int and 0 < value["port"] < 65536
                    and isinstance(value.get("token"), str) and re.fullmatch(r"[0-9a-f]{64}", value["token"])):
                return value
        except (OSError, ValueError):
            continue
    return None


def _request(state: dict, *, stop: bool = False) -> dict:
    connection = http.client.HTTPConnection("127.0.0.1", state["port"], timeout=2)
    try:
        connection.request("POST" if stop else "GET", "/stop" if stop else "/health",
                           body=b"{}" if stop else None,
                           headers={"Authorization": "Bearer " + state["token"], "Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read(4097)
        if response.status not in ({200, 409} if stop else {200}) or len(raw) > 4096:
            raise ValueError
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get("protocol") != 1
                or value.get("identity") != state["identity"] or (not stop and value.get("pid") != state["pid"])):
            raise ValueError
        return value
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise LauncherError("Embedding worker did not answer its authenticated local control endpoint.") from exc
    finally:
        connection.close()


class EmbeddingServices:
    def __init__(self, install: Installation):
        self.install = install

    def _workers(self) -> list[tuple[int, Path]]:
        workers = []
        for pid in _candidates():
            args = _argv(pid)
            if (len(args) == 5 and args[1:4] == ["-m", MODULE, "--directory"]
                    and Path(args[0]).parent == self.install.python.parent
                    and re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(args[0]).name)
                    and Path(args[4]).is_absolute()):
                workers.append((pid, Path(args[4])))
        return workers

    def discover(self) -> list[dict]:
        entries = []
        for pid, directory in self._workers():
            if any(entry["directory"] == str(directory) for entry in entries):
                raise LauncherError("Embedding worker election is in progress; retry when it settles.")
            entry = {"id": _key(str(directory)), "adapter": "embedding", "directory": str(directory), "pids": [pid]}
            self.validate(entry)
            entries.append(entry)
        return entries

    def validate(self, entry: dict) -> None:
        directory = entry.get("directory")
        if (not isinstance(directory, str) or len(directory) > 4096 or "\x00" in directory
                or not Path(directory).is_absolute() or entry.get("id") != _key(directory)
                or Path(directory).resolve() != Path(directory) or entry.get("adapter") != "embedding"):
            raise LauncherError("Invalid embedding service recovery identity.")

    def stop(self, entry: dict) -> None:
        self.validate(entry)
        directory = Path(entry["directory"])
        deadline = time.monotonic() + TIMEOUT
        requested: set[int] = set()
        while True:
            workers = [(pid, path) for pid, path in self._workers() if path == directory]
            if not workers:
                return
            for pid, _ in workers:
                if pid in requested:
                    continue
                state = _state(directory, pid)
                if state:
                    health = _request(state)
                    if not health.get("busy"):
                        if _request(state, stop=True).get("state") == "stopping":
                            requested.add(pid)
            if time.monotonic() >= deadline:
                raise LauncherError("Embedding worker is still busy or stopping; no source files were changed.")
            time.sleep(0.1)

    def start(self, entry: dict) -> None:
        self.validate(entry)
        directory = Path(entry["directory"])
        workers = [(pid, path) for pid, path in self._workers() if path == directory]
        child = None
        if not workers:
            try:
                child = subprocess.Popen([str(self.install.python), "-m", MODULE, "--directory", str(directory)],
                                         cwd=self.install.root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc:
                raise LauncherError("Cannot restart the embedding worker.") from exc
        deadline = time.monotonic() + TIMEOUT
        while True:
            for pid, path in self._workers():
                if path != directory:
                    continue
                state = _state(directory, pid)
                if state and _request(state).get("state") in {"loading", "ready"}:
                    return  # Loading is asynchronous; do not block updates on a model download.
            if (child is not None and child.poll() is not None) or time.monotonic() >= deadline:
                raise LauncherError("Embedding worker did not resume; inspect its model/runtime configuration.")
            time.sleep(0.1)
