"""One optional deadline shared by a turn's preparation, providers and tools."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncGenerator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TypeVar


class TurnBudgetExceeded(RuntimeError):
    def __init__(self, budget: "TurnBudget"):
        self.seconds = budget.seconds
        self.elapsed = max(0.0, time.monotonic() - budget.started)
        super().__init__(
            f"Turn time budget exhausted ({budget.seconds:g}s). Completed results are retained. "
            "Check uncertain tool effects before continuing; continue or /resume starts a new budget."
        )


def parse_turn_budget(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("Budget must be off or a number of seconds from 0 to 86400.")
    if isinstance(value, str) and value.strip().lower() == "off":
        return 0.0
    try:
        if not isinstance(value, (str, int, float)):
            raise ValueError
        seconds = float(value)
    except (TypeError, ValueError):
        raise ValueError("Budget must be off or a number of seconds from 0 to 86400.") from None
    if not math.isfinite(seconds) or not 0 <= seconds <= 86400:
        raise ValueError("Budget must be off or a number of seconds from 0 to 86400.")
    return seconds


@dataclass
class TurnBudget:
    seconds: float
    started: float = field(default_factory=time.monotonic)
    completed: bool = False

    @property
    def deadline(self) -> float:
        return self.started + self.seconds

    @property
    def work_deadline(self) -> float:
        return self.deadline - min(5.0, self.seconds * 0.1)

    def check_work(self) -> None:
        if not self.completed and time.monotonic() >= self.work_deadline:
            raise TurnBudgetExceeded(self)


_CURRENT: ContextVar[TurnBudget | None] = ContextVar("astra_turn_budget", default=None)


def current_turn_budget() -> TurnBudget | None:
    budget = _CURRENT.get()
    return budget if budget is not None and not budget.completed else None


def check_work_budget() -> None:
    budget = current_turn_budget()
    if budget is not None:
        budget.check_work()


async def _drain(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    drain = asyncio.gather(task, return_exceptions=True)
    while not drain.done():
        try:
            await asyncio.shield(drain)
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()


Event = TypeVar("Event", bound=dict)


async def budgeted_events(source: AsyncGenerator[Event, None], seconds: float) -> AsyncGenerator[Event, None]:
    """Own the timeout task; never arm cancellation across a caller's yield."""
    if seconds <= 0:
        async for event in source:
            yield event
        return
    budget = TurnBudget(seconds)
    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=1)

    async def pump() -> None:
        token = _CURRENT.set(budget)
        timeout = asyncio.timeout(max(0.0, budget.deadline - time.monotonic()))
        try:
            try:
                async with timeout:
                    async for event in source:
                        if event.get("type") == "done":
                            budget.completed = True
                            timeout.reschedule(None)
                        await queue.put(event)
            except TimeoutError:
                if timeout.expired():
                    raise TurnBudgetExceeded(budget) from None
                raise
        finally:
            try:
                await source.aclose()
            finally:
                _CURRENT.reset(token)

    worker = asyncio.create_task(pump(), name="astra-budgeted-turn")
    read: asyncio.Task[Event] | None = None
    try:
        while not worker.done() or not queue.empty():
            read = asyncio.create_task(queue.get())
            await asyncio.wait((worker, read), return_when=asyncio.FIRST_COMPLETED)
            if read.done():
                yield read.result()
                read = None
            else:
                await _drain(read)
                read = None
                if queue.empty():
                    break
        await worker
    finally:
        if read is not None:
            await _drain(read)
        await _drain(worker)
