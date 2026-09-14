"""Model connection creation, probing, and atomic switching."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from .model_preferences import save_selected_model
from .models import ModelProfile, prompt_token_budget, save_model_profile


@dataclass(frozen=True)
class ConnectionProbe:
    ok: bool
    message: str
    models: tuple[str, ...] = ()


def is_local_url(url: str) -> bool:
    return urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}


async def probe_profile(
    name: str,
    profile: ModelProfile,
    timeout: float = 5.0,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> ConnectionProbe:
    resolved_key = profile.api_key() if api_key is None else api_key
    resolved_url = base_url or profile.base_url
    if not resolved_key and not is_local_url(resolved_url):
        return ConnectionProbe(False, f"Missing API key environment variable: {profile.api_key_env}")
    headers = {"Authorization": f"Bearer {resolved_key}"} if resolved_key else {}
    url = resolved_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return ConnectionProbe(False, f"{type(exc).__name__}: {exc}")
    data = payload.get("data", []) if isinstance(payload, dict) else []
    models = tuple(
        str(item.get("id") or item.get("name"))
        for item in data
        if isinstance(item, dict) and (item.get("id") or item.get("name"))
    )
    visible = ", ".join(models[:5]) if models else "endpoint returned no model ids"
    return ConnectionProbe(True, f"Connected to {name} @ {resolved_url}; {visible}", models)


def sync_context_budget(agent, profile: ModelProfile) -> int:
    """Apply a model profile's current context metadata to a live agent."""
    budget = prompt_token_budget(profile.context_limit, agent.llm.config.max_tokens)
    agent.context.max_prompt_tokens = budget
    agent.llm.config.context_limit = profile.context_limit
    return budget


def switch_to_profile(agent, name: str, profile: ModelProfile, *, valid_models: set[str] | None = None):
    """Build the new provider first, then commit config and context changes."""
    api_key = profile.api_key()
    if not api_key:
        if is_local_url(profile.base_url) or not profile.api_key_env:
            api_key = "local"
        else:
            raise ValueError(
                f"Missing API key environment variable: {profile.api_key_env}"
            )
    runtime_model = profile.model_id or name
    agent.llm.switch_model(
        runtime_model,
        profile.base_url,
        provider_name=profile.provider,
        api_key=api_key,
        capabilities=profile.capabilities,
        **profile.generation_settings(),
    )
    sync_context_budget(agent, profile)
    return save_selected_model(name, valid_models=valid_models)


async def create_probe_and_switch(
    agent,
    name: str,
    base_url: str,
    api_key_env: str = "LLM_API_KEY",
    provider: str = "openai-compatible",
) -> tuple[ConnectionProbe, object | None]:
    profile = ModelProfile(
        base_url=base_url,
        context_limit=128_000,
        provider=provider,
        api_key_env=api_key_env,
        capabilities=frozenset({"tools", "streaming"}),
    )
    probe = await probe_profile(name, profile)
    if not probe.ok:
        return probe, None
    save_model_profile(name, profile)
    settings_path = switch_to_profile(agent, name, profile)
    return probe, settings_path
