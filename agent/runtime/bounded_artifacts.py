"""Small bounded append-only artifacts for long-running local sessions."""

from __future__ import annotations

import os
import threading
from pathlib import Path


_LOCK = threading.RLock()


def positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def append_bounded_text(
    path: str | Path,
    text: str,
    *,
    max_bytes: int,
    backup_count: int = 2,
) -> None:
    """Append text and rotate whole files before crossing a byte budget.

    Unlike ``RotatingFileHandler`` this is suitable for structured JSONL
    artifacts outside Python's logging hierarchy. It opens the file only for
    one append, which keeps rotation reliable on Windows.
    """
    target = Path(path)
    limit = max(1, int(max_bytes))
    backups = max(0, int(backup_count))
    payload = str(text).encode("utf-8", errors="replace")
    # Never write a partial structured record: callers prefer losing one
    # diagnostic event over corrupting the entire JSONL stream.
    if len(payload) > limit:
        return

    with _LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        current = target.stat().st_size if target.is_file() else 0
        if current and current + len(payload) > limit:
            if backups <= 0:
                target.unlink(missing_ok=True)
            else:
                Path(f"{target}.{backups}").unlink(missing_ok=True)
                for index in range(backups - 1, 0, -1):
                    source = Path(f"{target}.{index}")
                    if source.exists():
                        os.replace(source, Path(f"{target}.{index + 1}"))
                os.replace(target, Path(f"{target}.1"))
        with target.open("ab") as handle:
            handle.write(payload)
