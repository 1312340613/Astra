import asyncio
import base64
import json
import os

import pytest

from agent.runtime.browser_upload import CHUNK_BYTES, MAX_FILE_BYTES, inspect_files, read_files
from agent.runtime.extension_browser_backend import ExtensionBrowserBackend
from agent.runtime.browser_control_transport import BrowserControlTransport, MAX_FRAME


def test_local_file_identity_and_empty_list(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"file contents")
    files = inspect_files(["file.txt"], tmp_path)
    assert files[0].metadata() == {"name": "file.txt", "size": 13, "type": "text/plain"}
    assert read_files(files) == [b"file contents"]
    assert inspect_files([], tmp_path) == []
    path.write_bytes(b"modified file")
    with pytest.raises(ValueError, match="changed"):
        read_files(files)


def test_replacement_and_symlink_retarget_rejected(tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"old")
    link = tmp_path / "link"
    link.symlink_to(path)
    files = inspect_files([str(link)], tmp_path)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"new")
    replacement.replace(path)
    with pytest.raises(ValueError, match="changed"):
        read_files(files)
    files = inspect_files([str(link)], tmp_path)
    replacement.write_bytes(b"new")
    link.unlink()
    link.symlink_to(replacement)
    with pytest.raises(ValueError, match="changed"):
        read_files(files)


def test_nonfiles_and_limits_rejected_without_reading(tmp_path):
    for paths in ([str(tmp_path)], ["missing"], [""]):
        with pytest.raises((OSError, ValueError)):
            inspect_files(paths, tmp_path)
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="ordinary"):
        inspect_files([str(fifo)], tmp_path)
    big = tmp_path / "big"
    with big.open("wb") as f:
        f.truncate(MAX_FILE_BYTES + 1)
    with pytest.raises(ValueError, match="limit"):
        inspect_files([str(big)], tmp_path)
    with big.open("wb") as f:
        f.truncate(MAX_FILE_BYTES)
    with pytest.raises(ValueError, match="limit"):
        inspect_files([str(big)] * 3, tmp_path)
    with pytest.raises(ValueError, match="at most 10"):
        inspect_files([str(big)] * 11, tmp_path)


class UploadTransport:
    connected = True
    generation = 1

    def __init__(self, fail="", support=True):
        self.calls = []
        self.fail = fail
        self.support = support
        self.buffers = []

    def operation_support(self, _op):
        return self.support

    async def request(self, operation, *, tab_id=None, args=None):
        assert tab_id == 7
        self.calls.append((operation, args))
        assert len(json.dumps({"operation": operation, "args": args}).encode()) < MAX_FRAME
        if operation == self.fail:
            raise ConnectionError("transport error with PRIVATE-BYTES")
        if operation == "snapshot":
            return {"url": "https://example.com/form"}
        if operation == "upload_prepare":
            self.buffers = [bytearray() for f in args["files"]]
            return {"status": "prepared", "transferId": "transfer-1"}
        if operation == "upload_chunk":
            assert args["offset"] == len(self.buffers[args["index"]])
            data = base64.b64decode(args["data"], validate=True)
            assert len(data) <= CHUNK_BYTES
            self.buffers[args["index"]].extend(data)
            return {"status": "buffered"}
        if operation == "upload_commit":
            return {"status": "verified", "verified": True, "dispatch_state": "dispatched", "target": {}}
        return {"status": "aborted"}


@pytest.mark.parametrize("fail", ["", "upload_prepare", "upload_chunk", "upload_commit"])
def test_chunked_upload_and_uncertain_commit_never_replayed(tmp_path, fail):
    data = bytes(range(256)) * 5001
    path = tmp_path / "large.bin"
    path.write_bytes(data)
    files = inspect_files([str(path)], tmp_path)
    transport = UploadTransport(fail)
    backend = ExtensionBrowserBackend(transport=transport)
    backend._tabs["tab"] = (7, 1, "https://example.com")
    result = json.loads(asyncio.run(backend.interactive_upload("ref:file", files, tab_id="tab", url="https://example.com/form")))
    assert "PRIVATE-BYTES" not in json.dumps(result)
    operations = [op for op, _ in transport.calls]
    assert operations.count("upload_commit") <= 1
    if not fail:
        assert result["verified"]
        assert bytes(transport.buffers[0]) == data
    else:
        assert result["dispatch_state"] == ("unknown" if fail == "upload_commit" else "not_dispatched")
        if fail != "upload_commit":
            assert "upload_commit" not in operations


@pytest.mark.parametrize("support", [False, None])
def test_old_controller_refused_before_reading_file(tmp_path, monkeypatch, support):
    from agent.runtime import browser_upload
    path = tmp_path / "data"
    path.write_bytes(b"secret")
    files = inspect_files([str(path)], tmp_path)
    monkeypatch.setattr(browser_upload, "read_files", lambda _: pytest.fail("must not read bytes"))
    transport = UploadTransport(support=support)
    backend = ExtensionBrowserBackend(transport=transport)
    backend._tabs["tab"] = (7, 1, "https://example.com")
    result = json.loads(asyncio.run(backend.interactive_upload("ref:f", files, tab_id="tab", url="https://example.com")))
    assert result["status"] == "unsupported_operation"
    assert not transport.calls


def test_legacy_capability_negotiation_does_not_assume_upload(tmp_path):
    transport = BrowserControlTransport(tmp_path)
    assert transport.operation_support("upload_prepare") is None


@pytest.mark.parametrize("controller,page,expected", [(None, True, False), (False, True, False), (True, False, False), (True, True, True)])
def test_snapshot_upload_capability_requires_both_layers(controller, page, expected):
    backend = ExtensionBrowserBackend(transport=UploadTransport(support=controller))
    result = backend._observe_result("tab", {"capabilities": {"upload": page}})
    assert result["capabilities"]["upload"] is expected


@pytest.mark.parametrize("change", ["connection", "binding", "file"])
def test_transfer_change_prevents_commit(tmp_path, change):
    path = tmp_path / "data.bin"
    path.write_bytes(b"a" * (CHUNK_BYTES + 1))
    files = inspect_files([str(path)], tmp_path)
    transport = UploadTransport()
    backend = ExtensionBrowserBackend(transport=transport)
    backend._tabs["tab"] = (7, 1, "https://example.com")
    request = transport.request

    async def mutate(operation, **kwargs):
        result = await request(operation, **kwargs)
        if operation == "upload_chunk":
            if change == "connection":
                transport.generation += 1
            elif change == "binding":
                backend._tabs.pop("tab", None)
            else:
                path.write_bytes(b"changed")
        return result

    transport.request = mutate
    result = json.loads(asyncio.run(backend.interactive_upload("ref:file", files, tab_id="tab", url="https://example.com")))
    assert result["dispatch_state"] == "not_dispatched"
    assert "upload_commit" not in [op for op, _ in transport.calls]


def test_public_tool_exact_file_approval_and_yolo_cleanup(tmp_path):
    from agent.runtime.browser_session import BackendCapabilities, BrowserSessionManager
    from agent.runtime.tools.browser import register_browser_tools
    from agent.runtime.tools.registry import ToolRegistry
    from agent.runtime.tools.files import FilesystemPolicy

    class Backend:
        capabilities = BackendCapabilities(read=True, interactive=True, takeover=True)
        calls = []

        async def interactive_upload(self, selector, files, **kwargs):
            self.calls.append((selector, files, kwargs))
            return json.dumps({"status": "verified", "verified": True, "target": {},
                               "files": [f.metadata() for f in files]})

    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("only this file is allowed")
    manager = BrowserSessionManager(path=work / "browser.db")
    reg, backend = ToolRegistry(), Backend()
    access = FilesystemPolicy.load(str(work))
    before_roots = list(access.roots)
    register_browser_tools(reg, manager=manager, backend=backend, filesystem_policy=access)

    async def run():
        await reg.execute("browser_open", {"url": "https://example.test/form", "extract": False})
        tab_id = manager.list_sessions()[0].current_tab_id
        args = {"tab_id": tab_id, "selector": "ref:file", "paths": [str(outside)]}
        reg.approved_permission_scopes.add("browser-write:https://example.test")
        requests = []

        async def deny(request):
            requests.append(request)
            return "deny"

        reg.set_approval_handler(deny)
        denied = await reg.execute("browser_upload", args)
        assert denied["error"] and not backend.calls
        assert requests[-1]["kind"] == "browser_upload"
        assert requests[-1]["files"][0]["path"] == str(outside)
        assert requests[-1]["filesystem_requests"][0]["access"] == "read"
        # Eligible YOLO approval is still limited to the concrete files and target.
        reg.yolo = True
        result = await reg.execute("browser_upload", args)
        assert not result.get("error"), result
        assert len(backend.calls) == 1
        assert access.roots == before_roots
        assert not any(s.startswith("browser-upload:") for s in reg.approved_permission_scopes)

    asyncio.run(run())


def test_relative_upload_resolves_against_configured_workspace(tmp_path, monkeypatch):
    from agent.runtime.browser_session import BackendCapabilities, BrowserSessionManager
    from agent.runtime.tools.browser import register_browser_tools
    from agent.runtime.tools.registry import ToolRegistry

    work = tmp_path / "workspace"
    work.mkdir()
    (work / "file.txt").write_text("workspace file")
    monkeypatch.chdir(tmp_path)

    class Backend:
        capabilities = BackendCapabilities(read=True, interactive=True, takeover=True)

        async def interactive_upload(self, selector, files, **kwargs):
            assert files[0].path == work / "file.txt"
            return json.dumps({"status": "verified", "verified": True, "target": {}})

    async def run():
        registry = ToolRegistry()
        registry.yolo = True
        manager = BrowserSessionManager(path=tmp_path / "browser.db")
        register_browser_tools(registry, manager=manager, backend=Backend(), workdir=str(work))
        await registry.execute("browser_open", {"url": "https://example.test/form", "extract": False})
        tab_id = manager.list_sessions()[0].current_tab_id
        result = await registry.execute("browser_upload", {"tab_id": tab_id, "selector": "ref:f", "paths": ["file.txt"]})
        assert not result.get("error"), result

    asyncio.run(run())
