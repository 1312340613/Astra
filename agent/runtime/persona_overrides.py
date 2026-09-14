"""Optional, installation-local persona data; never read from a working project."""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

from .paths import state_path

MAX_CONFIG_BYTES = 1024 * 1024


def local_persona_path() -> Path:
    configured = os.environ.get("ASTRA_PERSONA_FILE", "").strip()
    return Path(configured).expanduser().resolve() if configured else state_path("persona.local.json")


@lru_cache(maxsize=1)
def _read_config(path: Path, mtime_ns: int, size: int) -> dict:
    # Metadata participates in the cache key so a local edit is seen on the next
    # profile lookup. This cache contains one installation's bounded config.
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("schema") != 1:
            raise ValueError
        if set(data) - {"schema", "profiles", "mode_prompts"}:
            raise ValueError
        profiles = data.get("profiles", [])
        modes = data.get("mode_prompts", {})
        if not isinstance(profiles, list) or not all(isinstance(p, dict) for p in profiles):
            raise ValueError
        if (not isinstance(modes, dict) or not all(isinstance(k, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,47}", k) for k in modes)
                or not all(isinstance(p, str) and p.strip() for p in modes.values())):
            raise ValueError
        return data
    except (OSError, ValueError, UnicodeError):
        # Do not include content or silently fall back to a different identity.
        raise ValueError("Cannot load local persona configuration; check ASTRA_PERSONA_FILE or persona.local.json.") from None


def local_persona_config() -> dict:
    path = local_persona_path()
    try:
        info = path.stat()
    except FileNotFoundError:
        return {}
    except OSError:
        raise ValueError("Cannot read local persona configuration metadata.") from None
    if info.st_size > MAX_CONFIG_BYTES:
        raise ValueError("Local persona configuration exceeds 1 MiB.")
    return _read_config(path, info.st_mtime_ns, info.st_size)


def local_mode_prompt(mode: str, default: str) -> str:
    return local_persona_config().get("mode_prompts", {}).get(mode, default)
