"""Recognize credential-bearing CU text at durable persistence boundaries.

This does not authorize input or change the text sent to an application. It
deliberately leaves ordinary prose, JSON formatting and URLs intact; arbitrary
unlabelled strings cannot reliably be identified as passwords.
"""

from __future__ import annotations

import json
import re
from typing import Any

_CREDENTIAL_KEYS = frozenset({
    "apikey", "accesstoken", "refreshtoken", "clientsecret", "token",
    "password", "passwd", "secret", "cookie", "authorization", "privatekey",
    "secretkey", "accesskey", "secretaccesskey",
})
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(?<![\w])(?:[a-z][a-z0-9]*_)*"
    r"(?:(?:api|secret|(?:secret[_-]?)?access)[_-]?key|"
    r"(?:access|refresh)[_-]?token|client[_-]?secret|token|"
    r"password|passwd|secret|cookie|authorization|private[_-]?key)"
    r"[\"']?\s*[:=]\s*(?P<value>\"(?:\\.|[^\"\\])*(?:\"|\\?$)|"
    r"'(?:\\.|[^'\\])*(?:'|\\?$)|[^\s,;\"']+)"
)
_BEARER_LINE = re.compile(r"(?im)^\s*Bearer\s+[a-z0-9._~+/-]+=*\s*$")
_URL_CREDENTIALS = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]*:[^\s/@]+@")
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
_JSON_STRING = re.compile(r'"(?:\\.|[^"\\])*(?:"|\\?$)')
_EMPTY_VALUES = frozenset({"''", '\"\"'})


def _credential_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return normalized in _CREDENTIAL_KEYS or bool(_CREDENTIAL_ASSIGNMENT.match(f"{key}=x"))


def _json_contains_credentials(value: Any) -> bool:
    # Iterative traversal also handles bounded but deeply nested model input.
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if any(_credential_key(key) and _has_credential_value(value) for key, value in item.items()):
                return True
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and _credential_syntax(item):
            return True
    return False


def _has_credential_value(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    return bool(value) if isinstance(value, (str, list, dict)) else True


def _credential_syntax(text: str) -> bool:
    if any(match["value"] not in _EMPTY_VALUES for match in _CREDENTIAL_ASSIGNMENT.finditer(text)):
        return True
    # A deeply nested JSON document can exceed the decoder's recursion limit.
    # Inspect escaped keys in its undecoded fragments instead of classifying
    # every undecodable document as a credential.
    for match in _JSON_STRING.finditer(text):
        end = match.end()
        while end < len(text) and text[end].isspace():
            end += 1
        if end == len(text) or text[end] != ":":
            continue
        try:
            key = json.loads(match[0])
        except (ValueError, RecursionError):
            continue
        if _credential_key(key):
            assignment = _CREDENTIAL_ASSIGNMENT.match(f"{key}={text[end + 1:]}")
            if assignment is not None and assignment["value"] not in _EMPTY_VALUES:
                return True
    return any(pattern.search(text) for pattern in (
        _BEARER_LINE, _URL_CREDENTIALS, _PRIVATE_KEY,
    ))


def contains_credentials(text: str) -> bool:
    """Recognize labelled credentials without transforming ordinary text."""
    decoder = json.JSONDecoder(parse_int=str)
    cursor = 0
    syntax_parts = []
    for candidate in re.finditer(r"[\[{]", text):
        if candidate.start() < cursor:
            continue
        try:
            value, end = decoder.raw_decode(text, candidate.start())
        except json.JSONDecodeError:
            continue
        except (RecursionError, ValueError):
            # Continue to inner values and scan undecoded text for explicit
            # credential syntax. Parser limits alone are not secret evidence.
            continue
        if _json_contains_credentials(value):
            return True
        # Keep surrounding assignment/URL syntax joined. Splitting and scanning
        # each side alone would miss api_key=[1234] or user:abc[0]def@host.
        # This is only a classification copy; retained input remains verbatim.
        syntax_parts.extend((text[cursor:candidate.start()], "__json_value__"))
        cursor = end
    syntax_parts.append(text[cursor:])
    return _credential_syntax("".join(syntax_parts))
