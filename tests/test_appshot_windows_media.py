"""Actual Windows handles and DLL; no mocked platform or user screenshots."""
import base64
import ctypes as C
import os
from pathlib import Path
import sys

import pytest

from agent.runtime.appshot_media import AppshotMediaError, AppshotMediaStore

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native media")
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg==")


@pytest.fixture(autouse=True)
def native(monkeypatch):
    helper = os.environ.get("ASTRA_COMPUTER_HELPER_PATH")
    if not helper:
        helper = str(Path(__file__).resolve().parents[1] / "native/windows-computer-helper/.build/x86_64-unknown-windows-msvc/debug/AstraWindowsComputerHelper.exe")
    if not Path(helper).with_name("AstraWindowsNative.dll").is_file():
        pytest.skip("build Windows helper and native storage first")
    monkeypatch.setenv("ASTRA_COMPUTER_HELPER_PATH", helper)


def test_windows_save_reload_rollback_and_delete(tmp_path):
    store = AppshotMediaStore(tmp_path / "s.json")
    first = store.put(PNG, 1, 1)
    second = store.put(PNG, 1, 1)
    assert AppshotMediaStore(tmp_path / "s.json").hydrate(first)["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()
    # A fresh store does not acquire rollback ownership over existing media.
    AppshotMediaStore(tmp_path / "s.json").rollback([first])
    store.rollback([second])
    assert list(store.path.iterdir()) == [store.path / (first["media_id"] + ".png")]
    store.delete()
    assert not store.path.exists()


def test_windows_session_history_rename_and_delete(tmp_path, monkeypatch):
    from agent.cli import sessions
    from agent.runtime.context import AgentContext

    monkeypatch.setattr(sessions, "SESSION_DIR", tmp_path)
    old, new = sessions.session_path("old"), sessions.session_path("new")
    store = AppshotMediaStore(old)
    ref = store.put(PNG, 1, 1)
    context = AgentContext()
    context.set_session(str(old))
    context.add_user([{"type": "appshot_image", "appshot_image": ref}])
    context.save()
    sessions.rename_session("old", "new")
    restored = AgentContext()
    restored.set_session(str(new))
    assert restored.load()
    assert "data:image/png;base64," in str(restored.get_prompt())
    assert not store.path.exists()
    sessions.delete_session("new")
    assert not AppshotMediaStore(new).path.exists()


def test_windows_replacement_survives_rollback(tmp_path):
    store = AppshotMediaStore(tmp_path / "s.json")
    ref = store.put(PNG, 1, 1)
    path = store.path / (ref["media_id"] + ".png")
    backend = store.windows()
    original = backend.owned[ref["media_id"]]
    from agent.runtime.appshot_windows_storage import FileProof, check

    with backend.directory() as pointer:
        check(backend.library.as_private_remove(pointer, path.name.encode(), C.byref(original)))
        replacement = FileProof()
        check(backend.library.as_private_publish(pointer, b"replacement.tmp", path.name.encode(), PNG, len(PNG), C.byref(replacement)))
    with pytest.raises(AppshotMediaError, match="authority"):
        store.rollback([ref])
    assert path.read_bytes() == PNG
    store.delete()


@pytest.mark.parametrize("attack", ["hardlink", "unknown", "tamper", "broad_acl"])
def test_windows_rejects_unsafe_media(tmp_path, attack):
    store = AppshotMediaStore(tmp_path / "s.json")
    ref = store.put(PNG, 1, 1)
    path = store.path / (ref["media_id"] + ".png")
    if attack == "hardlink":
        os.link(path, store.path / "alias")
    elif attack == "unknown":
        (store.path / "user-notes.txt").write_text("preserve")
    elif attack == "tamper":
        path.write_bytes(b"changed")
    else:
        import subprocess
        subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:(R)"], check=True, capture_output=True)
    if attack != "unknown":
        with pytest.raises(AppshotMediaError):
            store.hydrate(ref)
    if attack != "tamper":
        with pytest.raises(AppshotMediaError):
            store.delete()
        assert path.exists(), "preflight must preserve all media on invalid inventory"


def test_windows_rename_collision_preserves_both(tmp_path):
    a, b = AppshotMediaStore(tmp_path / "a.json"), AppshotMediaStore(tmp_path / "b.json")
    a_ref, b_ref = a.put(PNG, 1, 1), b.put(PNG, 1, 1)
    with pytest.raises(AppshotMediaError):
        a.rename_to(b)
    assert a.hydrate(a_ref) == b.hydrate(b_ref)
    a.delete(); b.delete()


def test_windows_open_pins_directory_and_ancestors(tmp_path):
    store = AppshotMediaStore(tmp_path / "s.json")
    ref = store.put(PNG, 1, 1)
    with store.windows().directory():
        with pytest.raises(OSError):
            store.path.rename(tmp_path / "replacement")
        with pytest.raises(OSError):
            tmp_path.rename(tmp_path.with_name(tmp_path.name + "-moved"))
    assert store.hydrate(ref)
    store.delete()


def test_windows_missing_optional_helper_does_not_break_plain_sessions(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRA_COMPUTER_HELPER_PATH", raising=False)
    store = AppshotMediaStore(tmp_path / "plain.json")
    store.delete()
    store.rename_to(AppshotMediaStore(tmp_path / "new.json"))


def test_windows_partial_publication_read_failure_rolls_back(tmp_path, monkeypatch):
    store = AppshotMediaStore(tmp_path / "s.json")
    backend = store.windows()
    def fail(*args):
        raise AppshotMediaError("injected")
    monkeypatch.setattr(backend, "_read", fail)
    with pytest.raises(AppshotMediaError, match="injected"):
        store.put(PNG, 1, 1)
    assert list(store.path.iterdir()) == []
    assert backend.owned == {}
    store.delete()


def test_windows_failed_load_preserves_live_prompt_and_history(tmp_path):
    from agent.runtime.context import AgentContext

    path = tmp_path / "bad.json"
    store = AppshotMediaStore(path)
    ref = store.put(PNG, 1, 1)
    saved = AgentContext(system_prompt="saved prompt")
    saved.set_session(str(path))
    saved.add_user([{"type": "appshot_image", "appshot_image": ref}])
    saved.save()
    (store.path / (ref["media_id"] + ".png")).write_bytes(b"tampered")
    live = AgentContext(system_prompt="keep live prompt")
    live.set_session(str(path))
    live.add_user("keep live message")
    before = (live.system_prompt, list(live.messages), live.persona_id)
    with pytest.raises(AppshotMediaError):
        live.load()
    assert (live.system_prompt, live.messages, live.persona_id) == before


def test_windows_config_is_install_root_not_current_directory(tmp_path, monkeypatch):
    from agent.runtime.appshot_windows_config import resolve_windows_appshot_helper

    assert resolve_windows_appshot_helper().is_file()
    for override in ("relative.exe", str(tmp_path / "missing.exe")):
        with pytest.raises(AppshotMediaError):
            resolve_windows_appshot_helper({"ASTRA_COMPUTER_HELPER_PATH": override})


def test_staged_session_failure_keeps_current_session_and_compressor(tmp_path):
    from agent.runtime.context import AgentContext
    from types import SimpleNamespace

    saved = AgentContext(system_prompt="other prompt")
    saved.set_session(str(tmp_path / "other.json"))
    ref = AppshotMediaStore(saved.session_path).put(PNG, 1, 1)
    saved.add_user([{"type": "appshot_image", "appshot_image": ref}])
    saved.save()
    (AppshotMediaStore(saved.session_path).path / (ref["media_id"] + ".png")).write_bytes(b"bad")
    active = AgentContext(system_prompt="current prompt")
    active.set_session(str(tmp_path / "active.json"))
    active.add_user("current history")
    resets = []
    active.compressor = SimpleNamespace(reset=lambda: resets.append(True))
    before = (active.session_path, active.system_prompt, list(active.messages))
    with pytest.raises(AppshotMediaError):
        active.stage_session(saved.session_path)
    assert (active.session_path, active.system_prompt, active.messages) == before
    assert resets == []
    candidate = active.stage_session(str(tmp_path / "new.json"))
    assert active.messages and candidate.messages == [] and resets == []
    active.reset()
    active.adopt_session(candidate)
    assert active.session_path == str(tmp_path / "new.json") and not active.messages
    assert resets == [True]
