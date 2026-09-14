"""User-owned trust decision for automatically discovered project inputs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


_TRUTHY = {"1", "true", "yes", "on", "trusted"}


def _project_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    for directory in (resolved, *resolved.parents):
        if (directory / ".git").exists():
            return directory
    return resolved


def _configured_roots() -> set[Path]:
    roots: set[Path] = set()
    for raw in os.getenv("ASTRA_TRUSTED_PROJECTS", "").split(os.pathsep):
        value = raw.strip()
        if not value:
            continue
        try:
            roots.add(Path(value).expanduser().resolve())
        except OSError:
            continue
    return roots


@dataclass(frozen=True)
class ProjectTrust:
    root: Path
    trusted: bool
    source: str

    @classmethod
    def for_path(cls, path: str | Path) -> "ProjectTrust":
        root = _project_root(Path(path))
        if os.getenv("ASTRA_PROJECT_TRUST", "").strip().lower() in _TRUTHY:
            return cls(root, True, "ASTRA_PROJECT_TRUST")
        if root in _configured_roots():
            return cls(root, True, "ASTRA_TRUSTED_PROJECTS")
        return cls(root, False, "default-untrusted")

    def permits(self, path: str | Path) -> bool:
        candidate = Path(path).expanduser().resolve()
        if candidate != self.root and self.root not in candidate.parents:
            return True
        return self.trusted
