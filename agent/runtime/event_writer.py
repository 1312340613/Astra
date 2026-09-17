"""Bounded, ordered backend event persistence and transport off the event loop."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from typing import Callable

from .async_io import durable_io
from .event_stream import RuntimeEventStream
from .latency import current_profiler, current_request_id

logger = logging.getLogger(__name__)


class EventQueueFull(RuntimeError):
    """A synchronous producer exceeded the explicit outbox capacity."""


class OrderedEventWriter:
    """One owned worker preserves publication, stdout and replay order.

    The queue counts UTF-8 bytes and items, excluding one in-flight operation.
    Event JSON is captured at admission, so later producer mutation cannot
    change an accepted event. Async producers wait for space; callback producers
    fail explicitly on overload. Accepted events are drained before close.
    """

    def __init__(
        self,
        stream: RuntimeEventStream | None,
        write: Callable[[dict], None],
        *,
        max_items: int = 1024,
        max_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        self.stream = stream
        self.write = write
        self.profiler = current_profiler()
        self.max_items = max(1, max_items)
        self.max_bytes = max(1, max_bytes)
        self._loop = asyncio.get_running_loop()
        self._space = asyncio.Event()
        self._space.set()
        self._condition = threading.Condition()
        self._queue: deque[tuple[str, str, int, float, str]] = deque()
        self._bytes = 0
        self._closing = False
        self._error: Exception | None = None
        self._last_delivery = 0.0
        self._thread = threading.Thread(target=self._run, name="astra-event-writer", daemon=True)
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def _put(self, kind: str, payload: str, size: int) -> None:
        with self._condition:
            if self._error is not None:
                raise self._error
            if self._closing:
                raise RuntimeError("Event writer is closed")
            if size > self.max_bytes:
                raise EventQueueFull("Event exceeds the outbox byte limit")
            if len(self._queue) >= self.max_items or self._bytes + size > self.max_bytes:
                raise EventQueueFull("Event outbox is full; async producers must await capacity")
            self._queue.append((kind, payload, size, time.perf_counter() if self.profiler else 0.0,
                                current_request_id() if self.profiler else ""))
            self._bytes += size
            self._condition.notify()

    def send(self, event: dict) -> None:
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        self._put("event", payload, len(payload.encode("utf-8")))

    async def send_async(self, event: dict) -> None:
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        size = len(payload.encode("utf-8"))
        if size > self.max_bytes:
            raise EventQueueFull("Event exceeds the outbox byte limit")
        while True:
            with self._condition:
                try:
                    self._put("event", payload, size)
                    return
                except EventQueueFull:
                    self._space.clear()
            await self._space.wait()

    async def wait_for_capacity(self) -> None:
        """Yield streaming producers before callbacks can exhaust the reserve."""
        while True:
            with self._condition:
                if self._error is not None:
                    raise self._error
                if self._closing:
                    raise RuntimeError("Event writer is closed")
                if len(self._queue) < max(1, self.max_items // 2) and self._bytes < max(1, self.max_bytes // 2):
                    return
                self._space.clear()
            await self._space.wait()

    def replay(self, after_cursor: int, *, limit: int = 500) -> None:
        payload = json.dumps({"after_cursor": max(0, after_cursor), "limit": max(1, min(limit, 2000))})
        self._put("replay", payload, len(payload))

    def _publish(self, event: dict, *, queue_ms: float = 0.0, request_id: str = "") -> None:
        started = time.perf_counter() if self.profiler else 0.0
        if self.stream is not None:
            try:
                event = self.stream.publish(event)
            except Exception:
                # Preserve the existing degraded live-delivery contract; never
                # invent a cursor for an event that failed to commit.
                logger.exception("runtime event persistence failed type=%s", event.get("type", ""))
                event = {**event, "replay_unavailable": True}
        persisted = time.perf_counter() if self.profiler else 0.0
        if self.profiler is not None:
            event = self.profiler.trace_event(event, request_id=request_id)
        self._write_output(event)
        if self.profiler is not None:
            self.profiler.record("event", {
                "queue_ms": queue_ms, "persist_ms": (persisted - started) * 1000,
                "write_ms": (time.perf_counter() - persisted) * 1000,
            }, identity=str(event.get("performance_trace_id") or ""), label=str(event.get("type") or ""),
               request_id=request_id)

    def _deliver(self, kind: str, payload: str, *, queue_ms: float = 0.0, request_id: str = "") -> None:
        event = json.loads(payload)
        if kind == "event":
            self._publish(event, queue_ms=queue_ms, request_id=request_id)
            return
        after = event["after_cursor"]
        limit = event["limit"]
        replayed = self.stream.replay(after, limit=limit) if self.stream is not None else []
        for saved in replayed:
            self._write_output(saved)
        self._publish({
            "type": "event_replay_complete", "after_cursor": after,
            "next_cursor": int(replayed[-1]["cursor"]) if replayed else after,
            "cursor": self.stream.cursor if self.stream is not None else after,
            "count": len(replayed), "has_more": len(replayed) >= limit,
        })

    def _write_output(self, event: dict) -> None:
        self.write(event)
        # Only completed output counts as progress, not queue admission or a
        # persistence attempt. Replay output must advance this clock too.
        with self._condition:
            self._last_delivery = time.monotonic()

    def _wake_producers(self) -> None:
        if not self._space.is_set():
            try:
                self._loop.call_soon_threadsafe(self._space.set)
            except RuntimeError:
                # A timed-out daemon writer may finish after its owner exits.
                if not self._loop.is_closed():
                    raise

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: bool(self._queue) or self._closing)
                    if not self._queue:
                        return
                    kind, payload, size, queued, request_id = self._queue.popleft()
                    self._bytes -= size
                    self._wake_producers()
                self._deliver(kind, payload, queue_ms=(time.perf_counter() - queued) * 1000 if self.profiler else 0.0,
                              request_id=request_id)
        except Exception as exc:
            with self._condition:
                self._error = exc
                self._queue.clear()
                self._bytes = 0
                self._wake_producers()

    def _join_until(self, started: float, timeout: float, stall_timeout: float) -> None:
        deadline = started + timeout
        while self.alive:
            with self._condition:
                idle_deadline = max(started, self._last_delivery) + stall_timeout
            remaining = min(deadline, idle_deadline) - time.monotonic()
            if remaining <= 0:
                return
            self._thread.join(remaining)

    async def close(self, *, timeout: float = 8.0, stall_timeout: float = 2.0) -> None:
        # Allow a healthy backlog to drain within the TUI's 10-second grace,
        # but retain the short bound for a reader or disk that stops progressing.
        started = time.monotonic()
        with self._condition:
            self._closing = True
            self._condition.notify()
            self._wake_producers()
        # Bound the join itself. Cancelling an unbounded to_thread(join) would
        # still leave asyncio.run waiting for that executor thread at exit.
        await durable_io(self._join_until, started, max(0.0, timeout), max(0.0, stall_timeout))
        if self.alive:
            with self._condition:
                self._error = TimeoutError("Backend event output did not drain before shutdown")
                self._queue.clear()
                self._bytes = 0
                self._wake_producers()
        if self._error is not None:
            raise self._error
