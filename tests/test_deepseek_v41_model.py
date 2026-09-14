import pytest

from agent.cli import model_catalog, models
from agent.runtime.llm import _messages_for_capabilities


MODELS = ["deepseek-flash"]


@pytest.fixture
def profiles(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MODELS_FILE", str(models.DEFAULT_MODELS_PATH))
    monkeypatch.setenv("AGENT_USER_MODELS_FILE", str(tmp_path / "absent.yaml"))
    return models.model_profiles()


@pytest.mark.parametrize("MODEL", MODELS)
def test_v41_profile_uses_shared_deepseek_connection(MODEL, profiles, monkeypatch):
    profile = profiles[MODEL]
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    assert profile.base_url == "https://api.deepseek.com"
    assert profile.provider == "openai-compatible"
    assert profile.api_key_env == "DEEPSEEK_API_KEY"
    assert profile.api_key() == "test-only-key"
    assert profile.context_limit == 1_000_000
    assert profile.capabilities == frozenset({"tools", "reasoning", "vision", "streaming"})
    assert profile.vision_detail == "original"
    assert profile.vision_preprocess is not None


@pytest.mark.parametrize("MODEL", MODELS)
def test_v41_catalog_is_selectable_offline(MODEL, profiles, monkeypatch):
    monkeypatch.setattr(model_catalog, "provider_endpoints", lambda: ())
    monkeypatch.setattr(model_catalog, "model_profiles", lambda: profiles)
    catalog = model_catalog.configured_model_catalog()
    entry = catalog.resolve(MODEL)
    assert entry is not None
    assert entry.key == f"deepseek::{MODEL}"
    assert catalog.resolve_persisted(entry.key) == entry
    assert entry.to_event()["context_limit"] == 1_000_000


@pytest.mark.parametrize("MODEL", MODELS)
def test_v41_request_preserves_image_input(MODEL, profiles):
    profile = profiles[MODEL]
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://example.test/image.png"}},
    ]}]
    prepared = _messages_for_capabilities(
        messages, profile.capabilities, vision_detail=profile.vision_detail,
    )
    assert prepared[0]["content"][0]["image_url"] == {
        "url": "https://example.test/image.png", "detail": "original",
    }
