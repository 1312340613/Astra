"""Optional local code must be absent in public installs and isolated when present."""

from pathlib import Path

import pytest

from agent.runtime.local_mode import LocalMode, load_local_mode, local_mode_path
from agent.runtime.session_store import SessionStore

FIXTURE = Path(__file__).parent / "fixtures/local_mode.py"


def test_no_local_module_does_not_publish_a_command_or_read_sessions(monkeypatch):
    mode = load_local_mode(None)
    assert not mode.enabled and not mode.active
    assert not mode.matches("/anything")
    assert mode.menu_event() == {"type": "local_mode_info", "definition": None, "sessions": []}
    with pytest.raises(RuntimeError, match="No local mode"):
        mode.session_path("arbitrary")


def test_default_local_module_is_in_installation_state_not_current_project(monkeypatch, tmp_path):
    monkeypatch.delenv("ASTRA_LOCAL_MODE_FILE", raising=False)
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "installation"))
    project = tmp_path / "project"
    (project / ".astra").mkdir(parents=True)
    (project / ".astra/local_mode.py").write_text("raise RuntimeError('must never execute')")
    monkeypatch.chdir(project)
    assert local_mode_path() == tmp_path / "installation/local_mode.py"
    assert not load_local_mode(None).enabled


@pytest.mark.parametrize("body", [
    "raise RuntimeError('PRIVATE_SENTINEL')",
    "API_VERSION = 999\ndef create_mode(agent): return None",
    "API_VERSION = 1\ndef create_mode(agent): return None",
])
def test_broken_local_module_reports_configuration_error_without_private_contents(monkeypatch, tmp_path, body):
    path = tmp_path / "module.py"
    path.write_text(body)
    monkeypatch.setenv("ASTRA_LOCAL_MODE_FILE", str(path))
    with pytest.raises(ValueError, match="Cannot load local mode") as error:
        load_local_mode(None)
    assert "PRIVATE_SENTINEL" not in str(error.value)


def test_extension_loads_its_own_command_and_retains_session_namespace(monkeypatch, tmp_path):
    from agent.cli import sessions

    monkeypatch.setenv("ASTRA_LOCAL_MODE_FILE", str(FIXTURE))
    monkeypatch.setattr(sessions, "SESSION_DIR", tmp_path)
    mode = load_local_mode(None)
    assert mode.matches("/fixture") and mode.matches("/fixture new")
    assert not mode.matches("/fixture-other")
    path = mode.session_path("existing")
    SessionStore(path).save({"system_prompt": "private", "messages": [{"role": "user", "content": "retained"}]})
    event = mode.menu_event()
    assert event["definition"]["command"] == "/fixture"
    assert event["sessions"] == [{"name": "existing", "messages": 1, "current": False}]
    assert path == tmp_path / "fixture/existing.json"
    for name in ("../work", "/tmp/escape", ".."):
        with pytest.raises(ValueError):
            mode.session_path(name)
    assert mode.new_session_name().startswith("fixture_")
    assert sessions.list_sessions() == []


@pytest.mark.parametrize("command,namespace", [("/bar", "fixture"), ("/retry", "fixture"), ("/custom", "bar"), ("/custom", "../escape")])
def test_local_extension_cannot_shadow_builtins_or_session_namespaces(command, namespace):
    with pytest.raises(ValueError):
        LocalMode(object(), command=command, label="Example", description="Example", namespace=namespace)
