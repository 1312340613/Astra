"""Opt-in content-free runtime timings, written through a bounded worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import queue
import re
import statistics
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

from .bounded_artifacts import append_bounded_text
from .paths import state_path

_PROFILE: ContextVar[RuntimeProfiler | None] = ContextVar("runtime_profiler", default=None)
_TOOL: ContextVar[ToolTiming | None] = ContextVar("tool_timing", default=None)
_REQUEST: ContextVar[str] = ContextVar("latency_request", default="")
_KINDS = {"llm", "io", "tool", "event", "frontend", "profiler"}
_EVENTS = {"task_started", "tool_calls", "tool_result", "done", "chunk", "reasoning"}
_PHASES = {
    "preparing": "prepare", "claiming": "claim", "authorizing": "authorization",
    "awaiting_approval": "approval", "approved": "authorization", "running": "execute",
    "retrying": "execute", "finalizing": "finalize", "postprocessing": "postprocess",
    "persisting_result": "result_save", "cached": "cache",
}


def current_profiler() -> RuntimeProfiler | None:
    return _PROFILE.get()


def current_request_id() -> str:
    return _REQUEST.get()


@contextmanager
def profile_request(request_id: str) -> Iterator[None]:
    token = _REQUEST.set(request_id)
    try:
        yield
    finally:
        _REQUEST.reset(token)


def tool_phase(stage: str) -> None:
    timing = _TOOL.get()
    if timing is not None:
        timing.stage(stage)


class RuntimeProfiler:
    """One recording lifetime. No prompt, output or argument values are accepted."""

    def __init__(self, path: Path, *, capacity: int = 2048) -> None:
        self.path = path
        self.generation = uuid.uuid4().hex
        self._queue: queue.Queue[dict | None] = queue.Queue(maxsize=max(1, capacity))
        self._lock = threading.Lock()
        self._closed = False
        self.dropped = 0
        self.write_failures = 0
        self._pending: OrderedDict[str, tuple[float, str, str]] = OrderedDict()
        self._seen_text: set[str] = set()
        self._text_request = ""
        self._thread = threading.Thread(target=self._write, name="astra-latency-writer", daemon=True)
        self._thread.start()

    @classmethod
    def from_env(cls, root: Path) -> RuntimeProfiler | None:
        if os.getenv("ASTRA_PROFILE_QUERY", "").strip().lower() not in {"1", "true", "yes", "on"}:
            return None
        return cls(state_path("runtime-profile.jsonl", root=root))

    @contextmanager
    def activate(self) -> Iterator[None]:
        token = _PROFILE.set(self)
        try:
            yield
        finally:
            _PROFILE.reset(token)

    def record(self, kind: str, metrics: dict[str, float], *, identity: str = "", label: str = "",
               request_id: str | None = None) -> None:
        if kind not in _KINDS:
            return
        safe_metrics = {
            key: round(float(value), 3)
            for key, value in list(metrics.items())[:32]
            if isinstance(key, str) and re.fullmatch(r"[a-z_]{1,48}", key)
            and isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 <= value <= 86_400_000 and math.isfinite(value)
        }
        # Identity hashes let a tool's I/O and total timings be joined without
        # trusting provider-supplied call IDs as harmless log content.
        event = {
            "schema": 1, "timestamp": time.time(), "generation": self.generation,
            "kind": kind, "metrics_ms": safe_metrics,
            "identity": hashlib.sha256(identity.encode()).hexdigest()[:16] if identity else "",
            "label": label if re.fullmatch(r"[a-zA-Z_][a-zA-Z_.]{0,95}", label) else "other",
        }
        request = current_request_id() if request_id is None else request_id
        event["request"] = hashlib.sha256(request.encode()).hexdigest()[:16] if request else ""
        with self._lock:
            if self._closed:
                return
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                self.dropped += 1

    def _append(self, events: list[dict]) -> None:
        try:
            append_bounded_text(self.path, "".join(json.dumps(e, sort_keys=True) + "\n" for e in events),
                                max_bytes=5 * 1024 * 1024, backup_count=2)
        except OSError:
            with self._lock:
                self.write_failures += len(events)

    def _write(self) -> None:
        while True:
            first = self._queue.get()
            if first is None:
                break
            batch = [first]
            stopping = False
            for _ in range(63):
                try:
                    event = self._queue.get_nowait()
                except queue.Empty:
                    break
                if event is None:
                    stopping = True
                    break
                batch.append(event)
            self._append(batch)
            if stopping:
                break
        self._append([{"schema": 1, "kind": "profiler", "generation": self.generation,
                       "dropped_records": self.dropped, "write_failures": self.write_failures,
                       "unacknowledged_frontend_samples": len(self._pending)}])

    def trace_event(self, event: dict, *, request_id: str = "") -> dict:
        """Sample milestones, plus first reasoning/text, for frontend receipts."""
        kind = event.get("type")
        with self._lock:
            if kind == "task_started":
                self._seen_text.clear()
            if kind not in _EVENTS or event.get("replayed"):
                return event
            if kind in {"chunk", "reasoning"}:
                if request_id and request_id != self._text_request:
                    self._text_request = request_id
                    self._seen_text.clear()
                if kind in self._seen_text:
                    return event
                self._seen_text.add(kind)
            now = time.perf_counter()
            while self._pending and (len(self._pending) >= 2048 or now - next(iter(self._pending.values()))[0] > 60):
                self._pending.popitem(last=False)
                self.dropped += 1
            trace_id = uuid.uuid4().hex
            self._pending[trace_id] = (now, str(kind), request_id)
        return {**event, "performance_trace_id": trace_id}

    def acknowledge(self, samples: object) -> None:
        if not isinstance(samples, list):
            return
        for sample in samples[:128]:
            if not isinstance(sample, dict) or not isinstance(sample.get("trace_id"), str):
                continue
            with self._lock:
                pending = self._pending.pop(sample["trace_id"], None)
            if pending is None:
                continue
            sent, kind, request_id = pending
            allowed = {"parse_ms", "batch_ms", "handle_ms", "react_commit_ms", "receive_to_commit_ms"}
            metrics = {key: value for key, value in sample.items() if key in allowed}
            # This is a Python-clock round trip, not a subtraction of Node and
            # Python clock origins or a measurement of terminal pixel latency.
            metrics["receipt_roundtrip_ms"] = (time.perf_counter() - sent) * 1000
            self.record("frontend", metrics, identity=sample["trace_id"], label=kind, request_id=request_id)

    async def close(self) -> None:
        from .async_io import durable_io

        with self._lock:
            if self._closed:
                return
            self._closed = True

        def stop() -> None:
            self._queue.put(None)
            self._thread.join()

        await durable_io(stop)


class ToolTiming:
    def __init__(self, profiler: RuntimeProfiler, *, queued_at: float, identity: str) -> None:
        self.profiler = profiler
        self.identity = identity
        self.started = queued_at
        self.last = time.perf_counter()
        self.phase = "prepare"
        self.outcome = "completed"
        self.metrics = {"queue_ms": (self.last - queued_at) * 1000}

    def stage(self, stage: str) -> None:
        phase = _PHASES.get(stage)
        if phase is None or phase == self.phase:
            return
        now = time.perf_counter()
        key = f"{self.phase}_ms"
        self.metrics[key] = self.metrics.get(key, 0.0) + (now - self.last) * 1000
        self.phase, self.last = phase, now

    @contextmanager
    def active(self) -> Iterator[None]:
        token = _TOOL.set(self)
        try:
            yield
        except asyncio.CancelledError:
            self.outcome = "cancelled"
            raise
        except Exception:
            self.outcome = "failed"
            raise
        finally:
            now = time.perf_counter()
            key = f"{self.phase}_ms"
            self.metrics[key] = self.metrics.get(key, 0.0) + (now - self.last) * 1000
            self.metrics["total_ms"] = (now - self.started) * 1000
            self.profiler.record("tool", self.metrics, identity=self.identity, label=self.outcome)
            _TOOL.reset(token)


def io_identity() -> str:
    timing = _TOOL.get()
    return timing.identity if timing is not None else ""


def analyze_runtime_profile(path: Path, *, generation: str = "") -> dict:
    """Summarize the latest backend lifetime, never mixing test/old sessions."""
    rows: list[dict] = []
    if path.exists():
        with path.open(encoding="utf-8") as source:
            for line in source:
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(row, dict) and row.get("schema") == 1:
                    rows.append(row)
    selected = generation or next((str(r.get("generation") or "") for r in reversed(rows) if r.get("generation")), "")
    groups: dict[str, list[float]] = {}
    diagnostics = {}
    complete = False
    requests: set[str] = set()
    for row in rows:
        if row.get("generation") != selected:
            continue
        if row.get("kind") == "profiler":
            complete = True
            diagnostics = {k: row[k] for k in ("dropped_records", "write_failures", "unacknowledged_frontend_samples") if k in row}
            continue
        if row.get("request"):
            requests.add(str(row["request"]))
        metrics = row.get("metrics_ms")
        if not isinstance(metrics, dict):
            continue
        for name, value in metrics.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                key = f"{row.get('kind', 'unknown')}.{row.get('label', 'other')}.{name}"
                groups.setdefault(key, []).append(value)
    return {"schema": 1, "generation": selected, "complete": complete, "requests": len(requests), "diagnostics": diagnostics,
            "timings": {key: {"n": len(values), "p50_ms": round(statistics.median(values), 3),
                               "p95_ms": round(sorted(values)[math.ceil(len(values) * .95) - 1], 3),
                               "max_ms": round(max(values), 3)}
                        for key, values in sorted(groups.items())}}


def main() -> None:
    from argparse import ArgumentParser

    parser = ArgumentParser(description="Summarize content-free latency timings for one backend lifetime.")
    parser.add_argument("path", nargs="?", type=Path, default=state_path("runtime-profile.jsonl"))
    parser.add_argument("--generation", default="", help="exact generation ID; defaults to the latest")
    args = parser.parse_args()
    print(json.dumps(analyze_runtime_profile(args.path, generation=args.generation), indent=2))


if __name__ == "__main__":
    main()
