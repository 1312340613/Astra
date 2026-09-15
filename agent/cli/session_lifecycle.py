"""Small restart state machine, independent of transport and process exit."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

RESTART_EXIT_CODE = 42


class ControlledRestart:
    def __init__(self, *, supported: bool = True, timeout: float = 120,
                 clock: Callable[[], float] = time.monotonic):
        self.supported = supported
        self.timeout = timeout
        self.clock = clock
        self.request_id = ""
        self.deadline = 0.0
        self.state = "idle"

    @property
    def draining(self) -> bool:
        return self.state in {"draining", "awaiting_ack", "exiting"}

    def request(self) -> dict:
        if not self.supported:
            raise ValueError("Controlled restart requires the current Astra TUI. Use /reconnect after stopping the backend.")
        if not self.draining:
            self.request_id = uuid.uuid4().hex
            self.deadline = self.clock() + self.timeout
            self.state = "draining"
        return {"type": "restart_status", "state": self.state, "request_id": self.request_id,
                "message": "Restart requested. Waiting for the current work to finish; /restart cancel cancels it."}

    def cancel(self, message: str = "Restart cancelled. The current backend remains available.") -> dict:
        self.state = "idle"
        return {"type": "restart_status", "state": "cancelled", "request_id": self.request_id, "message": message}

    def advance(self, *, busy: bool, session: str) -> dict | None:
        if not self.draining or self.state == "exiting":
            return None
        if self.clock() >= self.deadline:
            return self.cancel("Restart timed out and was cancelled; no running work was interrupted.")
        if self.state == "draining" and not busy:
            self.state = "awaiting_ack"
            return {"type": "restart_ready", "request_id": self.request_id, "session": session}
        return None

    def acknowledge(self, request_id: str) -> bool:
        if (self.state != "awaiting_ack" or request_id != self.request_id
                or self.clock() >= self.deadline):
            return False
        self.state = "exiting"
        return True
