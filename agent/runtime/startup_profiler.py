"""Opt-in phase profiler for the backend startup critical path."""

from __future__ import annotations

from agent.runtime.paths import state_path

import json
import os
import time
from pathlib import Path
from typing import Any


class StartupProfiler:
    def __init__(self, root: str | Path, *, enabled: bool, started: float | None = None):
        self.root = Path(root).expanduser().resolve()
        self.enabled = enabled
        self.started = started if started is not None else time.perf_counter()
        self.previous = self.started
        self.phases: list[dict[str, Any]] = []
        self._finished = False

    @classmethod
    def from_env(
        cls,
        root: str | Path,
        *,
        started: float | None = None,
    ) -> "StartupProfiler":
        enabled = os.getenv("ASTRA_PROFILE_STARTUP", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        return cls(root, enabled=enabled, started=started)

    def mark(self, name: str) -> None:
        if not self.enabled or self._finished:
            return
        now = time.perf_counter()
        self.phases.append({
            "name": str(name)[:80],
            "delta_ms": round((now - self.previous) * 1000, 3),
            "total_ms": round((now - self.started) * 1000, 3),
        })
        self.previous = now

    def finish(self, name: str = "ready") -> dict[str, Any]:
        if not self.enabled or self._finished:
            return {}
        self.mark(name)
        self._finished = True
        report = {
            "version": 1,
            "timestamp": time.time(),
            "pid": os.getpid(),
            "total_ms": self.phases[-1]["total_ms"] if self.phases else 0.0,
            "phases": self.phases,
        }
        path = state_path("startup-profile.json", root=self.root)
        temporary = path.with_suffix(".json.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, path)
            report["path"] = str(path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return report
