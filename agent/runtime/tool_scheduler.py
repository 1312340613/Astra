"""Bounded tool overlap without crossing ordered-operation barriers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

Call = TypeVar("Call")
Result = TypeVar("Result")


async def execute_ordered(
    calls: Sequence[Call],
    execute: Callable[[Call], Awaitable[Result]],
    can_overlap: Callable[[Call], bool],
    limit: int,
) -> list[Result]:
    """Reclassify each unstarted call; return results in original model order."""
    pending: dict[asyncio.Task[Result], int] = {}
    results: dict[int, Result] = {}
    position = 0
    async def invoke(call: Call) -> Result:
        return await execute(call)
    try:
        while position < len(calls) or pending:
            # An exclusive call stays at the front until all predecessors
            # finish. Later reads cannot leapfrog it into the current pool.
            if not pending and position < len(calls) and not can_overlap(calls[position]):
                results[position] = await execute(calls[position])
                position += 1
                continue
            while position < len(calls) and len(pending) < max(1, limit):
                if not can_overlap(calls[position]):
                    break
                pending[asyncio.create_task(invoke(calls[position]))] = position
                position += 1
            if pending:
                completed, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in sorted(completed, key=pending.__getitem__):
                    results[pending.pop(task)] = task.result()
    finally:
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            # Repeated cancellation must not orphan an already-started call.
            drain = asyncio.gather(*pending, return_exceptions=True)
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    for task in pending:
                        if not task.done():
                            task.cancel()
    return [results[index] for index in range(len(calls))]
