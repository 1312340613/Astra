from agent.cli import main as cli
from agent.cli.models import ModelProfile
import pytest


@pytest.mark.parametrize("override", ["test-profile", "provider/test-model"])
def test_backend_override_uses_profile_fields_without_starting_services(monkeypatch, override):
    profile = ModelProfile(base_url="http://127.0.0.1:9/v1", context_limit=4096,
                           model_id="provider/test-model")
    monkeypatch.setattr(cli, "model_profiles", lambda: {
        "deepseek-flash": ModelProfile(base_url="http://old.invalid", context_limit=4096),
        "earlier-alias": ModelProfile(base_url="http://wrong.invalid", context_limit=4096, model_id="test-profile"),
        "test-profile": profile,
    })
    monkeypatch.setattr(cli, "resolve_startup_model", lambda *_: ("unused", "http://old.invalid"))
    monkeypatch.setattr(cli, "setup_logging", lambda *_: None)
    monkeypatch.setattr(cli, "load_project_env", lambda *_: None)
    monkeypatch.setattr(cli, "configure_tracing", lambda: None)
    monkeypatch.setattr(cli.os, "system", lambda *_: 0)
    monkeypatch.setattr(cli.sys, "argv", ["astra"])
    monkeypatch.setenv("ASTRA_BACKEND", override)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_CONTEXT_LIMIT", raising=False)
    received = []

    async def capture(config, *_):
        received.append(config)

    monkeypatch.setattr(cli, "_async_init", capture)
    cli.main()
    assert received[0].base_url == profile.base_url
    assert received[0].model == "provider/test-model"

    assert received[0].context_limit == profile.context_limit


def test_configured_context_limit_keeps_explicit_environment_override(monkeypatch):
    monkeypatch.setenv("LLM_CONTEXT_LIMIT", "8192")
    assert cli._resolve_context_limit_simple("provider/alias", 4096) == 8192
    monkeypatch.delenv("LLM_CONTEXT_LIMIT")
    assert cli._resolve_context_limit_simple("provider/alias", 4096) == 4096
