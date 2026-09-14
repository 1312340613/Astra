"""Opt-in, offline macOS memory probe using Astra's actual cached MLX model.

Uses synthetic text and an isolated worker, never a user's live embedding
runtime or archives. Both MLX allocator metrics and OS footprint are reported.
"""
from __future__ import annotations

import argparse
from contextlib import suppress
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from agent.runtime.context_index import embedding_runtime as runtime

_PEER = """
import json, sys
from agent.runtime.context_index.embedding_runtime import SharedMlxBackend
client = SharedMlxBackend()
try:
    client.load()
    vectors = client.encode(["另外一个会话查询相同的历史记录"])
    print(json.dumps({"worker_pid": client._state["pid"],
                      "client_has_mlx": "mlx.core" in sys.modules,
                      "dimensions": len(vectors[0])}))
finally:
    client.close()
"""


def measure(iterations: int, max_peak_gib: float) -> dict:
    previous = {key: os.environ.get(key) for key in (
        "ASTRA_EMBEDDING_RUNTIME_DIR", "ASTRA_CONTEXT_INDEX_EMBEDDING",
        "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE",
    )}
    try:
        with tempfile.TemporaryDirectory(prefix="astra-embedding-memory-") as temporary:
            os.environ.update(ASTRA_EMBEDDING_RUNTIME_DIR=temporary, ASTRA_CONTEXT_INDEX_EMBEDDING="on",
                              HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
            client = runtime.SharedMlxBackend()
            try:
                started = time.monotonic()
                client.load()
                load_ms = (time.monotonic() - started) * 1000
                state = runtime.read_state(Path(temporary))
                baseline = client.memory_stats()
                timings: dict[str, list[float]] = {"short": [], "long": []}
                for index in range(iterations):
                    kind = "long" if index % 2 else "short"
                    text = ("复盘项目的历史决策与验证结果，保留重要的技术细节。" * 50)[:900] if kind == "long" else "之前的项目决策是什么"
                    started = time.monotonic()
                    client.encode([text])
                    timings[kind].append((time.monotonic() - started) * 1000)
                peer = subprocess.run([sys.executable, "-c", _PEER], check=True, capture_output=True,
                                      text=True, timeout=30)
                peer_result = json.loads(peer.stdout)
                memory = client.memory_stats()
                footprint = subprocess.run(["vmmap", "-summary", str(state["pid"])],
                                           capture_output=True, text=True, timeout=15)
                result = {
                    "schema_version": 1, "identity": runtime.identity(), "worker_pid": state["pid"],
                    "fixture": "synthetic alternating short queries and 900-character passages; no private archives",
                    "iterations": iterations, "load_ms": load_ms, "baseline_memory": baseline,
                    "memory": memory, "second_process": peer_result,
                    "client_has_mlx": "mlx.core" in sys.modules,
                    "os_footprint": [line.strip() for line in footprint.stdout.splitlines()
                                     if line.startswith("Physical footprint")],
                    "latency_ms": {kind: {"samples": len(values), "p95": sorted(values)[math.ceil(len(values) * .95) - 1]}
                                   for kind, values in timings.items() if values},
                    "checks": {
                        "one_worker_for_two_processes": state["pid"] == peer_result["worker_pid"],
                        "no_model_in_clients": "mlx.core" not in sys.modules and not peer_result["client_has_mlx"],
                        "no_retained_scratch_cache": memory["cache_bytes"] == 0,
                        "peak_active_within_budget": memory["peak_active_bytes"] <= max_peak_gib * 1024 ** 3,
                        "active_returns_to_baseline": memory["active_bytes"] <= baseline["active_bytes"] + 64 * 1024 ** 2,
                    },
                    "max_peak_active_gib": max_peak_gib,
                }
                return result
            finally:
                client.close()
                with suppress(OSError, ValueError, RuntimeError):
                    runtime.request(runtime.read_state(Path(temporary)), "/stop", {})
                until = time.monotonic() + 5
                while runtime.state_path(Path(temporary)).exists() and time.monotonic() < until:
                    time.sleep(.05)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=24)
    parser.add_argument("--max-peak-gib", type=float, default=8, help="MLX active-allocation acceptance budget, not a hard OS limit")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        parser.error("This probe requires macOS, the embedding extra, and the already cached MLX model")
    if not 2 <= args.iterations <= 10000 or not 0 < args.max_peak_gib <= 128:
        parser.error("iterations must be 2–10000 and max-peak-gib must be >0 and <=128")
    result = measure(args.iterations, args.max_peak_gib)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return int(not all(result["checks"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
