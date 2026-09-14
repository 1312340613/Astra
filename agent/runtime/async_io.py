"""Await blocking persistence without abandoning an in-flight write on cancel."""

from __future__ import annotations

import asyncio
import contextvars
import time
from typing import Callable, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


async def durable_io(function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run I/O off the loop; settle it before propagating cancellation.

    Cancelling ``to_thread`` cannot stop its OS thread. Shielding and draining
    prevents a late commit from racing with task finalization or session close.
    Repeated cancellation must not interrupt that drain. Callers still own
    domain cleanup (e.g. marking an unconfirmed tool step unknown).
    """
    from .latency import current_profiler, io_identity

    profiler = current_profiler()
    queued = time.perf_counter() if profiler is not None else 0.0
    worker_started = queued
    worker_finished = queued

    def invoke() -> T:
        nonlocal worker_started, worker_finished
        if profiler is not None:
            worker_started = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            if profiler is not None:
                worker_finished = time.perf_counter()

    # A bare executor Future is not included in asyncio.run's all-task
    # cancellation sweep. A child Task wrapping to_thread could itself be
    # cancelled during shutdown and falsely look settled while its OS thread
    # still writes. Copy context just as asyncio.to_thread does.
    context = contextvars.copy_context()
    operation = asyncio.get_running_loop().run_in_executor(None, context.run, invoke)
    cancellation: asyncio.CancelledError | None = None
    try:
        while True:
            try:
                result = await asyncio.shield(operation)
                break
            except asyncio.CancelledError as exc:
                if operation.cancelled():
                    raise
                cancellation = exc
            except Exception:
                if cancellation is not None:
                    raise cancellation from None
                raise
    finally:
        if profiler is not None:
            profiler.record("io", {
                "queue_ms": (worker_started - queued) * 1000,
                "execution_ms": (worker_finished - worker_started) * 1000,
                "total_ms": (time.perf_counter() - queued) * 1000,
            }, identity=io_identity(), label=getattr(function, "__qualname__", "io").replace("<locals>.", ""))
    if cancellation is not None:
        raise cancellation
    return result
