"""Dynamic model discovery grouped by OpenAI-compatible provider endpoints."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx

from agent.runtime.deepseek import LEGACY_DEEPSEEK_MODELS, canonical_deepseek_model

from .local_omlx import local_omlx_provider_spec
from .models import (
    DEFAULT_CONTEXT_LIMIT,
    DEFAULT_MODELS_PATH,
    PACKAGED_MODELS_PATH,
    USER_MODELS_PATH,
    ModelProfile,
    _load_yaml,
    _profile_from_data,
    model_profiles,
)


@dataclass(frozen=True)
class ProviderEndpoint:
    id: str
    label: str
    profile: ModelProfile
    discovery_timeout: float = 4.0


@dataclass(frozen=True)
class CatalogEntry:
    key: str
    model_id: str
    provider_id: str
    provider_label: str
    base_url: str
    profile: ModelProfile

    def to_event(self, *, current: bool = False) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.model_id,
            "provider": self.provider_label,
            "provider_id": self.provider_id,
            "endpoint": self.base_url,
            "context_limit": self.profile.context_limit,
            "current": current,
        }


@dataclass(frozen=True)
class ModelCatalog:
    entries: tuple[CatalogEntry, ...]
    errors: dict[str, str]

    @property
    def profiles(self) -> dict[str, ModelProfile]:
        return {entry.key: entry.profile for entry in self.entries}

    def resolve(self, value: str | None) -> CatalogEntry | None:
        if not value:
            return None
        exact = next((entry for entry in self.entries if entry.key == value), None)
        if exact:
            return exact
        matches = [entry for entry in self.entries if entry.model_id == value]
        if not matches:
            provider_id, _, model_id = value.rpartition("::")
            model_id = model_id or value
            matches = [
                entry for entry in self.entries
                if (not provider_id or provider_id == entry.provider_id)
                and canonical_deepseek_model(model_id, entry.base_url) == entry.model_id
            ]
        return matches[0] if len(matches) == 1 else None

    def resolve_persisted(self, value: str | None) -> CatalogEntry | None:
        """Resolve a saved dynamic model even when initial discovery is offline.

        The provider remains the trust boundary: a removed provider invalidates
        its saved selection, while a configured provider can recreate the last
        model profile without requiring a successful startup `/models` request.
        """
        resolved = self.resolve(value)
        if resolved or not value or "::" not in value:
            return resolved
        provider_id, model_id = value.split("::", 1)
        if not provider_id or not model_id:
            return None
        if provider_id == "configured":
            # Migrate selections saved before static remote profiles were
            # split into provider-specific API sources.
            static = model_profiles()
            static_profile = static.get(model_id)
            if static_profile is None and model_id in LEGACY_DEEPSEEK_MODELS:
                migrated = self.resolve(model_id)
                if migrated is not None and canonical_deepseek_model(model_id, migrated.base_url) != model_id:
                    return migrated
            if static_profile is not None:
                if static_profile.catalog_provider != "configured":
                    migrated_key = (
                        f"{static_profile.catalog_provider}::"
                        f"{static_profile.model_id or model_id}"
                    )
                    migrated = self.resolve(migrated_key)
                    if migrated is not None:
                        return migrated
                    return CatalogEntry(
                        key=migrated_key,
                        model_id=static_profile.model_id or model_id,
                        provider_id=static_profile.catalog_provider,
                        provider_label=static_profile.provider_label,
                        base_url=static_profile.base_url,
                        profile=replace(
                            static_profile,
                            model_id=static_profile.model_id or model_id,
                        ),
                    )
                endpoint = next(
                    (
                        item
                        for item in provider_endpoints()
                        if item.profile.base_url.rstrip("/")
                        == static_profile.base_url.rstrip("/")
                    ),
                    None,
                )
                if endpoint is not None:
                    migrated_key = f"{endpoint.id}::{static_profile.model_id or model_id}"
                    migrated = self.resolve(migrated_key)
                    if migrated is not None:
                        return migrated
                    profile = replace(
                        static_profile,
                        base_url=endpoint.profile.base_url,
                        api_key_env=endpoint.profile.api_key_env,
                        api_key_resolver=endpoint.profile.api_key_resolver,
                        model_id=static_profile.model_id or model_id,
                        catalog_provider=endpoint.id,
                        provider_label=endpoint.label,
                    )
                    return CatalogEntry(
                        key=migrated_key,
                        model_id=profile.model_id,
                        provider_id=endpoint.id,
                        provider_label=endpoint.label,
                        base_url=endpoint.profile.base_url,
                        profile=profile,
                    )
        endpoint = next((item for item in provider_endpoints() if item.id == provider_id), None)
        if endpoint is None:
            return None
        model_id = canonical_deepseek_model(model_id, endpoint.profile.base_url)
        value = f"{provider_id}::{model_id}"
        static = model_profiles()
        override = static.get(model_id)
        same_endpoint = (
            override is not None
            and override.base_url.rstrip("/") == endpoint.profile.base_url.rstrip("/")
        )
        template = override if override is not None and same_endpoint else endpoint.profile
        profile = replace(
            template,
            base_url=endpoint.profile.base_url,
            api_key_env=endpoint.profile.api_key_env,
            api_key_resolver=endpoint.profile.api_key_resolver,
            model_id=model_id,
            catalog_provider=endpoint.id,
            provider_label=endpoint.label,
        )
        return CatalogEntry(
            key=value,
            model_id=model_id,
            provider_id=endpoint.id,
            provider_label=endpoint.label,
            base_url=endpoint.profile.base_url,
            profile=profile,
        )


def configured_model_catalog() -> ModelCatalog:
    """Build a startup-safe catalog without probing provider endpoints.

    Live discovery remains available for an explicit model-menu refresh.  The
    persisted selection can also rebuild a dynamic entry through
    ``resolve_persisted`` when its provider is still configured.
    """
    endpoints = provider_endpoints()
    entries: dict[str, CatalogEntry] = {}

    for name, configured_profile in model_profiles().items():
        model_id = configured_profile.model_id or name
        endpoint = next(
            (
                item
                for item in endpoints
                if item.profile.base_url.rstrip("/")
                == configured_profile.base_url.rstrip("/")
            ),
            None,
        )
        if endpoint is not None:
            provider_id = endpoint.id
            provider_label = endpoint.label
            base_url = endpoint.profile.base_url
            profile = replace(
                configured_profile,
                base_url=base_url,
                api_key_env=endpoint.profile.api_key_env,
                api_key_resolver=endpoint.profile.api_key_resolver,
                model_id=model_id,
                catalog_provider=provider_id,
                provider_label=provider_label,
            )
        else:
            provider_id = configured_profile.catalog_provider or "configured"
            provider_label = configured_profile.provider_label or "Configured"
            base_url = configured_profile.base_url
            profile = replace(configured_profile, model_id=model_id)

        key = f"{provider_id}::{model_id}"
        # A legacy user alias must not replace the canonical bundled profile.
        if key in entries and name != model_id:
            continue
        entries[key] = CatalogEntry(
            key=key,
            model_id=model_id,
            provider_id=provider_id,
            provider_label=provider_label,
            base_url=base_url,
            profile=profile,
        )

    return ModelCatalog(tuple(entries.values()), {})


def parse_model_command_argument(command: str) -> str:
    """Return the complete model selector after ``/model``.

    Dynamic provider keys may contain spaces (for example a llama.cpp model
    alias). Command tokenization must not silently truncate those keys.
    Matching outer quotes are accepted for the plain CLI as well.
    """
    parts = str(command).strip().split(maxsplit=1)
    if not parts or parts[0].lower() != "/model" or len(parts) == 1:
        return ""
    argument = parts[1].strip()
    if len(argument) >= 2 and argument[0] == argument[-1] and argument[0] in {'"', "'"}:
        argument = argument[1:-1].strip()
    return argument


def _configured_paths() -> tuple[Path, Path]:
    bundled = DEFAULT_MODELS_PATH if DEFAULT_MODELS_PATH.exists() else PACKAGED_MODELS_PATH
    configured = Path(os.getenv("AGENT_MODELS_FILE", str(bundled))).expanduser()
    user = Path(os.getenv("AGENT_USER_MODELS_FILE", str(USER_MODELS_PATH))).expanduser()
    return configured, user


def provider_endpoints() -> tuple[ProviderEndpoint, ...]:
    raw_providers: dict[str, dict[str, Any]] = {}
    configured, user = _configured_paths()
    for path in (configured, user):
        if path == user and user.resolve() == configured.resolve():
            continue
        payload = _load_yaml(path)
        providers = payload.get("providers", {})
        if isinstance(providers, dict):
            raw_providers.update({
                str(name): data for name, data in providers.items() if isinstance(data, dict)
            })
    endpoints = []
    for provider_id, raw in raw_providers.items():
        if raw.get("enabled", True) is False:
            continue
        profile = _profile_from_data(f"provider:{provider_id}", raw)
        endpoints.append(ProviderEndpoint(
            id=provider_id,
            label=str(raw.get("label", provider_id.upper())),
            discovery_timeout=max(0.5, float(raw.get("discovery_timeout", 4.0))),
            profile=replace(
                profile,
                catalog_provider=provider_id,
                provider_label=str(raw.get("label", provider_id.upper())),
            ),
        ))
    local_omlx = local_omlx_provider_spec()
    if local_omlx is not None and local_omlx.provider_id not in raw_providers:
        endpoints.append(ProviderEndpoint(
            id=local_omlx.provider_id,
            label=local_omlx.label,
            profile=local_omlx.profile,
        ))
    return tuple(endpoints)


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def model_context_limit(item: dict[str, Any]) -> int | None:
    keys = (
        "context_length", "max_context_length", "max_model_len",
        "max_sequence_length", "max_position_embeddings", "n_ctx",
    )
    stack = [item]
    while stack:
        current = stack.pop()
        if not isinstance(current, dict):
            continue
        for key in keys:
            value = _positive_int(current.get(key))
            if value:
                return value
        for nested_key in ("metadata", "meta", "config", "parameters", "model_config", "details"):
            nested = current.get(nested_key)
            if isinstance(nested, dict):
                stack.append(nested)
    return None


def _model_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        data = payload.get("models")
    return [item for item in (data or []) if isinstance(item, dict)]


async def discover_model_catalog(timeout: float = 4.0) -> ModelCatalog:
    endpoints = provider_endpoints()
    static = model_profiles()
    errors: dict[str, str] = {}

    async def discover(endpoint: ProviderEndpoint) -> list[CatalogEntry]:
        headers = {}
        api_key = endpoint.profile.api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        url = endpoint.profile.base_url.rstrip("/") + "/models"
        try:
            async with httpx.AsyncClient(timeout=max(timeout, endpoint.discovery_timeout)) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                items = _model_items(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            errors[endpoint.id] = f"{type(exc).__name__}: {exc}"
            return []

        entries = []
        seen = set()
        for item in items:
            model_id = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
            if not model_id:
                continue
            model_id = canonical_deepseek_model(model_id, endpoint.profile.base_url)
            if model_id in seen:
                continue
            seen.add(model_id)
            override = static.get(model_id)
            same_endpoint = (
                override is not None
                and override.base_url.rstrip("/") == endpoint.profile.base_url.rstrip("/")
            )
            template = override if override is not None and same_endpoint else endpoint.profile
            context_limit = model_context_limit(item) or template.context_limit or DEFAULT_CONTEXT_LIMIT
            profile = replace(
                template,
                base_url=endpoint.profile.base_url,
                api_key_env=endpoint.profile.api_key_env,
                api_key_resolver=endpoint.profile.api_key_resolver,
                model_id=model_id,
                context_limit=context_limit,
                catalog_provider=endpoint.id,
                provider_label=endpoint.label,
            )
            entries.append(CatalogEntry(
                key=f"{endpoint.id}::{model_id}",
                model_id=model_id,
                provider_id=endpoint.id,
                provider_label=endpoint.label,
                base_url=endpoint.profile.base_url,
                profile=profile,
            ))
        return entries

    tasks = {endpoint.id: asyncio.create_task(discover(endpoint)) for endpoint in endpoints}
    if tasks:
        done, pending = await asyncio.wait(tasks.values(), timeout=timeout)
    else:
        done, pending = set(), set()
    for endpoint in endpoints:
        task = tasks[endpoint.id]
        if task in pending:
            errors[endpoint.id] = f"Discovery timed out after {timeout:.1f}s"
            task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    discovered = [
        tasks[endpoint.id].result()
        for endpoint in endpoints
        if tasks[endpoint.id] in done and not tasks[endpoint.id].cancelled()
    ]
    entries = [entry for group in discovered for entry in group]

    # Keep configured models visible for a provider that is temporarily offline.
    # A later menu refresh replaces these fallback entries with live discovery.
    for endpoint in endpoints:
        if endpoint.id not in errors:
            continue
        endpoint_url = endpoint.profile.base_url.rstrip("/")
        for name, configured_profile in static.items():
            if configured_profile.base_url.rstrip("/") != endpoint_url:
                continue
            model_id = configured_profile.model_id or name
            profile = replace(
                configured_profile,
                base_url=endpoint.profile.base_url,
                api_key_env=endpoint.profile.api_key_env,
                api_key_resolver=endpoint.profile.api_key_resolver,
                model_id=model_id,
                catalog_provider=endpoint.id,
                provider_label=endpoint.label,
            )
            entries.append(CatalogEntry(
                key=f"{endpoint.id}::{model_id}",
                model_id=model_id,
                provider_id=endpoint.id,
                provider_label=endpoint.label,
                base_url=endpoint.profile.base_url,
                profile=profile,
            ))

    dynamic_urls = {endpoint.profile.base_url.rstrip("/") for endpoint in endpoints}
    for name, profile in static.items():
        if profile.base_url.rstrip("/") in dynamic_urls:
            continue
        provider_id = profile.catalog_provider or "configured"
        provider_label = profile.provider_label or "Configured"
        entries.append(CatalogEntry(
            key=f"{provider_id}::{name}",
            model_id=profile.model_id or name,
            provider_id=provider_id,
            provider_label=provider_label,
            base_url=profile.base_url,
            profile=replace(profile, model_id=profile.model_id or name),
        ))
    return ModelCatalog(tuple({entry.key: entry for entry in entries}.values()), errors)
