import json

from agent.cli.vision_tile_preferences import (
    execute_vision_tiles_command,
    load_vision_tiles_enabled,
    save_vision_tiles_enabled,
)
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.vision_policy import VisionPreprocessPolicy


class FakeConfig:
    model = "deepseek-v4-flash-vision-exp"
    vision_preprocess = VisionPreprocessPolicy()


class FakeAgent:
    def __init__(self):
        self.vision_tiles_enabled = True
        self.llm = type("LLM", (), {"config": FakeConfig()})()
        self.invalidations = 0

    def set_vision_tiles_enabled(self, enabled: bool) -> None:
        self.vision_tiles_enabled = enabled
        self.invalidations += 1


def test_missing_setting_defaults_on(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))

    assert load_vision_tiles_enabled() is True


def test_corrupt_setting_defaults_on(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(path))

    assert load_vision_tiles_enabled() is True


def test_invalid_utf8_setting_defaults_on(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    path.write_bytes(b"\xff")
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(path))

    assert load_vision_tiles_enabled() is True


def test_save_preserves_unrelated_settings(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"selected_model":"keep","sandbox_mode":"local"}', encoding="utf-8")
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(path))

    saved_path = save_vision_tiles_enabled(False)

    assert saved_path == path
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "selected_model": "keep",
        "sandbox_mode": "local",
        "vision_tiles_enabled": False,
    }
    assert not path.with_suffix(".json.tmp").exists()


def test_command_applies_immediately_and_reports_inactive_model(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    agent = FakeAgent()

    output, error = execute_vision_tiles_command(agent, ["off"])

    assert error == ""
    assert agent.vision_tiles_enabled is False
    assert agent.invalidations == 1
    assert "Vision tiles: OFF" in output
    assert "provider downscaling" in output

    agent.llm.config.vision_preprocess = None
    output, error = execute_vision_tiles_command(agent, ["on"])

    assert error == ""
    assert "Inactive: this model has no tiled preprocessing policy." in output
    assert "Active for current model: NO" in output

    output, error = execute_vision_tiles_command(agent, ["off"])

    assert error == ""
    assert "large images are sent directly" in output


def test_command_recovers_invalid_utf8_settings_with_atomic_replace(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    path.write_bytes(b"\xff")
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(path))
    agent = FakeAgent()

    output, error = execute_vision_tiles_command(agent, ["off"])

    assert error == ""
    assert "Vision tiles: OFF" in output
    assert agent.vision_tiles_enabled is False
    assert json.loads(path.read_text(encoding="utf-8")) == {"vision_tiles_enabled": False}
    assert not path.with_suffix(".json.tmp").exists()


def test_command_preserves_lone_surrogate_setting_with_atomic_replace(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(r'{"note":"\ud800"}', encoding="utf-8")
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(path))
    agent = FakeAgent()

    output, error = execute_vision_tiles_command(agent, ["off"])

    assert error == ""
    assert "Vision tiles: OFF" in output
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "note": "\ud800",
        "vision_tiles_enabled": False,
    }
    assert not path.with_suffix(".json.tmp").exists()


def test_status_reports_active_policy_dimensions_and_budgets(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    agent = FakeAgent()

    output, error = execute_vision_tiles_command(agent, ["show"])

    assert error == ""
    assert "Vision tiles: ON" in output
    assert "768x768" in output
    assert "64px overlap" in output
    assert "12 images/request" in output
    assert "41943040 bytes" in output
    assert "DeepSeek vision models only" in output
    assert "Active for current model: YES" in output


def test_command_rejects_unknown_and_extra_arguments(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    agent = FakeAgent()

    for args in (["maybe"], ["on", "now"]):
        output, error = execute_vision_tiles_command(agent, args)

        assert output == ""
        assert error == "Usage: /vision-tiles [on|off]"
        assert agent.invalidations == 0


def test_react_agent_toggle_invalidates_the_tool_schema_cache():
    agent = object.__new__(ReActAgent)
    invalidations: list[bool] = []
    agent.invalidate_tool_schema_cache = lambda: invalidations.append(True)

    agent.set_vision_tiles_enabled(False)

    assert agent.vision_tiles_enabled is False
    assert invalidations == [True]


def test_react_agent_starts_with_vision_tiles_enabled():
    agent = ReActAgent("test", object(), ToolRegistry())  # type: ignore[arg-type]

    assert agent.vision_tiles_enabled is True
