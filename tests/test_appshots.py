import json
from pathlib import Path
import pytest
from agent.cli.appshots import parse_appshot_manifest, parse_appshot_message, AppshotValidationError

FIXTURE = Path(__file__).resolve().parents[1] / "native/macos-computer-helper/Tests/Fixtures"


def test_manifest_fixture():
    m = parse_appshot_manifest((FIXTURE / "appshot_manifest_v1.json").read_bytes())
    assert m.png.name == f"appshot-{m.token}.png"
    assert m.source.process_start == "9001"


def test_lifecycle_fixtures():
    for m in json.loads((FIXTURE / "appshot_messages_v1.json").read_text()):
        assert parse_appshot_message(json.dumps(m).encode()).type == m["type"]


@pytest.mark.parametrize(
    "raw", [b'{"schema_version":1,"schema_version":1}', b'{"schema_version":2}', b"[]", b"", b" " * 65537],
    ids=['duplicate-schema', 'unsupported-schema', 'array', 'empty', 'oversized'],
)
def test_invalid_json(raw):
    with pytest.raises(AppshotValidationError):
        parse_appshot_manifest(raw)


@pytest.mark.parametrize(
    "path,value",
    [
        ("png.name", "../x"),
        ("png.sha256", "A" * 64),
        ("png.width", 0),
        ("png.mode", 420),
        ("png.device", 1),
        ("png.inode", "01"),
        ("source.process_start", "18446744073709551616"),
        ("ax.node_count", 2001),
        ("ax.depth", 65),
        ("ax.coverage", "all"),
        ("source.app_label", "x" * 257),
        ("source.window_title", "x" * 1025),
        ("png.extra", 1),
        ("png.link_count", 2),
    ],
)
def test_manifest_mutations(path, value):
    m = json.loads((FIXTURE / "appshot_manifest_v1.json").read_text())
    a, b = path.split(".")
    m[a][b] = value
    with pytest.raises(AppshotValidationError):
        parse_appshot_manifest(json.dumps(m).encode())


def test_messages_reject_unknown_extra_and_bad_numbers():
    for original in json.loads((FIXTURE / "appshot_messages_v1.json").read_text()):
        for key, value in [("type", "unknown"), ("version", 2), ("extra", True)]:
            m = {**original, key: value}
            with pytest.raises(AppshotValidationError):
                parse_appshot_message(json.dumps(m).encode())


def test_framing_and_direction():
    from agent.cli.appshots import (
        AppshotFrameDecoder,
        encode_appshot_message,
        parse_appshot_client_message,
        parse_appshot_broker_message,
    )

    rows = json.loads((FIXTURE / "appshot_messages_v1.json").read_text())
    for i, row in enumerate(rows):
        raw = json.dumps(row).encode()
        m = parse_appshot_message(raw)
        frame = encode_appshot_message(m)
        decoder = AppshotFrameDecoder()
        assert decoder.feed(frame[:7]) == []
        assert decoder.feed(frame[7:]) == [m]
        decoder.finish()
        parser = (
            parse_appshot_client_message
            if row["type"] in ["hello", "client_state", "attach_ack", "release", "command"]
            else parse_appshot_broker_message
        )
        assert parser(raw) == m
    for raw in [b"\n", b" " * 65537, b'{"type":"hello","ty\\u0070e":"hello"}\n', b"[" * 33 + b"]" * 33 + b"\n"]:
        with pytest.raises(AppshotValidationError):
            AppshotFrameDecoder().feed(raw)
    decoder = AppshotFrameDecoder()
    decoder.feed(b"{")
    with pytest.raises(AppshotValidationError):
        decoder.finish()


@pytest.mark.parametrize("case", json.loads((FIXTURE / "appshot_protocol_cases_v1.json").read_text()))
def test_authoritative_protocol_cases(case):
    parser = parse_appshot_manifest if case["kind"] == "manifest" else parse_appshot_message
    if case["valid"]:
        parser(case["raw"].encode())
    else:
        with pytest.raises(AppshotValidationError):
            parser(case["raw"].encode())


@pytest.mark.parametrize("bad_type", [[], {}, None, True, 1])
def test_message_type_failure_is_validation_error(bad_type):
    with pytest.raises(AppshotValidationError):
        parse_appshot_message(json.dumps({"type": bad_type}).encode())


def test_huge_pid_failure_is_validation_error():
    hello = json.loads((FIXTURE / "appshot_messages_v1.json").read_text())[0]
    hello["pid"] = 9 * 10**308
    with pytest.raises(AppshotValidationError):
        parse_appshot_message(json.dumps(hello).encode())


def test_source_bounds_preserve_fractional_points():
    manifest = json.loads((FIXTURE / "appshot_manifest_v1.json").read_text())
    manifest["source"]["bounds"].update(width=800.5, height=600.25)
    parsed = parse_appshot_manifest(json.dumps(manifest).encode())
    assert parsed.source.bounds.width == 800.5
    assert parsed.source.bounds.height == 600.25
    assert type(parsed.png.width) is int
