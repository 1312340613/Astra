"""Offline coverage of retired IDs, native request flags and summary defaults."""

import asyncio
import json

import httpx
import pytest

from agent.cli import model_catalog, models
from agent.cli.model_preferences import resolve_startup_model
from agent.runtime.activity_recorder.summarizer import summary_config
from agent.runtime.deepseek import LEGACY_DEEPSEEK_MODELS
from agent.runtime.hindsight_provider import HindsightMemoryProvider
from agent.runtime.llm import LLMConfig, OpenAICompatibleProvider


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MODELS_FILE", str(models.DEFAULT_MODELS_PATH))
    monkeypatch.setenv("AGENT_USER_MODELS_FILE", str(tmp_path / "models.yaml"))
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(model_catalog, "provider_endpoints", lambda: ())


def test_bundled_deepseek_profiles():
    assert {name for name, profile in models.model_profiles().items()
            if profile.base_url == "https://api.deepseek.com"} == {
        "deepseek-flash", "deepseek-v4-pro",
    }


@pytest.mark.parametrize("legacy", sorted(LEGACY_DEEPSEEK_MODELS))
@pytest.mark.parametrize("prefix", ["", "deepseek::", "configured::"])
def test_saved_v4_selection_migrates_offline(legacy, prefix, tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"selected_model": prefix + legacy}))
    catalog = model_catalog.configured_model_catalog()
    entry = catalog.resolve_persisted(prefix + legacy)
    assert entry is not None
    assert entry.key == "deepseek::deepseek-flash"
    assert "vision" in entry.profile.capabilities
    assert entry.profile.vision_preprocess is not None
    assert resolve_startup_model("unused", "https://unused.invalid") == (
        "deepseek-flash", "https://api.deepseek.com",
    )


@pytest.mark.parametrize("legacy", sorted(LEGACY_DEEPSEEK_MODELS))
def test_env_and_transport_migrate_official_v4_ids(legacy):
    assert resolve_startup_model(legacy, "https://api.deepseek.com")[0] == "deepseek-flash"
    assert LLMConfig(model=legacy, base_url="https://api.deepseek.com/v1").model == "deepseek-flash"


def test_official_v4_pro_keeps_its_own_identity():
    assert resolve_startup_model("deepseek-v4-pro", "https://api.deepseek.com") == (
        "deepseek-v4-pro", "https://api.deepseek.com",
    )
    assert LLMConfig(model="deepseek-v4-pro", base_url="https://api.deepseek.com").model == "deepseek-v4-pro"


def test_saved_v4_pro_selection_resolves_to_pro(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"selected_model": "deepseek::deepseek-v4-pro"}))
    entry = model_catalog.configured_model_catalog().resolve_persisted("deepseek::deepseek-v4-pro")
    assert entry is not None
    assert entry.key == "deepseek::deepseek-v4-pro"
    assert "vision" not in entry.profile.capabilities
    assert resolve_startup_model("unused", "https://unused.invalid") == (
        "deepseek-v4-pro", "https://api.deepseek.com",
    )


@pytest.mark.parametrize("base_url", ["http://localhost:8080/v1", "https://gateway.example/v1"])
def test_custom_endpoint_names_are_preserved(base_url, tmp_path):
    legacy = "deepseek-v4-pro"
    (tmp_path / "models.yaml").write_text(json.dumps({"models": {legacy: {
        "base_url": base_url, "context_limit": 8192,
    }}}))
    assert LLMConfig(model=legacy, base_url=base_url).model == legacy
    assert resolve_startup_model(legacy, base_url) == (legacy, base_url)
    assert model_catalog.configured_model_catalog().resolve(legacy).model_id == legacy


def test_custom_profile_model_id_wins_over_official_legacy_alias(tmp_path):
    (tmp_path / "models.yaml").write_text(json.dumps({"models": {"my-gateway": {
        "model_id": "deepseek-v4-flash", "base_url": "https://gateway.example/v1", "context_limit": 8192,
    }}}))
    assert resolve_startup_model("deepseek-v4-flash", "https://gateway.example/v1") == (
        "my-gateway", "https://gateway.example/v1",
    )
    assert model_catalog.configured_model_catalog().resolve("deepseek-v4-flash").provider_id == "configured"


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_flash_native_request_flags_and_tool_reasoning_history(effort):
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(model="deepseek-flash", thinking_mode="enabled", reasoning_effort=effort)
    messages = [{"role": "assistant", "content": "Earlier answer", "reasoning_content": "Earlier reasoning"}]
    kwargs = provider._completion_kwargs(messages, [{"type": "function", "function": {"name": "test"}}])
    assert kwargs["model"] == "deepseek-flash"
    assert kwargs["extra_body"]["thinking"] == {"type": "enabled"}
    assert kwargs["reasoning_effort"] == effort
    assert kwargs["messages"][0]["reasoning_content"] == "Earlier reasoning"


@pytest.mark.parametrize("legacy", [None, "deepseek-v4-flash"])
def test_activity_summary_sends_flash_without_thinking(monkeypatch, legacy):
    monkeypatch.delenv("ASTRA_ACTIVITY_SUMMARY_MODEL", raising=False)
    monkeypatch.delenv("ASTRA_ACTIVITY_SUMMARY_BASE_URL", raising=False)
    if legacy:
        monkeypatch.setenv("ASTRA_ACTIVITY_SUMMARY_MODEL", legacy)
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = summary_config()
    kwargs = provider._completion_kwargs([{"role": "user", "content": "Summarize"}], None)
    assert kwargs["model"] == "deepseek-flash"
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert kwargs["max_tokens"] == 4096


def test_hindsight_default_is_label_only(monkeypatch):
    monkeypatch.setenv("HINDSIGHT_ENABLED", "1")
    monkeypatch.delenv("HINDSIGHT_PROCESSING_MODEL", raising=False)
    provider = HindsightMemoryProvider.from_env()
    assert provider is not None
    assert provider.processing_model == "deepseek-flash"
    assert provider._client is None  # No server mutation or network request.


@pytest.mark.parametrize("offline", [False, True])
def test_discovery_deduplicates_retired_official_models_and_keeps_pro(monkeypatch, tmp_path, offline):
    (tmp_path / "models.yaml").write_text(json.dumps({"models": {
        "deepseek-v4-flash": {"base_url": "https://api.deepseek.com", "context_limit": 4096},
    }}))
    endpoint = model_catalog.ProviderEndpoint(
        "deepseek", "DeepSeek", models.model_profiles()["deepseek-flash"],
    )
    monkeypatch.setattr(model_catalog, "provider_endpoints", lambda: (endpoint,))

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, **kwargs):
            if offline:
                raise httpx.ConnectError("offline")
            return httpx.Response(200, json={"data": [
                {"id": name}
                for name in [*sorted(LEGACY_DEEPSEEK_MODELS), "deepseek-flash", "deepseek-v4-pro"]
            ]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", Client)
    for catalog in [model_catalog.configured_model_catalog(), asyncio.run(model_catalog.discover_model_catalog())]:
        entries = {e.model_id: e for e in catalog.entries if e.provider_id == "deepseek"}
        assert set(entries) == {"deepseek-flash", "deepseek-v4-pro"}
        assert entries["deepseek-flash"].profile.context_limit == 1_000_000
        assert entries["deepseek-flash"].profile.vision_preprocess is not None
        assert entries["deepseek-v4-pro"].profile.context_limit == 1_000_000
        assert "vision" not in entries["deepseek-v4-pro"].profile.capabilities
