import json

from agent.cli import model_catalog
from agent.cli.model_catalog import (
    ModelCatalog,
    ProviderEndpoint,
    configured_model_catalog,
    parse_model_command_argument,
)
from agent.cli.model_preferences import read_selected_model
from agent.cli.models import ModelProfile


def test_reads_dynamic_persisted_model_without_static_catalog_validation(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"selected_model": "omlx::last-live-model"}), encoding="utf-8")
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings))

    assert read_selected_model() == "omlx::last-live-model"


def test_rebuilds_persisted_dynamic_model_from_configured_provider(monkeypatch):
    endpoint = ProviderEndpoint(
        id="omlx",
        label="OMLX · 8000",
        profile=ModelProfile(
            base_url="http://192.0.2.10:8000/v1",
            context_limit=262_144,
            api_key_env="OMLX_API_KEY",
            capabilities=frozenset({"tools", "vision", "streaming"}),
        ),
    )
    monkeypatch.setattr(model_catalog, "provider_endpoints", lambda: (endpoint,))
    monkeypatch.setattr(model_catalog, "model_profiles", lambda: {})

    entry = ModelCatalog((), {}).resolve_persisted("omlx::last-live-model")

    assert entry is not None
    assert entry.key == "omlx::last-live-model"
    assert entry.model_id == "last-live-model"
    assert entry.base_url == "http://192.0.2.10:8000/v1"
    assert entry.profile.context_limit == 262_144


def test_rebuilds_persisted_local_omlx_model_without_live_discovery(monkeypatch):
    endpoint = ProviderEndpoint(
        id="omlx-local",
        label="oMLX Local · 8000",
        profile=ModelProfile(
            base_url="http://127.0.0.1:8000/v1",
            context_limit=262_144,
            api_key_env="OMLX_API_KEY",
            capabilities=frozenset({"tools", "reasoning", "streaming"}),
            api_key_resolver=lambda: "local-secret",
        ),
    )
    monkeypatch.delenv("OMLX_API_KEY", raising=False)
    monkeypatch.setattr(model_catalog, "provider_endpoints", lambda: (endpoint,))
    monkeypatch.setattr(
        model_catalog,
        "model_profiles",
        lambda: {
            "last-live-model": ModelProfile(
                base_url="http://127.0.0.1:8000/v1",
                context_limit=131_072,
            )
        },
    )

    entry = ModelCatalog((), {}).resolve_persisted(
        "omlx-local::last-live-model"
    )

    assert entry is not None
    assert entry.key == "omlx-local::last-live-model"
    assert entry.base_url == "http://127.0.0.1:8000/v1"
    assert entry.profile.api_key() == "local-secret"


def test_configured_local_omlx_model_preserves_settings_key_resolver(monkeypatch):
    endpoint = ProviderEndpoint(
        id="omlx-local",
        label="oMLX Local · 8000",
        profile=ModelProfile(
            base_url="http://127.0.0.1:8000/v1",
            context_limit=262_144,
            api_key_env="OMLX_API_KEY",
            api_key_resolver=lambda: "local-secret",
        ),
    )
    monkeypatch.delenv("OMLX_API_KEY", raising=False)
    monkeypatch.setattr(model_catalog, "provider_endpoints", lambda: (endpoint,))
    monkeypatch.setattr(
        model_catalog,
        "model_profiles",
        lambda: {
            "configured-local": ModelProfile(
                base_url="http://127.0.0.1:8000/v1",
                context_limit=131_072,
            )
        },
    )

    entry = configured_model_catalog().resolve("omlx-local::configured-local")

    assert entry is not None
    assert entry.profile.api_key() == "local-secret"


def test_rejects_persisted_dynamic_model_when_provider_was_removed(monkeypatch):
    monkeypatch.setattr(model_catalog, "provider_endpoints", lambda: ())

    assert ModelCatalog((), {}).resolve_persisted("removed::old-model") is None


def test_migrates_legacy_configured_selection_to_provider_specific_source(monkeypatch):
    configured = ModelProfile(
        base_url="https://qwen.example/v1",
        context_limit=1_000_000,
        api_key_env="QWEN38_API_KEY",
        catalog_provider="qwen38",
        provider_label="Qwen 3.8 API",
    )
    monkeypatch.setattr(model_catalog, "provider_endpoints", lambda: ())
    monkeypatch.setattr(
        model_catalog,
        "model_profiles",
        lambda: {"qwen3.8-max": configured},
    )

    entry = ModelCatalog((), {}).resolve_persisted(
        "configured::qwen3.8-max"
    )

    assert entry is not None
    assert entry.key == "qwen38::qwen3.8-max"
    assert entry.profile.api_key_env == "QWEN38_API_KEY"
    assert entry.provider_label == "Qwen 3.8 API"


def test_model_command_preserves_dynamic_key_with_spaces():
    expected = "llamacpp::Fable Fusion 711"

    assert parse_model_command_argument(f"/model {expected}") == expected
    assert parse_model_command_argument(f'/model "{expected}"') == expected
    assert parse_model_command_argument("/model") == ""
    assert parse_model_command_argument(f"/persona {expected}") == ""


def test_configured_catalog_never_requires_live_provider_discovery(monkeypatch):
    endpoint = ProviderEndpoint(
        id="local",
        label="Local Runtime",
        profile=ModelProfile(
            base_url="http://localhost:9000/v1",
            context_limit=262_144,
            api_key_env="",
        ),
    )
    profile = ModelProfile(
        base_url=endpoint.profile.base_url,
        context_limit=131_072,
        model_id="saved-model",
    )
    monkeypatch.setattr(model_catalog, "provider_endpoints", lambda: (endpoint,))
    monkeypatch.setattr(model_catalog, "model_profiles", lambda: {"saved-model": profile})

    catalog = configured_model_catalog()

    assert catalog.errors == {}
    assert [entry.key for entry in catalog.entries] == ["local::saved-model"]
    assert catalog.entries[0].profile.context_limit == 131_072


def test_configured_deepseek_vision_selection_resolves_offline(monkeypatch, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"selected_model": "deepseek::deepseek-v4-flash-vision-exp"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings))

    selected = read_selected_model()
    entry = configured_model_catalog().resolve_persisted(selected)

    assert entry is not None
    assert entry.key == "deepseek::deepseek-flash"
    assert entry.model_id == "deepseek-flash"
    assert entry.profile.capabilities == frozenset(
        {"tools", "reasoning", "vision", "streaming"}
    )
