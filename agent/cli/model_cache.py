"""Bounded, credential-scoped model metadata cache. No startup network traffic."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from agent.runtime.paths import state_path
from .provider_connections import private_json_write

CACHE_TTL = 3600
MAX_MODELS = 5000


def cache_path(endpoint) -> Path:
    identity = "\0".join((endpoint.id, endpoint.profile.base_url.rstrip("/"), endpoint.profile.api_key()))
    digest = hashlib.sha256(identity.encode()).hexdigest()
    root = Path(os.environ["AGENT_MODEL_CACHE_DIR"]).expanduser() if os.getenv("AGENT_MODEL_CACHE_DIR") else state_path("model-cache")
    return root / f"{digest}.json"


def read_cache(endpoint, *, fresh: bool = False) -> tuple[list[dict], float] | None:
    path = cache_path(endpoint)
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        stamp = float(data["fetched_at"])
        age = time.time() - stamp
        if data.get("version") != 1 or age < 0 or (fresh and age >= CACHE_TTL):
            return None
        items = data["models"]
        if not isinstance(items, list) or len(items) > MAX_MODELS or not all(isinstance(i, dict) for i in items):
            return None
        return items, stamp
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_cache(endpoint, items: list[dict]) -> float:
    stamp = time.time()
    # Cache only normalized discovery metadata, never headers or arbitrary API payloads.
    private_json_write(cache_path(endpoint), {"version": 1, "fetched_at": stamp, "models": items})
    return stamp
