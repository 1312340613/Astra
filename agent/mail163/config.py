import base64
import binascii
import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

from .models import MailConfig

_DEFAULT_MAX_BODY_BYTES = 5 * 1024 * 1024
_DIRECT_MAILBOX_CHARACTERS = set(range(0x20, 0x7F)) - {ord("&")}
_FOLDER_ALIASES = {
    "inbox": "INBOX",
    "received": "INBOX",
    "收件箱": "INBOX",
    "sent": "已发送",
    "sent items": "已发送",
    "已发送": "已发送",
}


class MailConfigurationError(ValueError):
    """Raised when required 163 mail configuration is missing or invalid."""


def load_mail_config(
    project_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
    require_credentials: bool = True,
) -> MailConfig:
    """Load 163 mail settings without creating local state."""
    dotenv = {key: value for key, value in dotenv_values(project_root / ".env").items() if value is not None}
    environment = os.environ if environ is None else environ
    values = {**dotenv, **environment}

    account = values.get("ASTRA_163_EMAIL", "") if require_credentials else ""
    auth_code = values.get("ASTRA_163_AUTH_CODE", "") if require_credentials else ""
    if require_credentials and not account:
        raise MailConfigurationError("ASTRA_163_EMAIL is required")
    if require_credentials and not auth_code:
        raise MailConfigurationError("ASTRA_163_AUTH_CODE is required")

    max_body_bytes = _read_positive_int(values.get("ASTRA_163_MAX_BODY_BYTES"), "ASTRA_163_MAX_BODY_BYTES")
    mail_dir = project_root / ".astra" / "mail"
    return MailConfig(
        project_root=project_root,
        account=account,
        auth_code=auth_code,
        host="imap.163.com",
        port=993,
        database_path=mail_dir / "163.sqlite3",
        attachments_dir=mail_dir / "attachments",
        max_body_bytes=max_body_bytes,
        default_folders=("INBOX", "已发送"),
    )


def resolve_folder(value: str) -> str:
    """Return the canonical mailbox name for a supported folder alias."""
    return _FOLDER_ALIASES.get(value.strip().casefold(), value)


def encode_mailbox_name(value: str) -> bytes:
    """Encode a Unicode mailbox name using RFC 3501 modified UTF-7."""
    encoded = bytearray()
    non_direct: list[str] = []

    def flush_non_direct() -> None:
        if not non_direct:
            return
        utf16 = "".join(non_direct).encode("utf-16-be")
        shifted = base64.b64encode(utf16).rstrip(b"=").replace(b"/", b",")
        encoded.extend(b"&" + shifted + b"-")
        non_direct.clear()

    for character in value:
        if ord(character) in _DIRECT_MAILBOX_CHARACTERS:
            flush_non_direct()
            encoded.extend(character.encode("ascii"))
        elif character == "&":
            flush_non_direct()
            encoded.extend(b"&-")
        else:
            non_direct.append(character)
    flush_non_direct()
    return bytes(encoded)


def decode_mailbox_name(value: bytes) -> str:
    """Decode an RFC 3501 modified UTF-7 mailbox name."""
    decoded: list[str] = []
    index = 0
    while index < len(value):
        current = value[index]
        if current == ord("&"):
            end = value.find(b"-", index + 1)
            if end == -1:
                raise ValueError("unterminated modified UTF-7 sequence")
            shifted = value[index + 1 : end]
            if not shifted:
                decoded.append("&")
            else:
                padding = b"=" * (-len(shifted) % 4)
                try:
                    utf16 = base64.b64decode(shifted.replace(b",", b"/") + padding, validate=True)
                    decoded.append(utf16.decode("utf-16-be"))
                except (UnicodeDecodeError, binascii.Error) as error:
                    raise ValueError("invalid modified UTF-7 sequence") from error
            index = end + 1
            continue
        if current not in _DIRECT_MAILBOX_CHARACTERS:
            raise ValueError("invalid direct modified UTF-7 byte")
        decoded.append(chr(current))
        index += 1
    return "".join(decoded)


def _read_positive_int(value: str | None, name: str) -> int:
    if value is None or value == "":
        return _DEFAULT_MAX_BODY_BYTES
    try:
        parsed = int(value)
    except ValueError as error:
        raise MailConfigurationError(f"{name} must be a positive integer") from error
    if parsed <= 0:
        raise MailConfigurationError(f"{name} must be a positive integer")
    return parsed
