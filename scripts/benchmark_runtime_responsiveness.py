"""Offline A/B: identical SQLite lock contention with sync vs queued delivery.

Run with the project Python. This creates only temporary databases, invokes no
model/provider, and measures loop stalls separately from durable delivery time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.runtime.event_stream import RuntimeEventStream
from agent.runtime.event_writer import OrderedEventWriter


def distribution(values: list[float]) -> dict:
    ordered = sorted(values)
    return {"n": len(values), "p50_ms": round(statistics.median(values), 3),
            "p95_ms": round(ordered[max(0, math.ceil(len(ordered) * .95) - 1)], 3),
            "max_ms": round(max(values), 3)}


async def sample(root: Path, *, queued: bool, lock_ms: float) -> dict:
    stream = RuntimeEventStream(root / "events.db")
    ready = threading.Event()

    def contend() -> None:
        connection = sqlite3.connect(stream.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            ready.set()
            time.sleep(lock_ms / 1000)
            connection.rollback()
        finally:
            connection.close()

    holder = threading.Thread(target=contend)
    holder.start()
    assert await asyncio.to_thread(ready.wait, 2)
    running = True
    delays: list[float] = []

    async def heartbeat() -> None:
        while running:
            before = time.perf_counter()
            await asyncio.sleep(.005)
            delays.append(max(0, (time.perf_counter() - before - .005) * 1000))

    clock = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    started = time.perf_counter()
    output: list[dict] = []
    if queued:
        writer = OrderedEventWriter(stream, output.append)
        writer.send({"type": "tool_progress", "call_id": "fixture", "stage": "running"})
        admission_ms = (time.perf_counter() - started) * 1000
        await writer.close()
    else:
        output.append(stream.publish({"type": "tool_progress", "call_id": "fixture", "stage": "running"}))
        admission_ms = (time.perf_counter() - started) * 1000
    completed_ms = (time.perf_counter() - started) * 1000
    await asyncio.sleep(.01)
    running = False
    await clock
    holder.join()
    assert len(output) == 1 and len(stream.replay(0)) == 1
    return {"loop_stall_ms": max(delays, default=0), "durable_delivery_ms": completed_ms,
            "producer_admission_ms": admission_ms}


async def benchmark(samples: int, lock_ms: float) -> dict:
    results: dict[str, list[dict]] = {"synchronous": [], "queued": []}
    with tempfile.TemporaryDirectory(prefix="astra-latency-") as directory:
        for index in range(samples):
            # Alternate order to reduce systematic warmup/order bias.
            modes = [False, True] if index % 2 == 0 else [True, False]
            for queued in modes:
                path = Path(directory) / f"{index}-{queued}"
                result = await sample(path, queued=queued, lock_ms=lock_ms)
                results["queued" if queued else "synchronous"].append(result)
    return {"schema": 1, "lock_ms": lock_ms, "samples_per_mode": samples,
            "scope": "fault-injected local SQLite; not real-user latency or provider speed",
            "results": {mode: {key: distribution([r[key] for r in rows]) for key in rows[0]}
                        for mode, rows in results.items()}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--lock-ms", type=float, default=150)
    args = parser.parse_args()
    if not 1 <= args.samples <= 1000 or not 0 <= args.lock_ms <= 1000:
        parser.error("samples must be 1..1000 and lock-ms must be 0..1000")
    print(json.dumps(asyncio.run(benchmark(args.samples, args.lock_ms)), indent=2))


if __name__ == "__main__":
    main()
