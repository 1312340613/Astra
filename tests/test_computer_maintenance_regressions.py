import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.runtime.computer_backend import ComputerSessionError
from agent.runtime import computer_backend
from agent.runtime.tools.computer import LocalComputerRuntime


def test_local_runtime_forwards_user_activity_pause_to_current_session(tmp_path):
    runtime = LocalComputerRuntime(helper_path=None, cache_root=tmp_path)
    pause = AsyncMock()
    runtime._manager = SimpleNamespace(pause_for_user_activity=pause)

    asyncio.run(runtime.pause_for_user_activity())

    pause.assert_awaited_once_with()


def test_user_activity_pause_does_not_activate_a_new_helper(tmp_path):
    runtime = LocalComputerRuntime(helper_path=None, cache_root=tmp_path)
    with pytest.raises(ComputerSessionError, match="session_closed"):
        asyncio.run(runtime.pause_for_user_activity())
    assert not runtime.manager_started
    assert not runtime.helper_started


def test_unsupported_cache_platform_fails_before_creating_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(computer_backend, "os", SimpleNamespace(name="nt"))
    cache = tmp_path / "must-not-exist"
    with pytest.raises(ComputerSessionError, match="POSIX"):
        computer_backend.ComputerSessionManager(object(), cache_root=cache)
    assert not cache.exists()
