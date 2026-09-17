"""Aliyun's own model ID must retain its route and documented token capacity."""

from dataclasses import replace

import pytest

from agent.cli import model_cache, model_catalog, models


@pytest.fixture
def endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MODELS_FILE", str(models.DEFAULT_MODELS_PATH))
    monkeypatch.setenv("AGENT_USER_MODELS_FILE", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("AGENT_MODEL_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("QWEN38_BASE_URL", raising=False)
    endpoint = model_catalog.ProviderEndpoint(
        "qwen38", "Model Studio Token Plan",
        models.ModelProfile(
            "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            32_768, api_key_env="QWEN38_API_KEY", capabilities=frozenset({"streaming"}),
        ), inferred=True,
    )
    monkeypatch.setattr(model_catalog, "provider_endpoints", lambda: (endpoint,))
    return endpoint


def test_id_only_cached_model_resolves_with_documented_capacity(endpoint):
    model_cache.write_cache(endpoint, [{"id": "deepseek-v4.1-flash"}])
    entry = model_catalog.ModelCatalog((), {}).resolve_persisted("qwen38::deepseek-v4.1-flash")
    assert entry is not None
    assert entry.metadata_known
    assert entry.model_id == "deepseek-v4.1-flash"
    assert entry.base_url == endpoint.profile.base_url
    assert entry.profile.api_key_env == "QWEN38_API_KEY"
    assert entry.profile.context_limit == 1_000_000
    assert entry.profile.max_tokens == 393_216
    assert models.prompt_token_budget(entry.profile.context_limit, entry.profile.max_tokens) == 500_000
    assert {"tools", "reasoning", "vision", "streaming"} <= entry.profile.capabilities


def test_selection_survives_offline_restart_without_cached_metadata(endpoint):
    entry = model_catalog.configured_model_catalog().resolve_persisted("qwen38::deepseek-v4.1-flash")
    assert entry is not None
    assert entry.metadata_known
    assert entry.profile.context_limit == 1_000_000
    assert entry.model_id == "deepseek-v4.1-flash"


def test_explicit_server_limit_remains_authoritative(endpoint):
    entry, = model_catalog.entries_from_items(
        endpoint, [{"id": "deepseek-v4.1-flash", "context_length": 64_000}], source="live",
    )
    assert entry.profile.context_limit == 64_000
    assert entry.profile.max_tokens <= 32_000


def test_capacity_is_not_guessed_for_unrelated_endpoint_or_unknown_model(endpoint):
    custom = replace(endpoint, profile=replace(endpoint.profile, base_url="https://custom.example/v1"))
    entry, = model_catalog.entries_from_items(custom, [{"id": "deepseek-v4.1-flash"}], source="live")
    unknown, = model_catalog.entries_from_items(endpoint, [{"id": "unknown-model"}], source="live")
    assert entry.profile.context_limit == unknown.profile.context_limit == 32_768
    assert not entry.metadata_known and not unknown.metadata_known
