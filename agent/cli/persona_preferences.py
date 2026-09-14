"""Persist and restore the selected system prompt/persona profile."""

import json
from pathlib import Path

from agent.runtime.persona import PersonaState
from agent.runtime.prompts import DEFAULT_PROMPT_PROFILE, get_prompt_profile, resolve_prompt_profile_name

from .model_preferences import model_settings_path


def _read_settings(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_selected_persona() -> str | None:
    """Load a valid persisted persona name, ignoring missing/corrupt settings."""
    data = _read_settings(model_settings_path())
    selected = data.get("selected_persona")
    return resolve_prompt_profile_name(selected) if isinstance(selected, str) else None


def save_selected_persona(name: str) -> Path:
    """Atomically persist the selected persona profile for future launches."""
    resolved = resolve_prompt_profile_name(name)
    if resolved is None:
        raise ValueError(f"Unknown persona: {name}")
    name = resolved

    path = model_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_settings(path)
    profile = get_prompt_profile(name)
    data["selected_persona"] = name
    data["persona_definition_version"] = profile.version
    data["persona_state_revision"] = 0
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def resolve_startup_persona(env_persona: str | None = None) -> str:
    """Prefer persisted persona, then environment, then the default profile."""
    selected = load_selected_persona()
    if selected:
        return selected
    resolved = resolve_prompt_profile_name(env_persona)
    if resolved:
        return resolved
    return DEFAULT_PROMPT_PROFILE


def startup_system_prompt(env_persona: str | None = None) -> tuple[str, str]:
    """Return (persona_name, system_prompt) for agent startup."""
    name, prompt, _ = startup_persona(env_persona)
    return name, prompt


def startup_persona(env_persona: str | None = None) -> tuple[str, str, PersonaState]:
    """Return selected persona, assembled prompt, and versioned state metadata."""
    name = resolve_startup_persona(env_persona)
    profile = get_prompt_profile(name)
    data = _read_settings(model_settings_path())
    selected = data.get("selected_persona")
    if isinstance(selected, str) and selected != name and resolve_prompt_profile_name(selected) == name:
        save_selected_persona(name)
        data = _read_settings(model_settings_path())
    revision = data.get("persona_state_revision", 0) if data.get("selected_persona") == name else 0
    if not isinstance(revision, int) or revision < 0:
        revision = 0
    state = profile.default_state(state_revision=revision)
    return name, profile.system_prompt(state), state
