"""Windows Appshot v2 wire validation, without filesystem or process authority.

The v1 entry points intentionally remain v1-only. Enabling Windows admission
requires a separately verified native identity and handle-based media backend.
"""
from __future__ import annotations

import re
from typing import Any, NoReturn

from agent.cli.appshots import AppshotValidationError, _decode, _rule

_MODELS = {
    "WindowsProcess": {
        "pid": ["int",1,2147483647],
        "process_start": ["uint"],
        "user_sid": ["sid"],
    },
    "WindowsSource": {
        "process": "WindowsProcess",
        "app_id": ["str",256],
        "app_label": ["str",256],
        "window_title": ["str",1024],
        "window_handle": ["uint"],
        "bounds": "AppshotBounds",
    },
    "WindowsFile": {
        "volume_serial": ["uint"],
        "file_id": ["token"],
        "owner_sid": ["sid"],
        "link_count": ["int",1,1],
    },
    "WindowsPNG": {
        "name": ["str",128],
        "size": ["int",1,10485760],
        "width": ["int",1,16384],
        "height": ["int",1,16384],
        "sha256": ["hash"],
        "identity": "WindowsFile",
    },
    "WindowsUIA": {
        "name": ["str",128],
        "size": ["int",1,262144],
        "sha256": ["hash"],
        "identity": "WindowsFile",
        "coverage": ["enum","reported_uia_subtree","unavailable"],
        "node_count": ["int",0,2000],
        "depth": ["int",0,64],
        "truncated": ["bool"],
        "truncation_reasons": ["reasons"],
    },
    "WindowsBroker": {
        "instance_id": ["id"],
        "session_id": ["id"],
        "recipient": "WindowsProcess",
    },
    "WindowsManifest": {
        "schema_version": ["int",2,2],
        "platform": ["enum","windows"],
        "token": ["token"],
        "captured_at": ["date"],
        "source": "WindowsSource",
        "png": "WindowsPNG",
        "uia": "WindowsUIA",
        "broker": "WindowsBroker",
    },
    "AppshotBounds": {
        "x": ["coord"],
        "y": ["coord"],
        "width": ["extent"],
        "height": ["extent"],
    },
}
_MESSAGES = {
    "hello": {
        "version": ["int",2,2],
        "platform": ["enum","windows"],
        "type": ["enum","hello"],
        "session_id": ["id"],
        "pid": ["int",1,2147483647],
        "process_start": ["uint"],
        "user_sid": ["sid"],
        "client_nonce": ["id"],
    },
    "hello_ack": {
        "version": ["int",2,2],
        "platform": ["enum","windows"],
        "type": ["enum","hello_ack"],
        "instance_id": ["id"],
        "broker_nonce": ["id"],
        "session_id": ["id"],
    },
    "client_state": {
        "type": ["enum","client_state"],
        "version": ["int",2,2],
        "platform": ["enum","windows"],
        "request_id": ["id"],
        "broker_id": ["id"],
        "session_id": ["id"],
        "activity_ns": ["uint"],
        "appshot_count": ["int",0,4],
        "can_accept": ["bool"],
    },
    "attach_offer": {
        "type": ["enum","attach_offer"],
        "version": ["int",2,2],
        "platform": ["enum","windows"],
        "request_id": ["id"],
        "broker_id": ["id"],
        "session_id": ["id"],
        "manifest_path": ["windowsPath"],
    },
    "attach_ack": {
        "type": ["enum","attach_ack"],
        "version": ["int",2,2],
        "platform": ["enum","windows"],
        "request_id": ["id"],
        "broker_id": ["id"],
        "session_id": ["id"],
        "accepted": ["bool"],
        "reason": ["str",256],
    },
    "attach_commit": {
        "type": ["enum","attach_commit"],
        "version": ["int",2,2],
        "platform": ["enum","windows"],
        "request_id": ["id"],
        "broker_id": ["id"],
        "session_id": ["id"],
        "manifest_path": ["windowsPath"],
    },
    "attach_revoke": {
        "type": ["enum","attach_revoke"],
        "version": ["int",2,2],
        "platform": ["enum","windows"],
        "request_id": ["id"],
        "broker_id": ["id"],
        "session_id": ["id"],
        "reason": ["str",256],
    },
    "release": {
        "type": ["enum","release"],
        "version": ["int",2,2],
        "platform": ["enum","windows"],
        "request_id": ["id"],
        "broker_id": ["id"],
        "session_id": ["id"],
    },
    "release_ack": {
        "type": ["enum","release_ack"],
        "version": ["int",2,2],
        "platform": ["enum","windows"],
        "request_id": ["id"],
        "broker_id": ["id"],
        "session_id": ["id"],
        "released": ["bool"],
    },
    "command": {
        "type": ["enum","command"],
        "version": ["int",2,2],
        "platform": ["enum","windows"],
        "request_id": ["id"],
        "broker_id": ["id"],
        "session_id": ["id"],
        "name": ["enum","status","enable","disable","shortcut"],
        "argument": ["str",256],
    },
    "command_result": {
        "type": ["enum","command_result"],
        "version": ["int",2,2],
        "platform": ["enum","windows"],
        "request_id": ["id"],
        "broker_id": ["id"],
        "session_id": ["id"],
        "ok": ["bool"],
        "code": ["id"],
        "message": ["str",1024],
    },
    "client_state_ack": {
        "type": ["enum","client_state_ack"],
        "version": ["int",2,2],
        "platform": ["enum","windows"],
        "request_id": ["id"],
        "broker_id": ["id"],
        "session_id": ["id"],
        "appshot_count": ["int",0,4],
        "can_accept": ["bool"],
    },
    "status": {
        "type": ["enum","status"],
        "version": ["int",2,2],
        "platform": ["enum","windows"],
        "request_id": ["id"],
        "broker_id": ["id"],
        "session_id": ["id"],
        "enabled": ["bool"],
        "chord": ["str",128],
        "registration": ["enum","registered","conflict","unavailable"],
        "connected_tuis": ["int",0,16],
        "permission": ["enum","ready","unavailable","unknown"],
        "appshot_count": ["int",0,4],
        "can_accept": ["bool"],
    },
}


def _invalid() -> NoReturn:
    raise AppshotValidationError("invalid_windows_contract")


def _windows_rule(value: Any, rule: list | str) -> Any:
    if isinstance(rule, str):
        return _model(value, _MODELS[rule])
    if rule[0] == "sid":
        if type(value) is not str:
            _invalid()
        parts = value.split("-")
        if not 4 <= len(parts) <= 18 or parts[:2] != ["S", "1"]:
            _invalid()
        for i, part in enumerate(parts[2:]):
            if not re.fullmatch(r"0|[1-9][0-9]{0,14}", part):
                _invalid()
            if int(part) > (281474976710655 if i == 0 else 4294967295):
                _invalid()
        return value
    if rule[0] == "windowsPath":
        if type(value) is not str or len(value.encode("utf8")) > 4096 or not re.match(r"^[A-Z]:/", value):
            _invalid()
        for part in value[3:].split("/"):
            if (not part or part in (".", "..") or part.endswith((".", " "))
                or re.search(r'[\x00-\x1f<>:"\\|?*]', part)
                or re.fullmatch(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?", part, re.I)):
                _invalid()
        return value
    return _rule(value, rule)


def _model(value: Any, fields: dict | None) -> dict:
    if fields is None or type(value) is not dict or set(value) != set(fields):
        _invalid()
    return {key: _windows_rule(value[key], rule) for key, rule in fields.items()}


def parse_windows_appshot_manifest(raw: bytes | str) -> dict:
    m = _model(_decode(raw), _MODELS["WindowsManifest"])
    source, png, uia, broker = (m[key] for key in ("source", "png", "uia", "broker"))
    if (source["window_handle"] == "0" or source["process"]["process_start"] == "0"
        or broker["recipient"]["process_start"] == "0" or png["width"] * png["height"] > 32000000
        or png["name"] != f"appshot-{m['token']}.png" or uia["name"] != f"appshot-{m['token']}.uia.json"
        or any(sid != broker["recipient"]["user_sid"] for sid in (
            png["identity"]["owner_sid"], uia["identity"]["owner_sid"], source["process"]["user_sid"]))):
        _invalid()
    if uia["coverage"] == "unavailable" and (
        uia["node_count"] != 0 or uia["depth"] != 0 or not uia["truncated"] or not uia["truncation_reasons"]
    ):
        _invalid()
    return m


def parse_windows_appshot_message(raw: bytes | str) -> dict:
    value = _decode(raw)
    if type(value) is not dict or type(value.get("type")) is not str:
        _invalid()
    m = _model(value, _MESSAGES.get(value["type"]))
    if m["type"] == "hello" and m["process_start"] == "0":
        _invalid()
    return m
