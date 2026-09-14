"""Persist the selected night-bar output mode."""

from __future__ import annotations

import json

from .model_preferences import model_settings_path


BAR_OUTPUT_MODES = frozenset({"atomic", "stream"})
DEFAULT_BAR_OUTPUT_MODE = "atomic"


def load_bar_output_mode() -> str:
    """Return the saved mode, falling back safely for missing/corrupt settings."""
    path = model_settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_BAR_OUTPUT_MODE
    mode = data.get("bar_output_mode") if isinstance(data, dict) else None
    return mode if mode in BAR_OUTPUT_MODES else DEFAULT_BAR_OUTPUT_MODE


def save_bar_output_mode(mode: str):
    """Atomically persist a validated output mode without replacing other settings."""
    normalized = str(mode).strip().lower()
    if normalized not in BAR_OUTPUT_MODES:
        raise ValueError(f"Unknown bar output mode: {mode}")

    path = model_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            data.update(existing)
    except (OSError, json.JSONDecodeError):
        pass

    data["bar_output_mode"] = normalized
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path
