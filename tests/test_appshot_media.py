import pytest
from test_appshot_backend import bundle as bundle, verifier

from agent.runtime.appshot_media import AppshotMediaError, AppshotMediaStore


def test_media_reload_tamper_and_rollback(bundle, tmp_path):
    with verifier(bundle) as v:
        decoded = v.decode()
    store = AppshotMediaStore(tmp_path / "session.json")
    ref = store.put(decoded.png_bytes, decoded.width, decoded.height)
    assert store.hydrate(ref)["image_url"]["url"] == decoded.image_data_url
    path = store.path / (ref["media_id"] + ".png")
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_bytes(b"bad")
    with pytest.raises(AppshotMediaError):
        store.hydrate(ref)
    store.rollback([ref])
    assert not path.exists()


def test_media_path_injection(tmp_path):
    with pytest.raises(AppshotMediaError):
        AppshotMediaStore(tmp_path / "session.json").hydrate(
            {"media_id": "../evil", "sha256": "0" * 64, "width": 1, "height": 1}
        )


def test_session_rename_delete_keeps_image(bundle, tmp_path, monkeypatch):
    from agent.cli import sessions
    from agent.runtime.context import AgentContext

    monkeypatch.setattr(sessions, "SESSION_DIR", tmp_path.resolve())
    with verifier(bundle) as v:
        decoded = v.decode()
    old = sessions.session_path("old")
    store = AppshotMediaStore(old)
    ref = store.put(decoded.png_bytes, 12, 9)
    context = AgentContext()
    context.set_session(str(old))
    context.add_user([{"type": "appshot_image", "appshot_image": ref}])
    context.save()
    sessions.rename_session("old", "new")
    new = sessions.session_path("new")
    restored = AgentContext()
    restored.set_session(str(new))
    assert restored.load()
    assert "base64" in str(restored.get_prompt())
    assert not store.path.exists()
    sessions.delete_session("new")
    assert not AppshotMediaStore(new).path.exists()


def test_media_hardlink_and_unowned_delete_fail(bundle, tmp_path):
    import os

    with verifier(bundle) as v:
        decoded = v.decode()
    store = AppshotMediaStore(tmp_path.resolve() / "s.json")
    ref = store.put(decoded.png_bytes, 12, 9)
    target = store.path / (ref["media_id"] + ".png")
    os.link(target, store.path / "unknown")
    with pytest.raises(AppshotMediaError):
        store.hydrate(ref)
    with pytest.raises(AppshotMediaError):
        store.delete()
    assert target.exists()


def test_publish_fsync_failure_removes_exact_new_media(bundle, tmp_path, monkeypatch):
    import os

    with verifier(bundle) as v:
        decoded = v.decode()
    store = AppshotMediaStore(tmp_path.resolve() / "s.json")
    real = os.fsync
    calls = []

    def fail_directory(fd):
        calls.append(fd)
        if len(calls) == 2:
            raise OSError("injected fsync failure")
        return real(fd)

    monkeypatch.setattr(os, "fsync", fail_directory)
    with pytest.raises(AppshotMediaError):
        store.put(decoded.png_bytes, 12, 9)
    assert not list(store.path.iterdir())


@pytest.mark.parametrize("swap", ["source", "destination"])
def test_session_rename_replacement_fails_closed(bundle, tmp_path, monkeypatch, swap):
    from pathlib import Path

    from agent.cli import sessions
    from agent.runtime.context import AgentContext

    monkeypatch.setattr(sessions, "SESSION_DIR", tmp_path.resolve())
    with verifier(bundle) as v:
        decoded = v.decode()
    old = sessions.session_path("old")
    new = sessions.session_path("new")
    store = AppshotMediaStore(old)
    destination = AppshotMediaStore(new)
    ref = store.put(decoded.png_bytes, decoded.width, decoded.height)
    context = AgentContext()
    context.set_session(str(old))
    context.add_user([{"type": "appshot_image", "appshot_image": ref}])
    context.save()
    history = old.with_suffix(".jsonl").read_bytes()
    original_identity = (store.path.stat().st_dev, store.path.stat().st_ino)
    displaced = tmp_path.resolve() / "displaced.appshot-media"
    real_rename = Path.rename

    def race(path, target):
        if path == store.path:
            if swap == "source":
                real_rename(path, displaced)
                path.mkdir(mode=0o700)
                (path / "unrelated").write_text("preserve me")
            result = real_rename(path, target)
            if swap == "destination":
                real_rename(target, displaced)
                target.mkdir(mode=0o700)
                (target / "unrelated").write_text("preserve me")
            return result
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", race)
    with pytest.raises(AppshotMediaError, match="session_media_authority"):
        sessions.rename_session("old", "new")
    assert old.with_suffix(".jsonl").read_bytes() == history
    assert not sessions.session_exists("new")
    assert (displaced.stat().st_dev, displaced.stat().st_ino) == original_identity
    assert (displaced / (ref["media_id"] + ".png")).read_bytes() == decoded.png_bytes
    assert (destination.path / "unrelated").read_text() == "preserve me"
