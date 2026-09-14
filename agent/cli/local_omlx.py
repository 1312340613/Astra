"""Safe macOS-local oMLX provider discovery."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import DEFAULT_CONTEXT_LIMIT, ModelProfile


@dataclass(frozen=True)
class LocalOMLXProviderSpec:
    provider_id: str
    label: str
    profile: ModelProfile


def _load_settings(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _read_api_key(path: Path) -> str:
    payload = _load_settings(path)
    auth = payload.get("auth") if payload is not None else None
    if not isinstance(auth, dict):
        return ""
    value = auth.get("api_key")
    return value.strip() if isinstance(value, str) else ""


def local_omlx_provider_spec(
    *,
    platform_name: str | None = None,
    settings_path: Path | None = None,
) -> LocalOMLXProviderSpec | None:
    """Return a loopback provider for this user's local macOS oMLX server."""
    if (platform_name or sys.platform) != "darwin":
        return None
    path = settings_path or Path(
        os.getenv("OMLX_SETTINGS_PATH", str(Path.home() / ".omlx" / "settings.json"))
    ).expanduser()
    payload = _load_settings(path)
    if payload is None:
        return None
    server = payload.get("server")
    if not isinstance(server, dict):
        return None
    port = _positive_int(server.get("port"))
    if port is None or port > 65_535:
        return None
    sampling = payload.get("sampling")
    raw_context = (
        sampling.get("max_context_window")
        if isinstance(sampling, dict)
        else None
    )
    context_limit = _positive_int(raw_context) or DEFAULT_CONTEXT_LIMIT
    label = f"oMLX Local · {port}"
    profile = ModelProfile(
        base_url=f"http://127.0.0.1:{port}/v1",
        context_limit=context_limit,
        api_key_env="OMLX_API_KEY",
        capabilities=frozenset({"tools", "reasoning", "streaming"}),
        model_id="",
        catalog_provider="omlx-local",
        provider_label=label,
        api_key_resolver=lambda: _read_api_key(path),
    )
    return LocalOMLXProviderSpec(
        provider_id="omlx-local",
        label=label,
        profile=profile,
    )
