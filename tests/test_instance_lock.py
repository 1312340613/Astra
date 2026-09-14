import asyncio
import os
from pathlib import Path

import pytest

from agent.cli import backend, sessions
from agent.channels.manager import is_address_in_use
from agent.runtime.instance_lock import InstanceAlreadyRunning, InstanceLock


def test_instance_lock_is_exclusive_and_reusable(tmp_path: Path):
    path = tmp_path / ".astra" / "backend.lock"
    first = InstanceLock(path)
    second = InstanceLock(path)

    first.acquire()
    try:
        with pytest.raises(InstanceAlreadyRunning):
            second.acquire()
    finally:
        first.release()

    assert path.read_text(encoding="ascii").strip() == str(os.getpid())
    second.acquire()
    second.release()


def test_backend_run_allows_multiple_cli_instances(monkeypatch):
    calls = []

    async def fake_main():
        calls.append(True)

    monkeypatch.setattr(backend, "main", fake_main)
    assert backend.run() == 0
    assert backend.run() == 0
    assert calls == [True, True]


def test_startup_session_path_is_process_scoped(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sessions, "SESSION_DIR", tmp_path)
    monkeypatch.delenv("AGENT_SESSION", raising=False)
    monkeypatch.setattr(sessions.os, "getpid", lambda: 101)
    first = sessions.startup_session_path()
    monkeypatch.setattr(sessions.os, "getpid", lambda: 202)
    second = sessions.startup_session_path()

    assert first != second
    assert first.stem.endswith("_101")
    assert second.stem.endswith("_202")


def test_channel_port_conflict_only_disables_optional_channel(capsys):
    class ConflictingChannelManager:
        async def start(self):
            raise OSError(10048, "address already in use")

    conflict = asyncio.run(backend._start_channel_manager(ConflictingChannelManager()))

    assert isinstance(conflict, OSError)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


@pytest.mark.parametrize("errno", [48, 98, 10048])
def test_backend_recognizes_address_in_use(errno):
    assert is_address_in_use(OSError(errno, "address in use"))
