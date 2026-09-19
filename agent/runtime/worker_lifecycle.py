"""Small, clock-explicit worker diagnostics independent of model behavior."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


class WorkerLifecycle:
    def __init__(
        self, limits: dict[str, Any], *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        publish: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.clock, self.wall_clock, self.publish = clock, wall_clock, publish
        self.started_at = wall_clock()
        self.started_monotonic = self.since = clock()
        self.state = "active"
        self.elapsed = {"active": 0.0, "idle": 0.0}
        self.limits = dict(limits)
        self.idle: dict[str, Any] = {}
        self.completion_reason = ""

    def transition(self, state: str, *, reason: str = "", idle_deadline: float | None = None) -> dict[str, Any]:
        now = self.clock()
        if self.state in self.elapsed:
            self.elapsed[self.state] += max(0.0, now - self.since)
        self.since, self.state = now, state
        self.completion_reason = reason
        if state == "idle":
            self.idle = {
                "last_idle_at": self.wall_clock(),
                "last_idle_monotonic": now,
                "idle_deadline_monotonic": idle_deadline,
            }
        result = self.snapshot()
        if self.publish is not None:
            self.publish(result)
        return result

    def snapshot(self) -> dict[str, Any]:
        now, wall = self.clock(), self.wall_clock()
        elapsed = dict(self.elapsed)
        if self.state in elapsed:
            elapsed[self.state] += max(0.0, now - self.since)
        return {
            "state": self.state,
            "completion_reason": self.completion_reason,
            "clock_basis": "monotonic",
            "timing_freshness": "last_transition",
            "clock_implementation": time.get_clock_info("monotonic").implementation,
            "started_at": self.started_at,
            "started_monotonic": self.started_monotonic,
            "observed_at": wall,
            "observed_monotonic": now,
            "wall_elapsed_seconds": round(max(0.0, wall - self.started_at), 3),
            "active_elapsed_seconds": round(elapsed["active"], 3),
            "idle_elapsed_seconds": round(elapsed["idle"], 3),
            "limits": dict(self.limits),
            **self.idle,
        }
