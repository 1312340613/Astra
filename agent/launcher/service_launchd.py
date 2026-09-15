"""Maintenance adapter for Astra's installed, per-user macOS LaunchAgents."""

from __future__ import annotations

import hashlib
import os
import plistlib
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

from .common import LauncherError
from .installation import Installation

PYTHON_JOBS = {
    "com.astra.activity-browser-bridge": ["-m", "agent.runtime.activity_recorder.browser_bridge"],
    "com.astra.activity-summarizer": ["-m", "agent.runtime.activity_recorder.summarizer"],
    "com.astra.activity-sync": ["-m", "agent.cli.main", "activity", "sync", "--scheduled"],
}
NATIVE_JOB = "com.astra.activity-recorder"
TIMEOUT = 20.0


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LauncherError("Cannot contact the macOS service manager.") from exc


def _missing(result: subprocess.CompletedProcess[str]) -> bool:
    return result.returncode == 113 or any(text in result.stderr.casefold() for text in
                                         ("could not find service", "no such process", "service not found"))


def _read(path: Path) -> tuple[dict, str]:
    if sys.platform == "win32":
        raise LauncherError("LaunchAgent maintenance requires macOS.")
    # Do not follow a replaced definition into a different user's configuration.
    if path.is_symlink():
        raise LauncherError("An Astra service definition is symlinked; inspect it before maintenance.")
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022 or info.st_size > 64 * 1024):
                raise LauncherError("An Astra service definition has unsafe ownership, permissions or size.")
            raw = stream.read(64 * 1024 + 1)
        data = plistlib.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        return data, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise LauncherError("Cannot read an Astra service definition; no configuration was replaced.") from exc


class LaunchdServices:
    install: Installation
    folder: Path
    domain: str

    def __init__(self, install: Installation):
        if sys.platform == "win32":
            raise LauncherError("LaunchAgent maintenance requires macOS.")
        self.install = install
        self.folder = Path.home() / "Library/LaunchAgents"
        self.domain = f"gui/{os.getuid()}"

    def _path(self, identifier: str) -> Path:
        if identifier not in {*PYTHON_JOBS, NATIVE_JOB}:
            raise LauncherError("Unknown LaunchAgent in the service recovery journal.")
        return self.folder / (identifier + ".plist")

    def _owned(self, identifier: str, data: dict) -> bool:
        args = data.get("ProgramArguments")
        if (data.get("Label") != identifier or not isinstance(args, list) or not args
                or not all(isinstance(arg, str) and not any(c in arg for c in "\x00\n\r") for arg in args)
                or data.get("Program", args[0]) != args[0]):
            return False
        if identifier == NATIVE_JOB:
            stable = Path.home() / "Library/Application Support/Astra/bin/AstraActivityRecorder"
            return args == [str(stable)] and not stable.is_symlink()
        prefix = PYTHON_JOBS[identifier]
        cwd = data.get("WorkingDirectory")
        executable = Path(args[0])
        return (args[1:1 + len(prefix)] == prefix and Path(args[0]).is_absolute()
                and executable.absolute().parent == self.install.python.absolute().parent
                and re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable.name) is not None
                and isinstance(cwd, str) and Path(cwd).resolve() == self.install.root)

    def _query(self, entry: dict, data: dict) -> dict | None:
        identifier = entry["id"]
        result = _run("print", f"{self.domain}/{identifier}")
        if result.returncode:
            if _missing(result):
                return None
            raise LauncherError(f"Cannot inspect {identifier}; service state is unknown.")
        # Match the live job as well as the on-disk file: launchd may still have
        # a different installation's previously loaded definition.
        fields = dict(re.findall(r"^\t(path|program|state|pid) = (.+)$", result.stdout, re.M))
        match = re.search(r"^\targuments = \{\n(.*?)^\t\}", result.stdout, re.M | re.S)
        args = [line.strip() for line in match[1].splitlines()] if match else []
        if (fields.get("path") != str(self._path(identifier))
                or fields.get("program") != data["ProgramArguments"][0]
                or args != data["ProgramArguments"]):
            raise LauncherError(f"Loaded {identifier} differs from its saved configuration; inspect it before updating.")
        pid = fields.get("pid", "0")
        if not pid.isdigit():
            raise LauncherError(f"Cannot verify the process identity of {identifier}.")
        return {"running": fields.get("state") == "running", "pid": int(pid)}

    def discover(self) -> list[dict]:
        definitions = {}
        foreign = False
        for identifier in PYTHON_JOBS:
            path = self._path(identifier)
            if not path.exists():
                continue
            data, digest = _read(path)
            if self._owned(identifier, data):
                definitions[identifier] = (data, digest)
            else:
                foreign = True
        # The native recorder is per user, outside Git. Associate it only when
        # this checkout is the sole owner of the installed activity pipeline.
        native = self._path(NATIVE_JOB)
        if definitions and not foreign and native.exists():
            data, digest = _read(native)
            if self._owned(NATIVE_JOB, data):
                definitions = {NATIVE_JOB: (data, digest), **definitions}
        entries = []
        for identifier, (data, digest) in definitions.items():
            entry = {"id": identifier, "adapter": "launchd", "digest": digest,
                     "path": str(self._path(identifier)), "pids": []}
            observed = self._query(entry, data)
            if observed is not None:
                entry["pids"] = [observed["pid"]] if observed["pid"] else []
                entries.append(entry)
        return entries

    def validate(self, entry: dict) -> None:
        if (entry.get("adapter") != "launchd" or entry.get("path") != str(self._path(entry["id"]))
                or not isinstance(entry.get("digest"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["digest"])
                or not isinstance(entry.get("pids"), list)
                or any(type(pid) is not int or pid <= 0 for pid in entry["pids"])):
            raise LauncherError("Invalid LaunchAgent recovery identity.")

    def _definition(self, entry: dict) -> dict:
        self.validate(entry)
        data, digest = _read(self._path(entry["id"]))
        if digest != entry["digest"] or not self._owned(entry["id"], data):
            raise LauncherError(f"Configuration for {entry['id']} changed during maintenance; it was preserved.")
        return data

    def stop(self, entry: dict) -> None:
        data = self._definition(entry)
        if self._query(entry, data) is None:
            return
        result = _run("bootout", f"{self.domain}/{entry['id']}")
        if result.returncode and not _missing(result):
            raise LauncherError(f"Could not pause {entry['id']}; no source files were changed.")
        deadline = time.monotonic() + TIMEOUT
        while self._query(entry, data) is not None:
            if time.monotonic() >= deadline:
                raise LauncherError(f"{entry['id']} is still stopping; retry maintenance after it exits.")
            time.sleep(0.1)

    def start(self, entry: dict) -> None:
        data = self._definition(entry)
        if self._query(entry, data) is None:
            result = _run("bootstrap", self.domain, str(self._path(entry["id"])))
            if result.returncode:
                raise LauncherError(f"Could not restore {entry['id']}.")
        deadline = time.monotonic() + TIMEOUT
        while True:
            observed = self._query(entry, data)
            if observed is not None and (not data.get("KeepAlive") or observed["running"]):
                return  # Periodic jobs need a restored schedule, not a permanent PID.
            if time.monotonic() >= deadline:
                raise LauncherError(f"{entry['id']} did not return to its enabled state.")
            time.sleep(0.1)
