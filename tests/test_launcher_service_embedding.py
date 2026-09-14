"""Shared worker lifecycle using fake processes and real authenticated loopback."""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent.launcher import service_embedding as mod
from agent.launcher.common import LauncherError
from agent.runtime.context_index.embedding_worker import Worker

pytestmark = pytest.mark.skipif(os.name == "nt", reason="macOS MLX worker/private POSIX state")


@pytest.fixture
def embedding(tmp_path, monkeypatch):
    install = SimpleNamespace(root=tmp_path, python=tmp_path / ".venv/bin/python")
    directory = tmp_path / "private runtime 中文 with spaces"
    directory.mkdir(mode=0o700)
    adapter = mod.EmbeddingServices(install)
    workers = {42: [str(install.python), "-m", mod.MODULE, "--directory", str(directory)]}
    monkeypatch.setattr(mod, "_candidates", lambda: list(workers))
    monkeypatch.setattr(mod, "_argv", lambda pid: workers.get(pid, []))
    return adapter, directory, workers


def test_discovery_is_exact_and_never_reads_another_checkouts_worker(embedding):
    adapter, directory, workers = embedding
    workers[43] = ["/other/.venv/bin/python", "-m", mod.MODULE, "--directory", str(directory)]
    workers[44] = [str(adapter.install.python), "-m", "other", "--directory", str(directory)]
    entries = adapter.discover()
    assert len(entries) == 1 and entries[0]["pids"] == [42]
    assert entries[0]["directory"] == str(directory)


def test_native_arguments_preserve_spaces_and_empty_values():
    args = ["/Astra 中文 folder/.venv/bin/python", "-m", mod.MODULE, "--directory", "/state folder", ""]
    raw = len(args).to_bytes(4, "little") + b"/real/python\0\0\0" + b"\0".join(a.encode() for a in args) + b"\0ENV=ignored\0"
    assert mod._decode_argv(raw) == args


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS kernel argument inventory")
def test_native_argument_probe_reads_current_process_without_ps_splitting():
    args = mod._argv(os.getpid())
    assert args and "python" in args[0].lower()


def test_cooperative_stop_waits_for_idle_and_dispatches_once(embedding, monkeypatch):
    adapter, _, workers = embedding
    entry = adapter.discover()[0]
    monkeypatch.setattr(mod, "_state", lambda directory, pid: {"pid": pid})
    requests = []
    polls = []
    def request(state, *, stop=False):
        requests.append(stop)
        return {"busy": len(requests) < 2, "state": "stopping" if stop else "ready"}
    def sleep(_):
        polls.append(1)
        if len(polls) == 3:
            workers.clear()
    monkeypatch.setattr(mod, "_request", request)
    monkeypatch.setattr(mod.time, "sleep", sleep)
    adapter.stop(entry)
    assert requests == [False, False, True]


def test_busy_worker_is_not_interrupted(embedding, monkeypatch):
    adapter, _, _ = embedding
    entry = adapter.discover()[0]
    monkeypatch.setattr(mod, "_state", lambda *args: {})
    monkeypatch.setattr(mod, "TIMEOUT", 0)
    with pytest.raises(LauncherError, match="still busy or stopping"):
        adapter.stop(entry)


def test_resume_waits_for_new_control_endpoint_and_accepts_background_loading(embedding, monkeypatch):
    adapter, directory, workers = embedding
    entry = adapter.discover()[0]
    workers.clear()
    launches = []
    def popen(args, **kwargs):
        launches.append((args, kwargs))
        workers[99] = args
        return SimpleNamespace(pid=99, poll=lambda: None)
    monkeypatch.setattr(mod.subprocess, "Popen", popen)
    monkeypatch.setattr(mod, "_state", lambda directory, pid: {"pid": pid})
    monkeypatch.setattr(mod, "_request", lambda state: {"state": "loading"})
    adapter.start(entry)
    assert launches[0][0] == [str(adapter.install.python), "-m", mod.MODULE, "--directory", str(directory)]
    assert launches[0][1]["start_new_session"]


def test_private_worker_state_and_authenticated_real_control(embedding):
    _, directory, _ = embedding
    backend = SimpleNamespace()
    worker = Worker(backend)
    server = ThreadingHTTPServer(("127.0.0.1", 0), worker.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    from agent.runtime.context_index.embedding_runtime import identity
    state = {"protocol": 1, "identity": identity(), "pid": os.getpid(), "port": server.server_port, "token": worker.token}
    path = directory / "profile.json"
    path.write_text(json.dumps(state))
    path.chmod(0o600)
    try:
        assert mod._state(directory, os.getpid()) == state
        assert mod._request(state)["state"] == "loading"
        with pytest.raises(LauncherError, match="authenticated"):
            mod._request({**state, "token": "0" * 64}, stop=True)
        assert not worker.stop.is_set()
        worker.encoding.acquire()
        try:
            assert mod._request(state, stop=True)["state"] == "busy"
            assert not worker.stop.is_set()
        finally:
            worker.encoding.release()
        assert mod._request(state, stop=True)["state"] == "stopping"
        assert worker.stop.is_set()
        from agent.runtime.context_index.embedding_runtime import request
        encodes = []
        backend.encode = lambda texts: encodes.append(texts) or []
        worker.state = "ready"
        with pytest.raises(RuntimeError):
            request(state, "/encode", {"texts": ["late query"]})
        assert encodes == []
        path.chmod(0o644)
        assert mod._state(directory, os.getpid()) is None
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_recovery_cannot_redirect_worker_state_through_a_new_symlink(embedding):
    adapter, directory, _ = embedding
    entry = adapter.discover()[0]
    other = directory.with_name("moved")
    directory.rename(other)
    directory.symlink_to(other, target_is_directory=True)
    with pytest.raises(LauncherError, match="Invalid embedding"):
        adapter.start(entry)
