import base64
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from agent.cli.appshot_windows import decode_windows_artifact, validate_uia
from agent.cli.appshots import AppshotValidationError
from agent.runtime.appshot_process import AppshotProcessIdentityError
from agent.runtime.appshot_windows_process import WindowsAppshotProcessIdentity, read_windows_parent_identity, read_windows_process_identity
from agent.runtime import appshot_windows_process as process_module

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg==")


def node(parent=-1, depth=0, **kw):
    return dict(password=False, parent=parent, depth=depth, control_type=50033, name="Fixture", text_truncated=False, **kw)


def envelope(nodes=None):
    nodes = nodes if nodes is not None else [node()]
    return dict(schema_version=2, platform="windows", nodes=nodes, coverage="reported_uia_subtree",
        node_count=len(nodes), depth=max((n["depth"] for n in nodes), default=0), truncated=False, truncation_reasons=[])


def encode(value):
    return json.dumps(value, ensure_ascii=False).encode("utf8")


def descriptor(value):
    return {k: value[k] for k in ("coverage", "node_count", "depth", "truncated", "truncation_reasons")}


def artifact():
    manifest = json.loads((Path(__file__).parents[1] / "native/appshot-core/Tests/Fixtures/appshot_manifest_v2.json").read_text(encoding="utf8"))
    uia = envelope([node(), dict(password=True, parent=0, depth=1), node(parent=0, depth=1, text="Hello")])
    data = encode(uia)
    manifest["png"].update(size=len(PNG), width=1, height=1, sha256=hashlib.sha256(PNG).hexdigest())
    manifest["uia"].update(size=len(data), sha256=hashlib.sha256(data).hexdigest(), **descriptor(uia))
    return {"version": 2, "platform": "windows", "manifest": manifest, "png_base64": base64.b64encode(PNG).decode(), "uia_json": data.decode()}


def decode(value, recipient=None):
    m = value["manifest"]
    return decode_windows_artifact(value, manifest_path=f"C:/Fixture/appshot-{m['token']}.manifest.json",
        broker_id=m["broker"]["instance_id"], session_id=m["broker"]["session_id"],
        recipient=recipient or WindowsAppshotProcessIdentity(**m["broker"]["recipient"]))


def test_windows_decode_validates_pixels_and_password_tree():
    result = decode(artifact())
    assert result.png_bytes == PNG and result.width == result.height == 1
    assert result.projection["nodes"][1] == {"password": True, "parent": 0, "depth": 1}
    assert "file_id" not in result.source


@pytest.mark.parametrize("mutation", [
    lambda v: v["manifest"]["png"].update(sha256="0" * 64),
    lambda v: v.update(png_base64=v["png_base64"] + "\n"),
    lambda v: v["manifest"]["png"].update(width=2),
    lambda v: v.update(uia_json=v["uia_json"] + " "),
    lambda v: v.update(version=True),
    lambda v: v.update(authoritative_pid=123),
])
def test_windows_decode_rejects_invalid_or_tampered_content(mutation):
    value = artifact()
    mutation(value)
    with pytest.raises(AppshotValidationError):
        decode(value)


def test_windows_decode_independently_rejects_crc_failure_even_with_matching_hash():
    value = artifact()
    png = bytearray(PNG)
    png[-1] ^= 1
    value["png_base64"] = base64.b64encode(png).decode()
    value["manifest"]["png"]["sha256"] = hashlib.sha256(png).hexdigest()
    with pytest.raises(AppshotValidationError):
        decode(value)


@pytest.mark.parametrize("change", [dict(pid=123456789), dict(process_start="987"), dict(user_sid="S-1-5-21-9")])
def test_windows_decode_rejects_parent_identity_mismatch(change):
    value = artifact()
    identity = {**value["manifest"]["broker"]["recipient"], **change}
    with pytest.raises(AppshotValidationError):
        decode(value, WindowsAppshotProcessIdentity(**identity))


@pytest.mark.parametrize("nodes", [
    [node(parent=0)], [node(depth=1)], [node(), node(parent=1, depth=1)], [node(), node(parent=0, depth=2)],
    [node(), node()], [dict(password=True, parent=-1, depth=0, name="secret")],
    [dict(password=True, parent=-1, depth=0), node(parent=0, depth=1)],
    [node(), node(parent=0, depth=1), node(parent=0, depth=1), node(parent=1, depth=2)],
    [node(text="bad\0value")], [node(text="x" * 8193)], [node(action="invoke")],
])
def test_uia_rejects_malformed_tree_and_password_payload(nodes):
    value = envelope(nodes)
    with pytest.raises(AppshotValidationError):
        validate_uia(encode(value), descriptor(value))


def test_uia_rejects_metadata_mismatch_duplicate_keys_and_wrong_platform():
    value = envelope()
    for raw in [encode({**value, "depth": 4}), encode({**value, "platform": "darwin"}),
        encode(value).replace(b'"schema_version": 2', b'"schema_version": 2, "schema_version": 2'), b" " * 262145]:
        with pytest.raises(AppshotValidationError):
            validate_uia(raw, descriptor(value))


def test_uia_screenshot_only_envelope_is_explicit_and_empty():
    value = dict(schema_version=2, platform="windows", coverage="unavailable", node_count=0, depth=0,
        truncated=True, truncation_reasons=["uia_timeout"], nodes=[])
    assert validate_uia(encode(value), descriptor(value)) == value


@pytest.mark.skipif(sys.platform != "win32", reason="Windows kernel identity")
def test_windows_kernel_identity_parent_and_invalid_pid():
    own = read_windows_process_identity(os.getpid())
    parent = read_windows_parent_identity()
    assert own.pid == os.getpid() and parent.pid == os.getppid()
    assert own.user_sid == parent.user_sid and own.user_sid.startswith("S-1-")
    assert int(parent.process_start) < int(own.process_start)
    assert read_windows_process_identity(os.getpid()) == own
    for pid in [True, 0, -1, 2**31, "1"]:
        with pytest.raises(AppshotProcessIdentityError):
            read_windows_process_identity(pid)


@pytest.mark.parametrize("wrapper", [False, True])
def test_recipient_proof_uses_stdio_and_bounded_ancestry(monkeypatch, wrapper):
    identities = {pid: WindowsAppshotProcessIdentity(pid, str(pid), "S-1-5-21-1") for pid in (1, 2, 3)}
    monkeypatch.setattr(process_module.os, "getpid", lambda: 3)
    monkeypatch.setattr(process_module, "_stdio_owner", lambda: identities[1])
    monkeypatch.setattr(process_module, "read_windows_process_identity", lambda pid: identities[pid])
    monkeypatch.setattr(process_module, "_parent_pid", lambda pid: {3: 2 if wrapper else 1, 2: 1}[pid])
    assert process_module.read_windows_recipient_identity() == identities[1]


@pytest.mark.parametrize("failure", ["not_ancestor", "wrong_sid", "future_parent", "reused_parent", "pipe_replaced"])
def test_recipient_proof_rejects_ambiguous_or_changed_authority(monkeypatch, failure):
    identities = {pid: WindowsAppshotProcessIdentity(pid, str(pid), "S-1-5-21-1") for pid in (1, 2, 3, 4)}
    if failure == "wrong_sid":
        identities[2] = WindowsAppshotProcessIdentity(2, "2", "S-1-5-21-9")
    if failure == "future_parent":
        identities[2] = WindowsAppshotProcessIdentity(2, "999", "S-1-5-21-1")
    monkeypatch.setattr(process_module.os, "getpid", lambda: 3)
    owners = iter([identities[4] if failure == "not_ancestor" else identities[1], identities[4] if failure == "pipe_replaced" else identities[1]])
    monkeypatch.setattr(process_module, "_stdio_owner", lambda: next(owners))
    reads = {}

    def read(pid):
        reads[pid] = reads.get(pid, 0) + 1
        if failure == "reused_parent" and pid == 2 and reads[pid] > 1:
            return WindowsAppshotProcessIdentity(2, "987", identities[2].user_sid)
        return identities[pid]

    monkeypatch.setattr(process_module, "read_windows_process_identity", read)
    monkeypatch.setattr(process_module, "_parent_pid", lambda pid: {3: 2, 2: 1}[pid])
    with pytest.raises(AppshotProcessIdentityError):
        process_module.read_windows_recipient_identity()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reader subprocess flags")
def test_reader_drains_short_reads_before_waiting_for_exit(monkeypatch):
    from agent.cli import appshot_windows as reader
    value = artifact()
    raw = encode(value)
    chunks = [raw[i:i + 7] for i in range(0, len(raw), 7)] + [b""]
    class Output:
        async def read(self, maximum):
            return chunks.pop(0)
    class Child:
        stdout = Output()
        returncode = None
        async def wait(self):
            assert not chunks, "must read through EOF before waiting"
            self.returncode = 0
            return 0
        def kill(self):
            raise AssertionError("valid short reads must not kill the helper")
    async def create(*args, **kwargs):
        return Child()
    binding = value["manifest"]["broker"]
    monkeypatch.setattr(reader.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(reader, "read_windows_recipient_identity", lambda: WindowsAppshotProcessIdentity(**binding["recipient"]))
    result = asyncio.run(reader.read_windows_appshot("fixture.exe",
        f"C:/Fixture/appshot-{value['manifest']['token']}.manifest.json",
        broker_id=binding["instance_id"], session_id=binding["session_id"], runtime_root="C:/Fixture"))
    assert result.png_bytes == PNG


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reader subprocess flags")
def test_reader_rejects_missing_output_pipe_and_reaps_child(monkeypatch):
    from agent.cli import appshot_windows as reader
    value = artifact()
    class Child:
        stdout = None
        returncode = None
        killed = False
        waited = False
        def kill(self):
            self.killed = True
        async def wait(self):
            assert self.killed
            self.waited = True
            self.returncode = 1
            return 1
    child = Child()
    async def create(*args, **kwargs):
        return child
    binding = value["manifest"]["broker"]
    monkeypatch.setattr(reader.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(reader, "read_windows_recipient_identity", lambda: WindowsAppshotProcessIdentity(**binding["recipient"]))
    with pytest.raises(AppshotValidationError, match="artifact_unsafe"):
        asyncio.run(reader.read_windows_appshot("fixture.exe",
            f"C:/Fixture/appshot-{value['manifest']['token']}.manifest.json",
            broker_id=binding["instance_id"], session_id=binding["session_id"], runtime_root="C:/Fixture"))
    assert child.killed and child.waited
