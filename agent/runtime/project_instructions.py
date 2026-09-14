"""Budgeted project guidance with hierarchical and path-scoped loading."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .project_trust import ProjectTrust


@dataclass(frozen=True)
class PathRule:
    path: Path
    patterns: tuple[str, ...]
    body: str


class ProjectInstructions:
    def __init__(
        self,
        workdir: str | Path,
        *,
        max_bytes: int | None = None,
        trusted: bool | None = None,
    ):
        self.workdir = Path(workdir).expanduser().resolve()
        self.root = self._project_root(self.workdir)
        self.max_bytes = max_bytes or self._positive_env("PROJECT_INSTRUCTIONS_MAX_BYTES", 32 * 1024)
        decision = ProjectTrust.for_path(self.workdir)
        self.trusted = decision.trusted if trusted is None else bool(trusted)
        self.base_prompt = ""
        self.rules: list[PathRule] = []
        self.reload()

    @staticmethod
    def _positive_env(name: str, default: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError:
            return default
        return value if value > 0 else default

    @staticmethod
    def _project_root(workdir: Path) -> Path:
        for directory in (workdir, *workdir.parents):
            if (directory / ".git").exists():
                return directory
        return workdir

    def _directories(self) -> list[Path]:
        try:
            relative = self.workdir.relative_to(self.root)
        except ValueError:
            return [self.workdir]
        directories = [self.root]
        current = self.root
        for part in relative.parts:
            current = current / part
            directories.append(current)
        return directories

    @staticmethod
    def _read_bounded(path: Path, remaining: int) -> str:
        if remaining <= 0 or not path.is_file():
            return ""
        try:
            with path.open("rb") as handle:
                raw = handle.read(remaining)
            return raw.decode("utf-8", errors="replace")
        except OSError:
            return ""

    @staticmethod
    def _parse_rule(path: Path, remaining: int) -> PathRule | None:
        content = ProjectInstructions._read_bounded(path, remaining).replace("\r\n", "\n")
        if not content.startswith("---\n"):
            return None
        end = content.find("\n---", 4)
        if end < 0:
            return None
        frontmatter = content[4:end]
        body = content[end + 4:].lstrip("\r\n")
        patterns: list[str] = []
        in_paths = False
        for line in frontmatter.splitlines():
            stripped = line.strip()
            if re.match(r"^paths\s*:\s*$", stripped):
                in_paths = True
                continue
            inline = re.match(r"^paths\s*:\s*\[(.*)]\s*$", stripped)
            if inline:
                patterns.extend(
                    item.strip().strip("\"'")
                    for item in inline.group(1).split(",")
                    if item.strip()
                )
                in_paths = False
                continue
            if in_paths and stripped.startswith("-"):
                value = stripped[1:].strip().strip("\"'")
                if value:
                    patterns.append(value)
            elif stripped and not stripped.startswith("#"):
                in_paths = False
        if not patterns or not body.strip():
            return None
        return PathRule(path=path, patterns=tuple(patterns[:128]), body=body.strip())

    def reload(self) -> None:
        if not self.trusted:
            self.base_prompt = ""
            self.rules = []
            return
        remaining = self.max_bytes
        blocks: list[str] = []
        rules: list[PathRule] = []
        for directory in self._directories():
            override = directory / "AGENTS.override.md"
            standard = directory / "AGENTS.md"
            selected = override if override.is_file() else standard
            content = self._read_bounded(selected, remaining)
            if content.strip():
                encoded = content.encode("utf-8")
                remaining -= len(encoded)
                blocks.append(f"## Project guidance: {selected}\n{content.strip()}")
            for rules_dir in (directory / ".astra" / "rules", directory / ".agents" / "rules"):
                if not rules_dir.is_dir() or remaining <= 0:
                    continue
                try:
                    paths = sorted(rules_dir.rglob("*.md"))
                except OSError:
                    paths = []
                for path in paths:
                    rule = self._parse_rule(path, remaining)
                    if rule is None:
                        continue
                    remaining -= len(rule.body.encode("utf-8"))
                    rules.append(rule)
                    if remaining <= 0:
                        break
        self.base_prompt = "\n\n".join(blocks)
        self.rules = rules

    def rules_for(self, paths: set[str]) -> str:
        if not paths or not self.rules:
            return ""
        normalized: set[str] = set()
        for path in paths:
            value = str(path).replace("\\", "/")
            while value.startswith("./"):
                value = value[2:]
            normalized.add(value)
        blocks: list[str] = []
        seen: set[Path] = set()
        for rule in self.rules:
            if rule.path in seen:
                continue
            if any(fnmatch.fnmatch(path, pattern) for path in normalized for pattern in rule.patterns):
                seen.add(rule.path)
                blocks.append(f"## Path-scoped project rule: {rule.path}\n{rule.body}")
        return "\n\n".join(blocks)
