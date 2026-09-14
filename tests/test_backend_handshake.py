import asyncio

import pytest

from agent.cli import backend


def test_ipc_handshake_precedes_optional_initialization(monkeypatch):
    events = []
    monkeypatch.setattr(backend, "_write_event", events.append)

    def cold_initialization(*args):
        assert events == [{"type": "backend_hello", "protocol_version": 1}]
        raise RuntimeError("simulated initialization failure")

    monkeypatch.setattr(backend, "load_project_env", cold_initialization)
    with pytest.raises(RuntimeError, match="simulated initialization failure"):
        asyncio.run(backend.main())
