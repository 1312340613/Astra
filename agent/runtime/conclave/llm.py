"""Resolve the chairperson LLM used by Conclave without mutating the active model."""

from __future__ import annotations

import logging
from urllib.parse import urlparse
from typing import Any, Awaitable, Callable

from agent.cli.models import model_profiles
from agent.runtime.deepseek import is_deepseek_model
from agent.runtime.llm import LLMClient, LLMConfig


logger = logging.getLogger(__name__)


def _is_local_endpoint(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def resolve_chairperson_llm(
    active_llm: Any,
    model_spec: str,
    *,
    fallback_to_active: bool = False,
) -> Any:
    """Return the active LLM or a temporary client for a configured model.

    Model-menu values use the canonical ``provider::model`` catalog key, while
    older Conclave configuration used a bare profile name.  Accept both at this
    boundary so a valid value selected in the UI cannot poison future tool calls.
    """
    spec = (model_spec or "active").strip()
    active_model = str(getattr(getattr(active_llm, "config", None), "model", ""))
    if spec in {"active", "current"} or spec == active_model:
        if active_llm is None:
            raise ValueError("Conclave chairperson 'active' requires an active LLM")
        return active_llm

    profiles = model_profiles()
    profile = profiles.get(spec)
    if profile is None:
        # The TUI persists canonical catalog keys such as
        # ``deepseek::deepseek-flash``.  Resolve them without live provider
        # discovery so Conclave startup remains deterministic and fast.
        from agent.cli.model_catalog import configured_model_catalog

        entry = configured_model_catalog().resolve_persisted(spec)
        if entry is not None:
            profile = entry.profile
    if profile is None and fallback_to_active and active_llm is not None:
        logger.warning(
            "Conclave chairperson model is unavailable; using active model spec=%s",
            spec,
        )
        return active_llm
    if profile is None:
        matches = [
            item
            for item in profiles.values()
            if item.model_id == spec
            or f"{item.provider}/{item.model_id or spec}" == spec
        ]
        profile = matches[0] if len(matches) == 1 else None
    if profile is None:
        available = ", ".join(sorted(profiles))
        raise ValueError(
            f"Unknown Conclave chairperson model '{spec}'. "
            f"Use 'active' or a configured profile: {available}"
        )
    if profile.provider == "conclave":
        if fallback_to_active and active_llm is not None:
            logger.warning("Conclave chairperson cannot be Conclave; using active model")
            return active_llm
        raise ValueError("Conclave cannot use itself as its chairperson model")

    api_key = profile.api_key()
    if not api_key and (_is_local_endpoint(profile.base_url) or not profile.api_key_env):
        api_key = "local"
    if not api_key and fallback_to_active and active_llm is not None:
        logger.warning(
            "Conclave chairperson credentials are unavailable; using active model spec=%s env=%s",
            spec,
            profile.api_key_env,
        )
        return active_llm
    if not api_key:
        raise ValueError(
            f"{profile.api_key_env} is not configured for Conclave model '{spec}'"
        )
    resolved_model = profile.model_id or spec
    return LLMClient(LLMConfig(
        provider=profile.provider,
        model=resolved_model,
        api_key=api_key,
        base_url=profile.base_url,
        capabilities=profile.capabilities,
        # Conclave needs a concise evidence synthesis, not a maximal hidden
        # chain of thought.  Max effort exhausted 8192 completion tokens in a
        # real run while returning no visible answer, then forced a slow
        # fallback to the active model.
        reasoning_effort=("low" if is_deepseek_model(resolved_model) else None),
        **profile.generation_settings(),
    ))


def chairperson_chat(
    active_llm: Any,
    model_spec: str,
    max_tokens: int,
    *,
    request_role: str = "chairperson",
) -> Callable[[list[dict]], Awaitable[str]]:
    """Build a resilient callback expected by ``Conclave.run``.

    A separately configured chairperson is an optimization, not a reason to
    discard an otherwise valid research run.  If it cannot be resolved or its
    request fails, continue inside the same tool call with the active model.
    """
    llm = resolve_chairperson_llm(
        active_llm,
        model_spec,
        fallback_to_active=True,
    )
    token_limit = max(256, min(8192, int(max_tokens)))
    use_active = llm is active_llm

    async def chat(messages: list[dict]) -> str:
        nonlocal use_active
        selected = active_llm if use_active else llm

        async def request(client: Any) -> str:
            response = await client.chat_limited(
                messages,
                max_tokens=token_limit,
                temperature=0.3,
            )
            content = (
                str(response.get("content") or "")
                if isinstance(response, dict)
                else str(response or "")
            )
            if content.strip():
                return content
            if isinstance(response, dict):
                logger.warning(
                    "Conclave %s returned no visible answer model=%s "
                    "finish_reason=%s reasoning_chars=%s usage=%s",
                    request_role,
                    getattr(getattr(client, "config", None), "model", model_spec),
                    response.get("finish_reason") or "",
                    len(str(response.get("reasoning_content") or "")),
                    response.get("usage") or {},
                )
            raise RuntimeError(f"Conclave {request_role} returned no visible answer")

        try:
            return await request(selected)
        except Exception:
            if selected is active_llm or active_llm is None:
                raise
            logger.warning(
                "Conclave %s request failed; retrying once with active model spec=%s",
                request_role,
                model_spec,
                exc_info=True,
            )
            use_active = True
            return await request(active_llm)

    return chat
