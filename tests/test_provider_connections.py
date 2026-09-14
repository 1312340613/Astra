import asyncio
import json
import os
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from agent.cli import connections, model_cache, model_catalog, provider_connections
from agent.cli.model_catalog import ModelCatalog, ProviderEndpoint
from agent.cli.models import ModelProfile
from agent.runtime.llm import LLMClient, LLMConfig


@pytest.fixture
def endpoint(monkeypatch):
    profile = ModelProfile("https://provider.example/v1", 32768, api_key_env="",
                           api_key_resolver=lambda: "secret-one", capabilities=frozenset({"streaming"}))
    endpoint = ProviderEndpoint("sample", "Sample", profile, inferred=True)
    monkeypatch.setattr(model_catalog, "provider_endpoints", lambda: (endpoint,))
    monkeypatch.setattr(model_catalog, "model_profiles", lambda: {})
    return endpoint


def mock_http(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", lambda timeout: client(
        transport=httpx.MockTransport(handler), timeout=timeout))


def test_connect_saves_provider_not_model_or_default(tmp_path, monkeypatch):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "fresh-a"}, {"id": "fresh-b"}]})
    mock_http(monkeypatch, handler)
    settings = tmp_path / "settings.json"
    settings.write_text('{"selected_model":"old"}')
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings))
    provider_id, catalog = asyncio.run(connections.connect_provider("openrouter", api_key="private-key"))
    assert provider_id == "openrouter"
    assert [e.model_id for e in catalog.entries] == ["fresh-a", "fresh-b"]
    assert seen[0].headers["Authorization"] == "Bearer private-key"
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/models"
    assert json.loads(settings.read_text())["selected_model"] == "old"
    assert "private-key" not in json.dumps([e.to_event() for e in catalog.entries])
    files = list(provider_connections.connections_path().glob("*.json"))
    assert len(files) == 1
    if os.name != "nt":
        assert files[0].stat().st_mode & 0o777 == 0o600
    assert next(e for e in model_catalog.provider_endpoints() if e.id == provider_id).profile.api_key() == "private-key"


@pytest.mark.parametrize("status", [401, 403, 500])
def test_failed_connection_preserves_existing_key(monkeypatch, status):
    provider_id, record = provider_connections.connection_record("openrouter", api_key="previous")
    provider_connections.save_connection(provider_id, record)
    mock_http(monkeypatch, lambda r: httpx.Response(status, text="sensitive upstream details"))
    with pytest.raises(ValueError) as error:
        asyncio.run(connections.connect_provider("openrouter", api_key="bad-new"))
    assert "sensitive" not in str(error.value)
    assert provider_connections.read_connections()["openrouter"]["api_key"] == "previous"


def test_unsupported_listing_allows_manual_model(monkeypatch):
    mock_http(monkeypatch, lambda r: httpx.Response(404))
    provider_id, catalog = asyncio.run(connections.connect_provider("custom", base_url="https://service.example/v1", api_key="key"))
    assert catalog.error_codes[provider_id] == "listing_unsupported"
    entry = ModelCatalog((), {}).resolve_persisted(f"{provider_id}::not-listed")
    assert entry and entry.model_id == "not-listed"
    assert entry.source == "manual" and entry.metadata_known is False
    assert entry.profile.context_limit == 32768
    assert "vision" not in entry.profile.capabilities


@pytest.mark.parametrize("url", ["https://user:pass@host/v1", "https://host/v1?key=secret", "https://host/v1/models", "https://{WorkspaceId}.host/v1", "file:///tmp/test"])
def test_rejects_credential_bearing_or_invalid_urls(url):
    with pytest.raises(ValueError):
        provider_connections.connection_record("custom", base_url=url, api_key="key")


def test_presets_cannot_redirect_keys_and_plan_routes_do_not_share_auth(monkeypatch):
    with pytest.raises(ValueError):
        provider_connections.connection_record("deepseek", base_url="https://other.example/v1", api_key="key")
    routes = {r.id: r for r in provider_connections.ROUTES}
    assert routes["zhipu"].api_key_env != routes["zhipu-coding"].api_key_env
    assert routes["qwen38"].api_key_env != routes["qwen-token-sg"].api_key_env
    monkeypatch.setenv("MY_KEY", "value")
    _, record = provider_connections.connection_record("deepseek", api_key_env="MY_KEY")
    assert record["api_key"] == "" and record["api_key_env"] == "MY_KEY"
    assert "value" not in json.dumps(record)


def test_live_cache_scoping_expiry_and_normalization(endpoint, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"id": "dynamic", "context_length": 64000, "api_key": "never-cache"}]})
    mock_http(monkeypatch, handler)
    live = asyncio.run(model_catalog.discover_endpoint(endpoint))
    cached = asyncio.run(model_catalog.discover_endpoint(endpoint))
    assert len(calls) == 1
    assert live.entries[0].source == "live" and cached.entries[0].source == "cache"
    assert "never-cache" not in model_cache.cache_path(endpoint).read_text()
    assert "secret-one" not in model_cache.cache_path(endpoint).read_text()
    other_key = replace(endpoint, profile=replace(endpoint.profile, api_key_resolver=lambda: "secret-two"))
    assert model_cache.read_cache(other_key) is None
    other_url = replace(endpoint, profile=replace(endpoint.profile, base_url="https://elsewhere.example/v1"))
    assert model_cache.read_cache(other_url) is None
    stamp = json.loads(model_cache.cache_path(endpoint).read_text())["fetched_at"]
    monkeypatch.setattr(model_cache.time, "time", lambda: stamp + model_cache.CACHE_TTL + 1)
    assert model_cache.read_cache(endpoint, fresh=True) is None
    asyncio.run(model_catalog.discover_endpoint(endpoint))
    assert len(calls) == 2


def test_offline_cache_and_empty_live_list_are_distinct(endpoint, monkeypatch):
    model_cache.write_cache(endpoint, [{"id": "previous"}])
    mock_http(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    empty = asyncio.run(model_catalog.discover_endpoint(endpoint, force=True))
    assert empty.entries == () and empty.statuses[endpoint.id] == "live"
    assert model_cache.read_cache(endpoint)[0] == []
    def offline(request):
        raise httpx.ConnectError("secret network detail")
    mock_http_client = httpx.MockTransport(offline)
    # Retain the previously captured constructor through the existing factory.
    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", lambda timeout: httpx._client.AsyncClient(transport=mock_http_client, timeout=timeout))
    result = asyncio.run(model_catalog.discover_endpoint(endpoint, force=True))
    assert result.entries == () and result.statuses[endpoint.id] == "stale-cache"
    assert "secret" not in result.errors[endpoint.id]


def test_discovery_does_not_copy_other_models_generation_settings(endpoint, monkeypatch):
    known = replace(endpoint.profile, model_id="known", temperature=.6, top_p=.7,
                    context_limit=1_000_000, max_tokens=384000,
                    capabilities=frozenset({"vision", "reasoning", "tools", "streaming"}))
    monkeypatch.setattr(model_catalog, "model_profiles", lambda: {"known": known})
    entries = model_catalog.entries_from_items(endpoint, [{"id": "known"}, {"id": "unknown"}], source="live")
    assert entries[0].profile.max_tokens == 384000 and entries[0].profile.temperature == .6
    assert entries[1].profile.max_tokens == 4096 and entries[1].profile.temperature is None
    assert entries[1].profile.context_limit == 32768
    assert entries[1].metadata_known is False
    assert entries[1].profile.capabilities == frozenset({"streaming"})


def test_rich_metadata_filters_non_chat_and_preserves_tools_vision(endpoint):
    payload = {"data": [
        {"id": "vendor/chat", "context_length": 64000, "supported_parameters": ["tools", "reasoning"],
         "top_provider": {"max_completion_tokens": 16000},
         "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}},
        {"id": "image-only", "architecture": {"output_modalities": ["image"]}},
    ]}
    entries = model_catalog.entries_from_items(endpoint, model_catalog.normalize_model_items(payload), source="live")
    assert len(entries) == 1
    assert entries[0].profile.max_tokens == 16000
    assert {"tools", "vision", "reasoning"} <= entries[0].profile.capabilities


def test_provider_only_refresh_and_offline_startup(endpoint, monkeypatch):
    other = replace(endpoint, id="other", profile=replace(endpoint.profile, base_url="https://other.example/v1"))
    monkeypatch.setattr(model_catalog, "provider_endpoints", lambda: (endpoint, other))
    requests = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"data": [{"id": "one", "context_length": 12345}]})
    mock_http(monkeypatch, handler)
    asyncio.run(model_catalog.discover_model_catalog(provider_id=endpoint.id))
    assert requests == [endpoint.profile.base_url + "/models"]
    restored = model_catalog.configured_model_catalog().resolve_persisted("sample::one")
    assert restored and restored.profile.context_limit == 12345
    assert len(requests) == 1


def test_failed_selection_persistence_rolls_back_runtime(tmp_path, monkeypatch):
    config = LLMConfig(model="old", base_url="http://localhost:8999/v1", api_key="local")
    client = LLMClient(config, provider=object())
    agent = SimpleNamespace(llm=client, context=SimpleNamespace(max_prompt_tokens=234))
    previous_provider = client.provider
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(connections, "save_selected_model", fail)
    with pytest.raises(OSError):
        connections.switch_to_profile(agent, "new", ModelProfile("http://localhost:8998/v1", 12345, api_key_env=""), valid_models={"new"})
    assert client.config is config and client.provider is previous_provider
    assert agent.context.max_prompt_tokens == 234


def test_timeout_cancels_discovery(endpoint, monkeypatch):
    async def handler(request):
        await asyncio.sleep(30)
        return httpx.Response(200, json={"data": []})
    mock_http(monkeypatch, handler)
    result = asyncio.run(model_catalog.discover_endpoint(endpoint, force=True, timeout=.02))
    assert result.error_codes[endpoint.id] == "timeout"


def test_unconfigured_adapter_never_dispatches_requests(monkeypatch):
    from agent.runtime.llm import OpenAICompatibleProvider
    config = LLMConfig(model="unselected", base_url="https://unconfigured.example/v1", api_key="", connection_required=True)
    provider = OpenAICompatibleProvider(config)
    called = []
    async def unexpected():
        called.append(True)
    monkeypatch.setattr(provider, "_ensure_client_route", unexpected)
    with pytest.raises(ValueError, match="/connect"):
        asyncio.run(provider._create_completion({"messages": [{"role": "user", "content": "private"}]}))
    assert not called
