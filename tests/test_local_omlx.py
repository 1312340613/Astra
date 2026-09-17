import asyncio
import json

import pytest

from agent.cli import model_catalog
from agent.cli.model_catalog import ProviderEndpoint, discover_model_catalog, provider_endpoints
from agent.cli.local_omlx import local_omlx_provider_spec
from agent.cli.models import DEFAULT_CONTEXT_LIMIT, ModelProfile


def _write_settings(path, *, port=8000, context_limit=262_144, api_key="local-secret"):
    path.write_text(
        json.dumps({
            "server": {"host": "0.0.0.0", "port": port},
            "sampling": {"max_context_window": context_limit},
            "auth": {"api_key": api_key},
        }),
        encoding="utf-8",
    )


def test_local_omlx_builds_loopback_provider_from_macos_settings(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    _write_settings(settings)
    monkeypatch.delenv("OMLX_API_KEY", raising=False)

    spec = local_omlx_provider_spec(
        platform_name="darwin",
        settings_path=settings,
    )

    assert spec is not None
    assert spec.provider_id == "omlx-local"
    assert spec.label == "oMLX Local · 8000"
    assert spec.profile.base_url == "http://127.0.0.1:8000/v1"
    assert spec.profile.context_limit == 262_144
    assert spec.profile.capabilities == frozenset({"tools", "reasoning", "streaming"})
    assert spec.profile.api_key() == "local-secret"
    assert "local-secret" not in repr(spec)


def test_local_omlx_environment_key_overrides_settings(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    _write_settings(settings)
    monkeypatch.setenv("OMLX_API_KEY", "environment-secret")

    spec = local_omlx_provider_spec(platform_name="darwin", settings_path=settings)

    assert spec is not None
    assert spec.profile.api_key() == "environment-secret"


def test_local_omlx_resolver_rereads_key_and_fails_closed(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    _write_settings(settings, api_key="first-secret")
    monkeypatch.delenv("OMLX_API_KEY", raising=False)
    spec = local_omlx_provider_spec(platform_name="darwin", settings_path=settings)
    assert spec is not None

    _write_settings(settings, api_key="second-secret")
    assert spec.profile.api_key() == "second-secret"

    settings.unlink()
    assert spec.profile.api_key() == ""


def test_local_omlx_uses_environment_settings_path(tmp_path, monkeypatch):
    settings = tmp_path / "custom-settings.json"
    _write_settings(settings, port=8123)
    monkeypatch.setenv("OMLX_SETTINGS_PATH", str(settings))

    spec = local_omlx_provider_spec(platform_name="darwin")

    assert spec is not None
    assert spec.profile.base_url == "http://127.0.0.1:8123/v1"


def test_local_omlx_uses_default_context_when_setting_is_not_positive(tmp_path):
    settings = tmp_path / "settings.json"
    _write_settings(settings, context_limit=0)

    spec = local_omlx_provider_spec(platform_name="darwin", settings_path=settings)

    assert spec is not None
    assert spec.profile.context_limit == DEFAULT_CONTEXT_LIMIT


@pytest.mark.parametrize("platform_name", ["win32", "linux"])
def test_local_omlx_is_not_auto_detected_outside_macos(
    tmp_path,
    platform_name,
):
    settings = tmp_path / "settings.json"
    _write_settings(settings)

    assert local_omlx_provider_spec(
        platform_name=platform_name,
        settings_path=settings,
    ) is None


@pytest.mark.parametrize("port", [True, False, 0, -1, 65_536, "not-a-port"])
def test_local_omlx_rejects_invalid_port(tmp_path, port):
    settings = tmp_path / "settings.json"
    _write_settings(settings, port=port)

    assert local_omlx_provider_spec(
        platform_name="darwin",
        settings_path=settings,
    ) is None


def test_local_omlx_ignores_missing_or_invalid_settings(tmp_path):
    missing = tmp_path / "missing.json"
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")

    assert local_omlx_provider_spec(
        platform_name="darwin",
        settings_path=missing,
    ) is None
    assert local_omlx_provider_spec(
        platform_name="darwin",
        settings_path=invalid,
    ) is None


def test_provider_endpoints_appends_detected_local_omlx(tmp_path, monkeypatch):
    catalog_file = tmp_path / "models.yaml"
    catalog_file.write_text("version: 1\nmodels: {}\n", encoding="utf-8")
    settings = tmp_path / "settings.json"
    _write_settings(settings)
    spec = local_omlx_provider_spec(platform_name="darwin", settings_path=settings)
    assert spec is not None
    monkeypatch.setenv("AGENT_MODELS_FILE", str(catalog_file))
    monkeypatch.setenv("AGENT_USER_MODELS_FILE", str(tmp_path / "missing.yaml"))
    monkeypatch.setattr(model_catalog, "local_omlx_provider_spec", lambda: spec)

    endpoints = provider_endpoints()

    assert [(item.id, item.label) for item in endpoints] == [
        ("omlx-local", "oMLX Local · 8000")
    ]


def test_explicit_local_provider_overrides_auto_detection(tmp_path, monkeypatch):
    catalog_file = tmp_path / "models.yaml"
    catalog_file.write_text(
        """version: 1
providers:
  omlx-local:
    label: Explicit oMLX
    base_url: http://127.0.0.1:9000/v1
    api_key_env: EXPLICIT_KEY
    context_limit: 131072
models: {}
""",
        encoding="utf-8",
    )
    settings = tmp_path / "settings.json"
    _write_settings(settings)
    spec = local_omlx_provider_spec(platform_name="darwin", settings_path=settings)
    assert spec is not None
    monkeypatch.setenv("AGENT_MODELS_FILE", str(catalog_file))
    monkeypatch.setenv("AGENT_USER_MODELS_FILE", str(tmp_path / "missing.yaml"))
    monkeypatch.setattr(model_catalog, "local_omlx_provider_spec", lambda: spec)

    endpoints = provider_endpoints()

    assert len(endpoints) == 1
    assert endpoints[0].id == "omlx-local"
    assert endpoints[0].label == "Explicit oMLX"
    assert endpoints[0].profile.base_url == "http://127.0.0.1:9000/v1"


def test_explicit_windows_lan_provider_is_unchanged_without_auto_detection(
    tmp_path,
    monkeypatch,
):
    catalog_file = tmp_path / "models.yaml"
    catalog_file.write_text(
        """version: 1
providers:
  omlx:
    label: OMLX LAN · 8000
    base_url: http://192.0.2.10:8000/v1
    api_key_env: OMLX_API_KEY
    context_limit: 262144
models: {}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_MODELS_FILE", str(catalog_file))
    monkeypatch.setenv("AGENT_USER_MODELS_FILE", str(tmp_path / "missing.yaml"))
    monkeypatch.setattr(model_catalog, "local_omlx_provider_spec", lambda: None)

    endpoints = provider_endpoints()

    assert len(endpoints) == 1
    assert endpoints[0].id == "omlx"
    assert endpoints[0].label == "OMLX LAN · 8000"
    assert endpoints[0].profile.base_url == "http://192.0.2.10:8000/v1"


def test_local_omlx_discovery_authenticates_and_redacts_catalog_event(
    tmp_path,
    monkeypatch,
):
    settings = tmp_path / "settings.json"
    _write_settings(settings)
    spec = local_omlx_provider_spec(platform_name="darwin", settings_path=settings)
    assert spec is not None
    endpoint = ProviderEndpoint(spec.provider_id, spec.label, spec.profile)
    monkeypatch.delenv("OMLX_API_KEY", raising=False)
    monkeypatch.setattr(model_catalog, "provider_endpoints", lambda: (endpoint,))
    monkeypatch.setattr(
        model_catalog,
        "model_profiles",
        lambda: {
            "local-model": ModelProfile(
                base_url="http://127.0.0.1:8000/v1",
                context_limit=131_072,
            )
        },
    )

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [{"id": "local-model", "max_model_len": 262_144}]
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers):
            assert url == "http://127.0.0.1:8000/v1/models"
            assert headers == {"Authorization": "Bearer local-secret"}
            return Response()

    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", lambda timeout: Client())

    catalog = asyncio.run(discover_model_catalog())

    entry = catalog.resolve("omlx-local::local-model")
    assert entry is not None
    assert entry.profile.context_limit == 262_144
    assert entry.profile.api_key() == "local-secret"
    event = entry.to_event()
    assert event["provider"] == "oMLX Local · 8000"
    assert "local-secret" not in json.dumps(event)


def test_local_omlx_offline_fallback_preserves_settings_key_resolver(
    tmp_path,
    monkeypatch,
):
    settings = tmp_path / "settings.json"
    _write_settings(settings)
    spec = local_omlx_provider_spec(platform_name="darwin", settings_path=settings)
    assert spec is not None
    endpoint = ProviderEndpoint(spec.provider_id, spec.label, spec.profile)
    monkeypatch.delenv("OMLX_API_KEY", raising=False)
    monkeypatch.setattr(model_catalog, "provider_endpoints", lambda: (endpoint,))
    monkeypatch.setattr(
        model_catalog,
        "model_profiles",
        lambda: {
            "cached-local": ModelProfile(
                base_url="http://127.0.0.1:8000/v1",
                context_limit=131_072,
            )
        },
    )

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers):
            raise model_catalog.httpx.ConnectError("offline")

    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", lambda timeout: Client())

    catalog = asyncio.run(discover_model_catalog())

    entry = catalog.resolve("omlx-local::cached-local")
    assert entry is not None
    assert entry.profile.api_key() == "local-secret"
