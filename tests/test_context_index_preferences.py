import json
from types import SimpleNamespace

import pytest

from agent.cli.context_index_commands import execute_context_index_command
from agent.cli.context_index_preferences import (
    ContextIndexPreferences,
    load_context_index_preferences,
    save_context_index_preferences,
)


class FakeBroker:
    def __init__(self):
        self.mode = "off"
        self.invalidations = 0

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.invalidations += 1

    def format_last_trace(self) -> str:
        return "No Context Index decision has been recorded."


def test_missing_and_invalid_settings_fail_closed(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(path))

    assert load_context_index_preferences() == ContextIndexPreferences("off", 900)

    path.write_text('{"context_index_mode":"surprise","context_index_char_budget":99999}')

    assert load_context_index_preferences() == ContextIndexPreferences(
        "off", 2000, invalid_mode="surprise"
    )


def test_save_preserves_unrelated_settings(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"theme":"nord"}')
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(path))

    save_context_index_preferences("session", 700)

    assert json.loads(path.read_text()) == {
        "theme": "nord",
        "context_index_mode": "session",
        "context_index_char_budget": 700,
    }


@pytest.mark.parametrize(
    ("argv", "expected"),
    [(["on"], "all"), (["all"], "all"), (["session"], "session"), (["off"], "off")],
)
def test_command_maps_modes_and_invalidates_handles(tmp_path, monkeypatch, argv, expected):
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    broker = FakeBroker()

    output, error = execute_context_index_command(
        SimpleNamespace(context_index_broker=broker), argv
    )

    assert error == ""
    assert broker.mode == expected
    assert broker.invalidations == 1
    assert expected.upper() in output


def test_status_reports_saved_and_active_modes(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"context_index_mode":"session"}')
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(path))
    broker = FakeBroker()
    broker.mode = "all"

    output, error = execute_context_index_command(
        SimpleNamespace(context_index_broker=broker), ["status"]
    )

    assert error == ""
    assert "Saved mode: SESSION" in output
    assert "Active mode: ALL" in output


def test_why_delegates_to_broker(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))

    output, error = execute_context_index_command(
        SimpleNamespace(context_index_broker=FakeBroker()), ["why"]
    )

    assert error == ""
    assert output == "No Context Index decision has been recorded."


@pytest.mark.parametrize("argv", [[], ["status"], ["why"], ["on", "now"], ["shadow"], ["unknown"]])
def test_commands_without_a_broker_are_safe_and_malformed_usage_is_stable(monkeypatch, tmp_path, argv):
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))

    output, error = execute_context_index_command(
        SimpleNamespace(context_index_broker=None), argv
    )

    if argv in ([], ["status"]):
        assert error == ""
        assert "Saved mode: OFF" in output
        assert "Active mode: unavailable" in output
    elif argv == ["why"]:
        assert error == ""
        assert output == "No Context Index decision has been recorded."
    else:
        assert output == ""
        assert error == "Usage: /context-index [on|off|session|all|status|why] or /context-index feedback R1 useful|irrelevant|outdated"
