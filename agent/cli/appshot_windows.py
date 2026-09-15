"""Windows verified read + independent decoding. Production admission remains gated.

The native helper owns pinned handles and verifies actual ACL/file IDs. Python
independently proves its TUI pipe peer/ancestry and validates PNG pixels and UIA semantics;
neither the manifest nor TUI may supply authoritative process identity.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
import hashlib
import json
from pathlib import PureWindowsPath
import subprocess
import sys

from agent.cli.appshots import AppshotValidationError, DecodedAppshot, _pairs, _unicode, validate_png
from agent.cli.appshot_protocol_windows import parse_windows_appshot_manifest, parse_windows_appshot_message
from agent.runtime.appshot_windows_process import WindowsAppshotProcessIdentity, read_windows_recipient_identity


def _json(raw: bytes, maximum: int):
    try:
        if not raw or len(raw) > maximum:
            raise ValueError()
        value = json.loads(raw.decode("utf8"), object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        _unicode(value)
        return value
    except (ValueError, TypeError, RecursionError) as exc:
        raise AppshotValidationError("artifact_unsafe") from exc


def validate_uia(raw: bytes, descriptor: dict) -> dict:
    """Bounded preorder tree, root depth zero, password nodes contain no text."""
    value = _json(raw, 262144)
    metadata = {key: descriptor[key] for key in ("coverage", "node_count", "depth", "truncated", "truncation_reasons")}
    metadata["truncation_reasons"] = list(metadata["truncation_reasons"])
    if (type(value) is not dict or set(value) != {"schema_version", "platform", "nodes", *metadata}
        or type(value["schema_version"]) is not int or value["schema_version"] != 2 or value["platform"] != "windows"
        or any(value[key] != item or type(value[key]) is not type(item) for key, item in metadata.items())
        or type(value["nodes"]) is not list or len(value["nodes"]) != metadata["node_count"]
        or len(value["nodes"]) > 2000):
        raise AppshotValidationError("invalid_uia")
    stack: list[int] = []
    deepest = 0
    for index, node in enumerate(value["nodes"]):
        if (type(node) is not dict or type(node.get("password")) is not bool
            or type(node.get("parent")) is not int or type(node.get("depth")) is not int):
            raise AppshotValidationError("invalid_uia")
        depth, parent = node["depth"], node["parent"]
        if not 0 <= depth <= min(64, len(stack)):
            raise AppshotValidationError("invalid_uia")
        if index == 0:
            if depth != 0 or parent != -1:
                raise AppshotValidationError("invalid_uia")
        elif depth == 0 or parent != stack[depth - 1] or value["nodes"][parent]["password"]:
            raise AppshotValidationError("invalid_uia")
        if node["password"]:
            if set(node) != {"password", "parent", "depth"}:
                raise AppshotValidationError("invalid_uia")
        else:
            required = {"password", "parent", "depth", "control_type", "name", "text_truncated"}
            if (set(node) not in (required, required | {"text"})
                or type(node["control_type"]) is not int or not 0 <= node["control_type"] < 2**31
                or type(node["text_truncated"]) is not bool):
                raise AppshotValidationError("invalid_uia")
            for key in ("name", "text"):
                if key in node and (type(node[key]) is not str or len(node[key].encode("utf8")) > 8192 or "\0" in node[key]):
                    raise AppshotValidationError("invalid_uia")
        stack[depth:] = [index]
        deepest = max(deepest, depth)
    if deepest != metadata["depth"] or metadata["coverage"] == "unavailable" and value["nodes"]:
        raise AppshotValidationError("invalid_uia")
    return value


def decode_windows_artifact(value: dict, *, manifest_path: str, broker_id: str, session_id: str,
    recipient: WindowsAppshotProcessIdentity) -> DecodedAppshot:
    try:
        if (type(value) is not dict or set(value) != {"version", "platform", "manifest", "png_base64", "uia_json"}
            or type(value["version"]) is not int or value["version"] != 2 or value["platform"] != "windows"
            or type(value["png_base64"]) is not str or len(value["png_base64"]) > 13981016
            or type(value["uia_json"]) is not str):
            raise ValueError()
        manifest = parse_windows_appshot_manifest(json.dumps(value["manifest"], ensure_ascii=False))
        if (manifest["broker"] != {"instance_id": broker_id, "session_id": session_id, "recipient": recipient.wire()}
            or PureWindowsPath(manifest_path).name != f"appshot-{manifest['token']}.manifest.json"):
            raise ValueError()
        png = base64.b64decode(value["png_base64"], validate=True)
        uia = value["uia_json"].encode("utf8")
        if base64.b64encode(png).decode("ascii") != value["png_base64"]:
            raise ValueError()
        for content, descriptor in ((png, manifest["png"]), (uia, manifest["uia"])):
            if len(content) != descriptor["size"] or hashlib.sha256(content).hexdigest() != descriptor["sha256"]:
                raise ValueError()
        width, height = manifest["png"]["width"], manifest["png"]["height"]
        validate_png(png, width, height)
        projection = validate_uia(uia, manifest["uia"])
        source = {key: manifest["source"][key] for key in ("app_id", "app_label", "window_title")}
        return DecodedAppshot(png, width, height, source, projection)
    except (ValueError, KeyError, TypeError, RecursionError) as exc:
        raise AppshotValidationError("artifact_unsafe") from exc


async def read_windows_appshot(executable: str, manifest_path: str, *, broker_id: str, session_id: str,
    runtime_root: str) -> DecodedAppshot:
    if sys.platform != "win32":
        raise AppshotValidationError("unsupported_platform")
    # Strict drive path, direct child of the independently configured runtime.
    parse_windows_appshot_message(json.dumps({"type": "attach_offer", "version": 2, "platform": "windows",
        "request_id": "backend-read", "broker_id": broker_id, "session_id": session_id, "manifest_path": manifest_path}))
    root = PureWindowsPath(runtime_root)
    if not root.is_absolute() or PureWindowsPath(manifest_path).parent != root:
        raise AppshotValidationError("artifact_unsafe")
    recipient = read_windows_recipient_identity()
    try:
        # Inherit input for the kernel pipe-owner proof only; helper never reads
        # commands from it. Output is a separate, bounded native-read channel.
        process = await asyncio.create_subprocess_exec(executable, "--appshot-read-backend-artifact", manifest_path, broker_id, session_id,
            stdin=None, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as exc:
        raise AppshotValidationError("artifact_unsafe") from exc
    try:
        output = process.stdout
        if output is None:
            raise AppshotValidationError("artifact_unsafe")
        async with asyncio.timeout(2):
            raw = bytearray()
            while True:
                chunk = await output.read(min(65536, 16 * 1024 * 1024 + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > 16 * 1024 * 1024:
                    raise AppshotValidationError("artifact_unsafe")
            if await process.wait() != 0:
                raise AppshotValidationError("artifact_unsafe")
        if read_windows_recipient_identity() != recipient:
            raise AppshotValidationError("recipient_identity_unavailable")
        decoded = decode_windows_artifact(_json(bytes(raw), 16 * 1024 * 1024), manifest_path=manifest_path,
            broker_id=broker_id, session_id=session_id, recipient=recipient)
        if read_windows_recipient_identity() != recipient:
            raise AppshotValidationError("recipient_identity_unavailable")
        return decoded
    except (TimeoutError, OSError) as exc:
        raise AppshotValidationError("artifact_unsafe") from exc
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.wait()
