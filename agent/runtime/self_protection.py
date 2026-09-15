"""Best-effort host termination checks; not an isolation boundary for arbitrary code.

Inspect commands, never execute lookups supplied by the model. Normal commands
do not inspect the process table. Internal child cleanup bypasses this surface.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import logging
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

from .process_env import hidden_process_creationflags

logger = logging.getLogger(__name__)
_TERMINATION = re.compile(r"\b(?:kill|killpg|pkill|killall|taskkill|stop-process)\b", re.I)


class SelfProtectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    ppid: int
    pgid: int
    name: str
    command: str
    started: str


def process_snapshot() -> list[ProcessIdentity]:
    if sys.platform == "win32":
        script = (
            "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
            "@(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,"
            "Name,CommandLine,CreationDate) | ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", timeout=5,
            creationflags=hidden_process_creationflags(), check=True,
        )
        data = json.loads(result.stdout or "[]")
        if isinstance(data, dict):
            data = [data]
        return [ProcessIdentity(int(row["ProcessId"]), int(row["ParentProcessId"]), 0,
                                str(row.get("Name") or ""), str(row.get("CommandLine") or ""),
                                str(row.get("CreationDate") or "")) for row in data]
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,lstart=,comm=,args="],
        capture_output=True, text=True, timeout=3, check=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 9)
        if len(parts) == 10:
            rows.append(ProcessIdentity(int(parts[0]), int(parts[1]), int(parts[2]),
                                        parts[8], parts[9], " ".join(parts[3:8])))
    return rows


def owned_service_pids() -> list[int]:
    from agent.launcher.installation import discover
    from agent.launcher.services import adapter_for
    return [pid for entry in adapter_for(discover()).discover()
            for pid in entry.get("pids", []) if type(pid) is int and pid > 1]


class HostProcessGuard:
    def __init__(self, *, owner_pid: int | None = None, parent_pids: list[int] | None = None,
                 snapshot: Callable[[], list[ProcessIdentity]] = process_snapshot,
                 service_pids: Callable[[], list[int]] = owned_service_pids):
        self.owner_pid = owner_pid or os.getpid()
        self.parent_pids = parent_pids if parent_pids is not None else [
            int(value) for key in ("ASTRA_TUI_PID", "ASTRA_LAUNCHER_PID")
            if (value := os.getenv(key, "")).isdigit() and int(value) > 1
        ]
        self.snapshot = snapshot
        self.service_pids = service_pids
        self._parents: dict[int, str] = {}
        self._parents_pinned = False

    def _protected(self) -> list[ProcessIdentity]:
        try:
            rows = {row.pid: row for row in self.snapshot()}
        except (OSError, ValueError, subprocess.SubprocessError):
            logger.warning("Self-protection process lookup unavailable; checking current PID only")
            rows = {}
        owner = rows.get(self.owner_pid) or ProcessIdentity(self.owner_pid, 0, 0, "", "", "")
        if not self._parents_pinned and self.owner_pid in rows:
            ancestor = owner.ppid
            seen = set()
            while ancestor in rows and ancestor > 1 and ancestor not in seen:
                seen.add(ancestor)
                if ancestor in self.parent_pids:
                    self._parents[ancestor] = rows[ancestor].started
                ancestor = rows[ancestor].ppid
            self._parents_pinned = True
        protected = [owner, *(row for pid, started in self._parents.items()
                              if (row := rows.get(pid)) is not None and row.started == started)]
        try:
            services = self.service_pids()
        except Exception:
            logger.warning("Self-protection companion ownership unavailable", exc_info=False)
            services = []
        protected.extend(rows[pid] for pid in services if pid in rows)
        return protected

    @staticmethod
    def _deny() -> None:
        logger.warning("Blocked tool attempt to terminate an Astra runtime process")
        raise SelfProtectionError(
            "Astra self-protection: this command would stop the hosting runtime. "
            "Use /restart for a controlled backend restart, or stop Astra from the user interface."
        )

    def check_pid(self, pid: int, rows: list[ProcessIdentity], *, group: bool = False) -> None:
        if group or pid <= 0:
            matches = pid in {-1, 0} or any(row.pgid > 0 and row.pgid == abs(pid) for row in rows)
        else:
            matches = any(row.pid == pid for row in rows)
        if matches:
            self._deny()

    @staticmethod
    def _matches(pattern: str, rows: list[ProcessIdentity], *, full: bool = False,
                 wildcard: bool = False) -> bool:
        for row in rows:
            value = row.command if full else row.name.replace("\\", "/").rsplit("/", 1)[-1]
            if wildcard:
                if fnmatch.fnmatch(value.lower().removesuffix(".exe"), pattern.lower().removesuffix(".exe")):
                    return True
            else:
                try:
                    if re.search(pattern, value):
                        return True
                except re.error:
                    continue
        return False

    def check_shell(self, command: str, *, foreign_namespace: bool = False) -> None:
        if not _TERMINATION.search(command):
            return
        self._shell(command, self._protected(), 0, foreign_namespace)

    def _shell(self, command: str, rows: list[ProcessIdentity], depth: int, foreign: bool = False) -> None:
        if depth > 8:
            return
        # Substitute only literal assignments and recognized process lookups.
        # This intentionally does not interpret arbitrary shell code.
        variables = dict(re.findall(r"(?:^|[;\s])([A-Za-z_]\w*)=['\"]?(-?\d+)['\"]?(?=\s|;|$)", command))
        command = re.sub(r"\$\{?(\w+)\}?", lambda m: variables.get(m[1], m[0]), command)

        def lookup(match: re.Match) -> str:
            text = next(value for value in match.groups() if value is not None)
            try:
                args = shlex.split(text)
            except ValueError:
                return match[0]
            if not args or args[0] not in {"pgrep", "pidof"}:
                return match[0]
            patterns = [arg for arg in args[1:] if not arg.startswith("-")]
            full = any("f" in arg for arg in args[1:] if arg.startswith("-"))
            return " ".join(str(row.pid) for row in rows
                            if any(self._matches(pattern, [row], full=full) for pattern in patterns)) or "999999999"

        command = re.sub(r"\$\(([^()]*)\)|`([^`]*)`", lookup, command)
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
            lexer.whitespace = " \t\r"
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            return
        segment: list[str] = []
        previous: list[str] = []
        for token in [*tokens, ";"]:
            if token and all(char in ";&|\n" for char in token):
                self._segment(segment, previous, rows, depth, foreign)
                previous, segment = segment, []
            else:
                segment.append(token)

    def _segment(self, tokens: list[str], previous: list[str], rows: list[ProcessIdentity], depth: int,
                 foreign: bool = False) -> None:
        if depth > 8:
            return
        tokens = list(tokens)
        while tokens and ("=" in tokens[0] or tokens[0] in {"env", "command", "exec", "sudo", "nohup"}):
            tokens.pop(0)
        if not tokens:
            return
        name = tokens.pop(0).replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        if name in {"sh", "bash", "zsh", "dash", "powershell", "pwsh", "cmd"}:
            for index, token in enumerate(tokens[:-1]):
                if token.lower() in {"-c", "-lc", "-command", "/c"}:
                    self._shell(tokens[index + 1], rows, depth + 1,
                                foreign and name not in {"powershell", "pwsh", "cmd"})
            return
        if name.startswith("python") and "-c" in tokens:
            if foreign:
                return
            index = tokens.index("-c")
            if index + 1 < len(tokens):
                self._python(tokens[index + 1], rows)
            return
        if name == "xargs":
            # Literal PID producers are common during cleanup; other pipelines
            # still get their directly specified xargs command checked.
            pids = re.findall(r"(?<!\d)\d+(?!\d)", " ".join(previous[1:])) if previous[:1] in (["echo"], ["printf"]) else []
            self._segment([*tokens, *pids], [], rows, depth + 1, foreign)
            return
        if foreign and name in {"kill", "pkill", "killall"}:
            # WSL numeric PIDs and process names do not identify Windows hosts.
            # Windows interop executables (taskkill/powershell) still do.
            return
        if name == "kill":
            if any(token in {"-0", "-s0"} for token in tokens) or tokens[:2] in (["-s", "0"], ["-n", "0"]):
                return
            if tokens and tokens[0] != "--" and tokens[0].startswith("-"):
                flag = tokens.pop(0)
                if flag in {"-s", "-n"} and tokens:
                    tokens.pop(0)
            for token in tokens:
                if re.fullmatch(r"-?\d+", token):
                    self.check_pid(int(token), rows)
        elif name in {"pkill", "killall"}:
            if "-0" in tokens:
                return
            full = name == "pkill" and any("f" in token for token in tokens if token.startswith("-"))
            if any(self._matches(token, rows, full=full, wildcard=name == "killall")
                   for token in tokens if not token.startswith("-")):
                self._deny()
        elif name in {"taskkill", "stop-process"}:
            for index, token in enumerate(tokens):
                if token.lower() in {"/pid", "-id"} and index + 1 < len(tokens):
                    for value in tokens[index + 1].split(","):
                        if value.isdigit():
                            self.check_pid(int(value), rows)
                if token.lower() in {"/im", "-name"} and index + 1 < len(tokens):
                    if self._matches(tokens[index + 1], rows, wildcard=True):
                        self._deny()

    def check_python(self, code: str) -> None:
        if _TERMINATION.search(code):
            self._python(code, self._protected())

    def _python(self, code: str, rows: list[ProcessIdentity]) -> None:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return
        aliases = {"os": "os"}
        constants: dict[str, int] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    if item.name == "os":
                        aliases[item.asname or item.name] = "os"
            elif isinstance(node, ast.ImportFrom) and node.module == "os":
                for item in node.names:
                    aliases[item.asname or item.name] = "os." + item.name
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and type(node.value.value) is int:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            fn = node.func
            name = aliases.get(fn.id, "") if isinstance(fn, ast.Name) else (
                aliases.get(fn.value.id, "") + "." + fn.attr
                if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) else ""
            )
            if name not in {"os.kill", "os.killpg"}:
                continue
            if isinstance(node.args[1], ast.Constant) and node.args[1].value == 0:
                continue
            target = node.args[0]
            pid = constants.get(target.id) if isinstance(target, ast.Name) else (
                target.value if isinstance(target, ast.Constant) and type(target.value) is int else None
            )
            if isinstance(target, ast.UnaryOp) and isinstance(target.op, ast.USub) and isinstance(target.operand, ast.Constant):
                pid = -target.operand.value if type(target.operand.value) is int else None
            if pid is not None:
                self.check_pid(pid, rows, group=name == "os.killpg")
