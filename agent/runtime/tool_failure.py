"""Structured failures shared by the model/tool execution boundary."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolFailure:
    """A stable, UI-safe error contract for tool-related failures."""

    code: str
    message: str
    retryable: bool
    recovery_hint: str = ""
    tool_name: str = ""
    call_id: str = ""
    partial: bool = False
    duration_ms: int | None = None
    details: dict[str, Any] = field(default_factory=dict)
    artifact_ref: str = ""

    def to_event(self, *, recoverable: bool = True) -> dict[str, Any]:
        event: dict[str, Any] = {
            "type": "error",
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "recoverable": recoverable,
            "recovery_hint": self.recovery_hint,
            "tool_name": self.tool_name,
            "call_id": self.call_id,
            "partial": self.partial,
            "details": dict(self.details),
        }
        if self.duration_ms is not None:
            event["duration_ms"] = self.duration_ms
        if self.artifact_ref:
            event["artifact_ref"] = self.artifact_ref
        return event
