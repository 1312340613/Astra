"""Small in-process reliability metrics used by Phase T release gates."""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class RuntimeMetrics:
    _counters: Counter[str] = field(default_factory=Counter)
    _totals: Counter[str] = field(default_factory=Counter)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def observe(self, name: str, value: float) -> None:
        scaled = int(round(max(0.0, value) * 1000))
        with self._lock:
            self._counters[f"{name}_count"] += 1
            self._totals[name] += scaled
            self._counters[f"{name}_last_micros"] = scaled
            self._counters[f"{name}_max_micros"] = max(
                self._counters[f"{name}_max_micros"],
                scaled,
            )

    @staticmethod
    def _rate(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 0.0

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            counters = dict(self._counters)
            totals = dict(self._totals)
        recovery_attempts = int(counters.get("recovery_attempt_count", 0))
        recovery_successes = int(counters.get("recovery_success_count", 0))
        background_started = int(counters.get("background_started_count", 0))
        background_completed = int(counters.get("background_completed_count", 0))
        postcondition_checks = int(counters.get("postcondition_check_count", 0))
        postcondition_failures = int(counters.get("postcondition_failure_count", 0))
        approval_count = int(counters.get("approval_wait_ms_count", 0))
        approval_total_micros = int(totals.get("approval_wait_ms", 0))
        return {
            **counters,
            "tool_truncation_count": int(counters.get("tool_truncation_count", 0)),
            "recovery_success_rate": self._rate(recovery_successes, recovery_attempts),
            "repeated_payload_count": int(counters.get("repeated_payload_count", 0)),
            "approval_wait_ms": round(
                approval_total_micros / 1000 / approval_count,
                2,
            ) if approval_count else 0.0,
            "background_completion_rate": self._rate(
                background_completed,
                background_started,
            ),
            "postcondition_failure_rate": self._rate(
                postcondition_failures,
                postcondition_checks,
            ),
        }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._totals.clear()

    def render(self) -> str:
        snapshot = self.snapshot()
        required = (
            "tool_truncation_count",
            "recovery_success_rate",
            "repeated_payload_count",
            "approval_wait_ms",
            "background_completion_rate",
            "postcondition_failure_rate",
        )
        return "\n".join(
            f"{name}: {snapshot[name]}"
            for name in required
        )


runtime_metrics = RuntimeMetrics()
