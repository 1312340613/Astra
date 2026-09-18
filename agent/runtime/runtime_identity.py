"""Small, model-safe loaded-runtime identity; never reads environment/config."""

import hashlib
import os
import time
import uuid
from pathlib import Path

from .execution_limits import execution_limits

_ROOT = Path(__file__).resolve().parents[2]
_SOURCES = (
    "agent/runtime/context.py",
    "agent/runtime/prompts.py",
    "agent/runtime/system_suffix.py",
    "agent/runtime/worker.py",
    "agent/runtime/agent_team.py",
    "agent/runtime/execution_limits.py",
    "agent/runtime/runtime_identity.py",
    "agent/runtime/tools/code.py",
    "agent/runtime/tools/delegate.py",
    "agent/runtime/tools/registry.py",
)


def _source_digest() -> str:
    digest = hashlib.sha256()
    for name in _SOURCES:
        digest.update(name.encode())
        try:
            digest.update((_ROOT / name).read_bytes())
        except OSError:
            digest.update(b"<unavailable>")
    return digest.hexdigest()


# Freeze at import/startup, not at /doctor invocation after files have changed.
_LOADED = {
    "pid": os.getpid(),
    "run_id": uuid.uuid4().hex,
    "loaded_at": time.time(),
    "source_sha256": _source_digest(),
}


def runtime_identity(*, compare_disk: bool = False) -> dict:
    result = {**_LOADED, "execution_limits": execution_limits()}
    if compare_disk:
        current = _source_digest()
        result.update(disk_source_sha256=current, source_changed=current != _LOADED["source_sha256"])
    return result
