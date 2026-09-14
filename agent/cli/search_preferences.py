"""Persist and restore the selected web-search provider."""

import json
import os
from pathlib import Path

from .model_preferences import model_settings_path


SEARCH_PROVIDERS = ("auto", "exa", "searxng")


def _read_settings(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_selected_search_provider() -> str | None:
    selected = _read_settings(model_settings_path()).get("search_provider")
    return selected if isinstance(selected, str) and selected in SEARCH_PROVIDERS else None


def resolve_startup_search_provider() -> str:
    selected = load_selected_search_provider()
    if selected:
        return selected
    configured = os.getenv("WEB_SEARCH_PROVIDER", "auto").strip().lower()
    return configured if configured in SEARCH_PROVIDERS else "auto"


def save_selected_search_provider(provider: str) -> Path:
    provider = provider.strip().lower()
    if provider not in SEARCH_PROVIDERS:
        raise ValueError(f"Unknown search provider: {provider}")

    path = model_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_settings(path)
    data["search_provider"] = provider
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
