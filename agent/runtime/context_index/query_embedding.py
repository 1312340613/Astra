"""One warm encoding per recommendation request, shared across source workers."""
from __future__ import annotations

from contextvars import ContextVar
import threading
import time
from collections.abc import Sequence

from . import embedder

_slots = threading.BoundedSemaphore(2)
current_embedding: ContextVar["QueryEmbedding | None"] = ContextVar("context_index_embedding", default=None)


class QueryEmbedding:
    def __init__(self, query: str, budget_seconds: float = 0.2) -> None:
        self.query = query
        self._deadline = time.monotonic() + budget_seconds
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._started = False
        self._value: Sequence[float] | None = None

    def get(self) -> Sequence[float] | None:
        with self._lock:
            owner = not self._started
            self._started = True
        if owner:
            acquired = _slots.acquire(blocking=False)
            try:
                backend = embedder.ready_embedder()
                if acquired and backend is not None:
                    rows = backend.encode([self.query])
                    self._value = rows[0] if rows else None
            except Exception:
                self._value = None
            finally:
                if acquired:
                    _slots.release()
                self._done.set()
        else:
            self._done.wait(max(0.0, self._deadline - time.monotonic()))
        return self._value if self._done.is_set() else None
