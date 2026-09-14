"""Strict Appshot v1 wire values. Parsing does not confer artifact authority."""

from __future__ import annotations
import json, math, re
from datetime import datetime
from dataclasses import dataclass
import sys
from os import stat_result
from pathlib import Path

MAX_MANIFEST_BYTES = MAX_FRAME_BYTES = 64 * 1024


class AppshotValidationError(ValueError):
    pass


@dataclass(frozen=True)
class AppshotBounds:
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class AppshotSource:
    pid: int
    process_start: str
    bundle_id: str
    app_label: str
    window_title: str
    window_id: int
    bounds: AppshotBounds


@dataclass(frozen=True)
class AppshotPNG:
    name: str
    size: int
    width: int
    height: int
    sha256: str
    device: str
    inode: str
    owner: int
    mode: int
    link_count: int


@dataclass(frozen=True)
class AppshotAX:
    name: str
    size: int
    sha256: str
    device: str
    inode: str
    owner: int
    mode: int
    link_count: int
    coverage: str
    node_count: int
    depth: int
    truncated: bool
    truncation_reasons: tuple[str, ...]


@dataclass(frozen=True)
class AppshotBrokerBinding:
    instance_id: str
    session_id: str
    process_start: str


@dataclass(frozen=True)
class AppshotManifest:
    schema_version: int
    token: str
    captured_at: str
    source: AppshotSource
    png: AppshotPNG
    ax: AppshotAX
    broker: AppshotBrokerBinding


@dataclass(frozen=True)
class AppshotHello:
    type: str
    version: int
    session_id: str
    pid: int
    process_start: str
    client_nonce: str


@dataclass(frozen=True)
class AppshotHelloAck:
    type: str
    version: int
    instance_id: str
    broker_nonce: str
    session_id: str


@dataclass(frozen=True)
class AppshotClientState:
    type: str
    version: int
    request_id: str
    broker_id: str
    session_id: str
    activity_ns: str
    appshot_count: int
    can_accept: bool


@dataclass(frozen=True)
class AppshotAttachOffer:
    type: str
    version: int
    request_id: str
    broker_id: str
    session_id: str
    manifest_path: str


@dataclass(frozen=True)
class AppshotAttachAck:
    type: str
    version: int
    request_id: str
    broker_id: str
    session_id: str
    accepted: bool
    reason: str


@dataclass(frozen=True)
class AppshotAttachCommit:
    type: str
    version: int
    request_id: str
    broker_id: str
    session_id: str
    manifest_path: str


@dataclass(frozen=True)
class AppshotAttachRevoke:
    type: str
    version: int
    request_id: str
    broker_id: str
    session_id: str
    reason: str


@dataclass(frozen=True)
class AppshotRelease:
    type: str
    version: int
    request_id: str
    broker_id: str
    session_id: str


@dataclass(frozen=True)
class AppshotReleaseAck:
    type: str
    version: int
    request_id: str
    broker_id: str
    session_id: str
    released: bool


@dataclass(frozen=True)
class AppshotCommand:
    type: str
    version: int
    request_id: str
    broker_id: str
    session_id: str
    name: str
    argument: str


@dataclass(frozen=True)
class AppshotCommandResult:
    type: str
    version: int
    request_id: str
    broker_id: str
    session_id: str
    ok: bool
    code: str
    message: str


@dataclass(frozen=True)
class AppshotClientStateAck:
    type: str
    version: int
    request_id: str
    broker_id: str
    session_id: str
    appshot_count: int
    can_accept: bool


@dataclass(frozen=True)
class AppshotStatus:
    type: str
    version: int
    request_id: str
    broker_id: str
    session_id: str
    enabled: bool
    chord: str
    registration: str
    connected_tuis: int
    permission: str
    appshot_count: int
    can_accept: bool


_SCHEMAS = {
    "AppshotBounds": {"x": ("coord",), "y": ("coord",), "width": ("extent",), "height": ("extent",)},
    "AppshotSource": {
        "pid": ("int", 1, 2147483647),
        "process_start": ("uint",),
        "bundle_id": ("str", 256),
        "app_label": ("str", 256),
        "window_title": ("str", 1024),
        "window_id": ("int", 0, 4294967295),
        "bounds": "AppshotBounds",
    },
    "AppshotPNG": {
        "name": ("str", 128),
        "size": ("int", 1, 10485760),
        "width": ("int", 1, 16384),
        "height": ("int", 1, 16384),
        "sha256": ("hash",),
        "device": ("uint",),
        "inode": ("uint",),
        "owner": ("int", 0, 4294967295),
        "mode": ("int", 384, 384),
        "link_count": ("int", 1, 1),
    },
    "AppshotAX": {
        "name": ("str", 128),
        "size": ("int", 1, 262144),
        "sha256": ("hash",),
        "device": ("uint",),
        "inode": ("uint",),
        "owner": ("int", 0, 4294967295),
        "mode": ("int", 384, 384),
        "link_count": ("int", 1, 1),
        "coverage": ("enum", "reported_ax_subtree", "unavailable"),
        "node_count": ("int", 0, 2000),
        "depth": ("int", 0, 64),
        "truncated": ("bool",),
        "truncation_reasons": ("reasons",),
    },
    "AppshotBrokerBinding": {"instance_id": ("id",), "session_id": ("id",), "process_start": ("uint",)},
    "AppshotManifest": {
        "schema_version": ("int", 1, 1),
        "token": ("token",),
        "captured_at": ("date",),
        "source": "AppshotSource",
        "png": "AppshotPNG",
        "ax": "AppshotAX",
        "broker": "AppshotBrokerBinding",
    },
    "AppshotHello": {
        "type": ("enum", "hello"),
        "version": ("int", 1, 1),
        "session_id": ("id",),
        "pid": ("int", 1, 2147483647),
        "process_start": ("uint",),
        "client_nonce": ("id",),
    },
    "AppshotHelloAck": {
        "type": ("enum", "hello_ack"),
        "version": ("int", 1, 1),
        "instance_id": ("id",),
        "broker_nonce": ("id",),
        "session_id": ("id",),
    },
    "AppshotClientState": {
        "type": ("enum", "client_state"),
        "version": ("int", 1, 1),
        "request_id": ("id",),
        "broker_id": ("id",),
        "session_id": ("id",),
        "activity_ns": ("uint",),
        "appshot_count": ("int", 0, 4),
        "can_accept": ("bool",),
    },
    "AppshotAttachOffer": {
        "type": ("enum", "attach_offer"),
        "version": ("int", 1, 1),
        "request_id": ("id",),
        "broker_id": ("id",),
        "session_id": ("id",),
        "manifest_path": ("path",),
    },
    "AppshotAttachAck": {
        "type": ("enum", "attach_ack"),
        "version": ("int", 1, 1),
        "request_id": ("id",),
        "broker_id": ("id",),
        "session_id": ("id",),
        "accepted": ("bool",),
        "reason": ("str", 256),
    },
    "AppshotAttachCommit": {
        "type": ("enum", "attach_commit"),
        "version": ("int", 1, 1),
        "request_id": ("id",),
        "broker_id": ("id",),
        "session_id": ("id",),
        "manifest_path": ("path",),
    },
    "AppshotAttachRevoke": {
        "type": ("enum", "attach_revoke"),
        "version": ("int", 1, 1),
        "request_id": ("id",),
        "broker_id": ("id",),
        "session_id": ("id",),
        "reason": ("str", 256),
    },
    "AppshotRelease": {
        "type": ("enum", "release"),
        "version": ("int", 1, 1),
        "request_id": ("id",),
        "broker_id": ("id",),
        "session_id": ("id",),
    },
    "AppshotReleaseAck": {
        "type": ("enum", "release_ack"),
        "version": ("int", 1, 1),
        "request_id": ("id",),
        "broker_id": ("id",),
        "session_id": ("id",),
        "released": ("bool",),
    },
    "AppshotCommand": {
        "type": ("enum", "command"),
        "version": ("int", 1, 1),
        "request_id": ("id",),
        "broker_id": ("id",),
        "session_id": ("id",),
        "name": ("enum", "status", "enable", "disable", "shortcut"),
        "argument": ("str", 256),
    },
    "AppshotCommandResult": {
        "type": ("enum", "command_result"),
        "version": ("int", 1, 1),
        "request_id": ("id",),
        "broker_id": ("id",),
        "session_id": ("id",),
        "ok": ("bool",),
        "code": ("id",),
        "message": ("str", 1024),
    },
    "AppshotClientStateAck": {
        "type": ("enum", "client_state_ack"),
        "version": ("int", 1, 1),
        "request_id": ("id",),
        "broker_id": ("id",),
        "session_id": ("id",),
        "appshot_count": ("int", 0, 4),
        "can_accept": ("bool",),
    },
    "AppshotStatus": {
        "type": ("enum", "status"),
        "version": ("int", 1, 1),
        "request_id": ("id",),
        "broker_id": ("id",),
        "session_id": ("id",),
        "enabled": ("bool",),
        "chord": ("str", 128),
        "registration": ("enum", "registered", "conflict", "unavailable"),
        "connected_tuis": ("int", 0, 16),
        "permission": ("enum", "ready", "unavailable", "unknown"),
        "appshot_count": ("int", 0, 4),
        "can_accept": ("bool",),
    },
}
_MESSAGES = {
    "hello": "AppshotHello",
    "hello_ack": "AppshotHelloAck",
    "client_state": "AppshotClientState",
    "attach_offer": "AppshotAttachOffer",
    "attach_ack": "AppshotAttachAck",
    "attach_commit": "AppshotAttachCommit",
    "attach_revoke": "AppshotAttachRevoke",
    "release": "AppshotRelease",
    "release_ack": "AppshotReleaseAck",
    "command": "AppshotCommand",
    "command_result": "AppshotCommandResult",
    "client_state_ack": "AppshotClientStateAck",
    "status": "AppshotStatus",
}


def _pairs(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise AppshotValidationError("duplicate_key")
        result[k] = v
    return result


def _unicode(value):
    if isinstance(value, str):
        value.encode("utf-8", errors="strict")
    elif isinstance(value, dict):
        for key, item in value.items():
            _unicode(key)
            _unicode(item)
    elif isinstance(value, list):
        for item in value:
            _unicode(item)


def _decode(raw):
    try:
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not raw or len(raw) > MAX_FRAME_BYTES:
            raise AppshotValidationError("frame_size")
        text = raw.decode("utf-8", errors="strict")
        depth = 0
        quoted = False
        escaped = False
        for c in text:
            if quoted:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    quoted = False
            elif c == '"':
                quoted = True
            elif c in "[{":
                depth += 1
                if depth > 32:
                    raise AppshotValidationError("json_depth")
            elif c in "]}":
                depth -= 1
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(AppshotValidationError("nonfinite")),
        )
        _unicode(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, AppshotValidationError):
            raise
        raise AppshotValidationError("invalid_json") from exc


def _rule(v, r):
    if isinstance(r, str):
        return _model(v, r)
    k = r[0]
    valid = False
    if k == "int":
        valid = type(v) in (int, float) and r[1] <= v <= r[2] and math.isfinite(v) and int(v) == v
        if valid:
            return int(v)
    elif k == "coord":
        valid = type(v) in (int, float) and abs(v) <= 1000000 and math.isfinite(v)
    elif k == "extent":
        valid = type(v) in (int, float) and 0 < v <= 1000000 and math.isfinite(v)
        if valid:
            return float(v)
    elif k == "bool":
        valid = type(v) is bool
    elif k == "reasons":
        valid = (
            type(v) is list
            and len(v) <= 16
            and all(type(x) is str and len(x.encode("utf8")) <= 128 and "\0" not in x for x in v)
        )
        if valid:
            return tuple(v)
    elif type(v) is str:
        if k == "str":
            valid = len(v.encode("utf8")) <= r[1] and "\0" not in v
        elif k == "enum":
            valid = v in r[1:]
        elif k == "uint":
            valid = re.fullmatch(r"0|[1-9][0-9]{0,19}", v) is not None and int(v) <= 18446744073709551615
        elif k == "hash":
            valid = re.fullmatch("[0-9a-f]{64}", v) is not None
        elif k == "token":
            valid = re.fullmatch("[0-9a-f]{32}", v) is not None
        elif k == "id":
            valid = re.fullmatch("[A-Za-z0-9_-]{1,128}", v) is not None
        elif k == "path":
            valid = (
                v.startswith("/")
                and len(v.encode("utf8")) <= 4096
                and "\0" not in v
                and all(x not in (".", "..") for x in v.split("/"))
            )
        elif k == "date":
            valid = re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", v) is not None
            if valid:
                try:
                    datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
                except ValueError:
                    valid = False
    if not valid:
        raise AppshotValidationError("invalid_" + k)
    return v


def _model(v, n):
    fs = _SCHEMAS[n]
    if type(v) is not dict or set(v) != set(fs):
        raise AppshotValidationError("invalid_fields")
    return globals()[n](**{k: _rule(v[k], r) for k, r in fs.items()})


def parse_appshot_manifest(raw: bytes | str) -> AppshotManifest:
    v = _decode(raw)
    if type(v) is dict and v.get("schema_version") != 1:
        raise AppshotValidationError("unsupported_schema")
    m = _model(v, "AppshotManifest")
    if m.png.name != f"appshot-{m.token}.png" or m.ax.name != f"appshot-{m.token}.ax.json":
        raise AppshotValidationError("invalid_name")
    if m.png.width * m.png.height > 32000000:
        raise AppshotValidationError("pixel_limit")
    if m.ax.coverage == "unavailable" and (
        m.ax.node_count != 0 or m.ax.depth != 0 or not m.ax.truncated or not m.ax.truncation_reasons
    ):
        raise AppshotValidationError("invalid_ax")
    return m


def parse_appshot_message(raw: bytes | str):
    v = _decode(raw)
    if type(v) is not dict or type(v.get("type")) is not str or v["type"] not in _MESSAGES:
        raise AppshotValidationError("unknown_message")
    return _model(v, _MESSAGES[v["type"]])


def encode_appshot_message(message) -> bytes:
    from dataclasses import asdict

    raw = json.dumps(asdict(message), separators=(",", ":")).encode()
    parse_appshot_message(raw)
    if len(raw) + 1 > MAX_FRAME_BYTES:
        raise AppshotValidationError("frame_size")
    return raw + b"\n"


class AppshotFrameDecoder:
    def __init__(self):
        self._buffer = bytearray()

    def feed(self, chunk: bytes):
        result = []
        for byte in chunk:
            if len(self._buffer) + 1 > MAX_FRAME_BYTES:
                self._buffer.clear()
                raise AppshotValidationError("frame_size")
            if byte == 10:
                raw = bytes(self._buffer)
                self._buffer.clear()
                result.append(parse_appshot_message(raw))
            else:
                self._buffer.append(byte)
        return result

    def finish(self):
        if self._buffer:
            self._buffer.clear()
            raise AppshotValidationError("incomplete_frame")


AppshotClientMessage = AppshotHello | AppshotClientState | AppshotAttachAck | AppshotRelease | AppshotCommand
AppshotBrokerMessage = (
    AppshotHelloAck
    | AppshotAttachOffer
    | AppshotAttachCommit
    | AppshotAttachRevoke
    | AppshotReleaseAck
    | AppshotCommandResult
    | AppshotClientStateAck
    | AppshotStatus
)


def parse_appshot_client_message(raw: bytes | str) -> AppshotClientMessage:
    message = parse_appshot_message(raw)
    if message.type not in ("hello", "client_state", "attach_ack", "release", "command"):
        raise AppshotValidationError("wrong_direction")
    return message


def parse_appshot_broker_message(raw: bytes | str) -> AppshotBrokerMessage:
    message = parse_appshot_message(raw)
    if message.type in ("hello", "client_state", "attach_ack", "release", "command"):
        raise AppshotValidationError("wrong_direction")
    return message


@dataclass(frozen=True)
class DecodedAppshot:
    png_bytes: bytes
    width: int
    height: int
    source: dict
    projection: dict

    @property
    def image_data_url(self):
        import base64

        return "data:image/png;base64," + base64.b64encode(self.png_bytes).decode("ascii")


def validate_png(raw: bytes, width: int, height: int) -> None:
    """Validate bounded, single-frame PNG structure and pixels before accepting bytes."""
    import io
    import struct
    import zlib
    from PIL import Image

    if not (1 <= width <= 16384 and 1 <= height <= 16384 and width * height <= 32000000):
        raise AppshotValidationError("pixel_limit")
    if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) > 10485760:
        raise AppshotValidationError("invalid_png")
    offset = 8
    ended = False
    while offset + 12 <= len(raw):
        size = int.from_bytes(raw[offset : offset + 4], "big")
        kind = raw[offset + 4 : offset + 8]
        end = offset + 12 + size
        if end > len(raw) or kind in (b"acTL", b"fcTL", b"fdAT"):
            raise AppshotValidationError("invalid_png")
        data = raw[offset + 8 : end - 4]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != int.from_bytes(raw[end - 4 : end], "big"):
            raise AppshotValidationError("invalid_png")
        if offset == 8 and (kind != b"IHDR" or size != 13 or struct.unpack(">II", data[:8]) != (width, height)):
            raise AppshotValidationError("invalid_png_dimensions")
        offset = end
        if kind == b"IEND":
            ended = size == 0 and offset == len(raw)
            break
    if not ended:
        raise AppshotValidationError("invalid_png")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG" or image.size != (width, height) or getattr(image, "n_frames", 1) != 1:
                raise AppshotValidationError("invalid_png")
            image.load()
    except Exception as exc:
        raise AppshotValidationError("invalid_png") from exc


def validate_ax(raw: bytes, descriptor: AppshotAX) -> dict:
    """Task 5 native envelope, root depth zero; no actionable element references."""
    if not raw or len(raw) > 262144:
        raise AppshotValidationError("ax_size")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
        _unicode(value)
        if (
            type(value) is not dict
            or set(value) != {"schema_version", "metadata", "root"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
        ):
            raise ValueError()
        metadata = {
            key: getattr(descriptor, key)
            for key in ("coverage", "node_count", "depth", "truncated", "truncation_reasons")
        }
        metadata["truncation_reasons"] = list(metadata["truncation_reasons"])
        if (
            value["metadata"] != metadata
            or type(value["metadata"].get("truncated")) is not bool
            or any(type(value["metadata"].get(k)) is not int for k in ("node_count", "depth"))
        ):
            raise ValueError()
        count = 0
        max_depth = 0

        def visit(node, depth):
            nonlocal count, max_depth
            if type(node) is not dict or depth > 64:
                raise ValueError()
            if not node and depth == 0 and descriptor.node_count == 0:
                return
            allowed = {
                "role",
                "subrole",
                "label",
                "title",
                "help",
                "value",
                "redacted",
                "bounds",
                "enabled",
                "focused",
                "children",
            }
            if "role" not in node or set(node) - allowed:
                raise ValueError()
            count += 1
            max_depth = max(max_depth, depth)
            if count > 2000:
                raise ValueError()
            for key, item in node.items():
                if key in {"role", "subrole", "label", "title", "help", "value"}:
                    if (
                        type(item) is not str
                        or len(item.encode("utf-8")) > (262144 if key == "value" else 4096)
                        or "\0" in item
                    ):
                        raise ValueError()
                elif key in {"redacted", "enabled", "focused"}:
                    if type(item) is not bool or (key == "redacted" and item is not True):
                        raise ValueError()
                elif key == "bounds":
                    if type(item) is not dict or set(item) != {"x", "y", "width", "height"}:
                        raise ValueError()
                    if (
                        any(type(x) not in (float, int) or not math.isfinite(x) for x in item.values())
                        or item["width"] < 0
                        or item["height"] < 0
                    ):
                        raise ValueError()
                elif key == "children":
                    if type(item) is not list or not item:
                        raise ValueError()
                    for child in item:
                        visit(child, depth + 1)
            if node.get("redacted") and "value" in node:
                raise ValueError()

        if descriptor.coverage == "unavailable" and (
            value["root"] != {} or descriptor.node_count != 0 or descriptor.depth != 0
            or not descriptor.truncated or not descriptor.truncation_reasons
        ):
            raise ValueError()
        visit(value["root"], 0)
        if (count, max_depth) != (descriptor.node_count, descriptor.depth) or descriptor.truncated != bool(
            descriptor.truncation_reasons
        ):
            raise ValueError()
        return value
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise AppshotValidationError("invalid_ax") from exc


class AppshotVerifier:
    """Held, nofollow authority. runtime_root is an explicit private-test seam only."""

    _fds: list[int]
    _members: list[tuple[str, int, stat_result]]
    root: Path
    _directory: int
    _dir_stat: stat_result
    manifest: AppshotManifest
    _png: bytes
    _ax: bytes

    @classmethod
    def open(
        cls,
        manifest_path: str,
        *,
        expected_broker_id: str,
        expected_session_id: str,
        expected_process_start: str,
        runtime_root=None,
    ):
        import os
        import stat
        from pathlib import Path

        if sys.platform == "win32" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "getuid"):
            raise AppshotValidationError("unsupported_platform")
        obj = cls()
        obj._fds = []
        obj._members = []
        obj.root = Path(runtime_root) if runtime_root is not None else Path(f"/private/tmp/astra-appshot-{os.getuid()}")
        path = Path(manifest_path)
        if (
            str(path) != manifest_path
            or path.parent != obj.root
            or obj.root.resolve() != obj.root
            or not re.fullmatch(r"appshot-[0-9a-f]{32}\.manifest\.json", path.name)
        ):
            raise AppshotValidationError("artifact_path")
        try:
            obj._directory = os.open(obj.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
            obj._fds.append(obj._directory)
            obj._dir_stat = os.fstat(obj._directory)
            if (
                not stat.S_ISDIR(obj._dir_stat.st_mode)
                or obj._dir_stat.st_uid != os.getuid()
                or stat.S_IMODE(obj._dir_stat.st_mode) != 0o700
            ):
                raise AppshotValidationError("artifact_directory")
            raw = obj._read_member(path.name, MAX_MANIFEST_BYTES)
            obj.manifest = m = parse_appshot_manifest(raw)
            if path.name != f"appshot-{m.token}.manifest.json":
                raise AppshotValidationError("artifact_name")
            if m.broker != AppshotBrokerBinding(expected_broker_id, expected_session_id, expected_process_start):
                raise AppshotValidationError("binding_mismatch")
            obj._png = obj._read_member(m.png.name, 10485760, m.png)
            obj._ax = obj._read_member(m.ax.name, 262144, m.ax)
            obj._recheck()
            return obj
        except Exception as exc:
            obj.close()
            if isinstance(exc, AppshotValidationError):
                raise
            if isinstance(exc, FileNotFoundError):
                raise AppshotValidationError("artifact_missing") from exc
            raise AppshotValidationError("artifact_authority") from exc

    @staticmethod
    def _identity(st):
        return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

    def _read_member(self, name, limit, descriptor=None):
        import os
        import stat
        import hashlib

        if sys.platform == "win32":
            raise AppshotValidationError("unsupported_platform")
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._directory)
        self._fds.append(fd)
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
            or stat.S_IMODE(st.st_mode) != 0o600
            or st.st_nlink != 1
            or not 0 < st.st_size <= limit
        ):
            raise AppshotValidationError("artifact_authority")
        if descriptor and (
            str(st.st_dev),
            str(st.st_ino),
            st.st_uid,
            stat.S_IMODE(st.st_mode),
            st.st_nlink,
            st.st_size,
        ) != (
            descriptor.device,
            descriptor.inode,
            descriptor.owner,
            descriptor.mode,
            descriptor.link_count,
            descriptor.size,
        ):
            raise AppshotValidationError("artifact_identity_changed")
        chunks = []
        remaining = st.st_size + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != st.st_size or (descriptor and hashlib.sha256(raw).hexdigest() != descriptor.sha256):
            raise AppshotValidationError("artifact_hash")
        self._members.append((name, fd, st))
        return raw

    def _recheck(self):
        import os

        try:
            named_dir = os.stat(self.root, follow_symlinks=False)
            if (named_dir.st_dev, named_dir.st_ino, named_dir.st_mode, named_dir.st_uid) != (
                self._dir_stat.st_dev,
                self._dir_stat.st_ino,
                self._dir_stat.st_mode,
                self._dir_stat.st_uid,
            ):
                raise AppshotValidationError("artifact_identity_changed")
            for name, fd, original in self._members:
                if self._identity(original) != self._identity(os.fstat(fd)) or self._identity(
                    original
                ) != self._identity(os.stat(name, dir_fd=self._directory, follow_symlinks=False)):
                    raise AppshotValidationError("artifact_identity_changed")
        except OSError as exc:
            raise AppshotValidationError("artifact_identity_changed") from exc

    def decode(self):
        from dataclasses import asdict

        self._recheck()
        validate_png(self._png, self.manifest.png.width, self.manifest.png.height)
        projection = validate_ax(self._ax, self.manifest.ax)
        self._recheck()
        # Only semantic, bounded observed metadata crosses into model content.
        source = {
            key: value
            for key, value in asdict(self.manifest.source).items()
            if key in ("app_label", "window_title", "bundle_id")
        }
        return DecodedAppshot(self._png, self.manifest.png.width, self.manifest.png.height, source, projection)

    def close(self):
        import os

        for fd in reversed(self._fds):
            os.close(fd)
        self._fds.clear()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
