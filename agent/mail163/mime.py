import base64
import binascii
import quopri
import re
import unicodedata
from email.header import decode_header
from email.utils import collapse_rfc2231_value, decode_params, unquote
from html.parser import HTMLParser
from typing import ClassVar, TypeAlias

from .models import BodyPart

_SExpression: TypeAlias = bytes | int | None | list["_SExpression"]
_MAX_BODYSTRUCTURE_DEPTH = 100
_WINDOWS_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class MailProtocolError(RuntimeError):
    """Raised when an IMAP response cannot be used safely."""


class _SExpressionParser:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._position = 0

    def parse(self) -> _SExpression:
        self._skip_spaces()
        if self._position >= len(self._payload):
            raise MailProtocolError("BODYSTRUCTURE response is empty")
        value = self._parse_value(depth=1)
        self._skip_spaces()
        if self._position != len(self._payload):
            raise MailProtocolError("BODYSTRUCTURE response has trailing data")
        return value

    def _parse_value(self, *, depth: int) -> _SExpression:
        if depth > _MAX_BODYSTRUCTURE_DEPTH:
            raise MailProtocolError("BODYSTRUCTURE nesting exceeds the safety limit")
        if self._position >= len(self._payload):
            raise MailProtocolError("BODYSTRUCTURE response is incomplete")
        current = self._payload[self._position]
        if current == ord("("):
            return self._parse_list(depth=depth)
        if current == ord('"'):
            return self._parse_quoted()
        if current == ord(")"):
            raise MailProtocolError("BODYSTRUCTURE response is unbalanced")
        return self._parse_atom()

    def _parse_list(self, *, depth: int) -> list[_SExpression]:
        self._position += 1
        values: list[_SExpression] = []
        while True:
            self._skip_spaces()
            if self._position >= len(self._payload):
                raise MailProtocolError("BODYSTRUCTURE response is unbalanced")
            if self._payload[self._position] == ord(")"):
                self._position += 1
                return values
            values.append(self._parse_value(depth=depth + 1))

    def _parse_quoted(self) -> bytes:
        self._position += 1
        value = bytearray()
        while self._position < len(self._payload):
            current = self._payload[self._position]
            self._position += 1
            if current == ord('"'):
                return bytes(value)
            if current == ord("\\"):
                if self._position >= len(self._payload):
                    raise MailProtocolError("BODYSTRUCTURE quoted string is incomplete")
                current = self._payload[self._position]
                self._position += 1
            value.append(current)
        raise MailProtocolError("BODYSTRUCTURE quoted string is unbalanced")

    def _parse_atom(self) -> _SExpression:
        start = self._position
        while self._position < len(self._payload):
            current = self._payload[self._position]
            if current in b" ()\t\r\n":
                break
            self._position += 1
        if start == self._position:
            raise MailProtocolError("BODYSTRUCTURE response contains an invalid atom")
        atom = self._payload[start : self._position]
        if atom.upper() == b"NIL":
            return None
        if atom.isdigit():
            return int(atom)
        return atom

    def _skip_spaces(self) -> None:
        while self._position < len(self._payload) and self._payload[self._position] in b" \t\r\n":
            self._position += 1


class _TextExtractor(HTMLParser):
    _BLOCK_TAGS: ClassVar[frozenset[str]] = frozenset(
        {
            "address",
            "article",
            "aside",
            "blockquote",
            "br",
            "div",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "li",
            "main",
            "p",
            "section",
            "table",
            "tr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.fragments: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in self._BLOCK_TAGS:
            self.fragments.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and tag in self._BLOCK_TAGS:
            self.fragments.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.fragments.append(data)


def parse_bodystructure(payload: bytes) -> tuple[BodyPart, ...]:
    """Parse a bounded IMAP BODYSTRUCTURE response into safe leaf metadata."""
    parsed = _SExpressionParser(payload).parse()
    if not isinstance(parsed, list) or not parsed:
        raise MailProtocolError("BODYSTRUCTURE response is not a body structure")
    parts: list[BodyPart] = []
    _collect_parts(parsed, prefix="", output=parts)
    if not parts:
        raise MailProtocolError("BODYSTRUCTURE response contains no body parts")
    return tuple(parts)


def decode_text_part(payload: bytes, part: BodyPart) -> str:
    """Decode a MIME text leaf and return normalized safe text."""
    text, _warnings, _truncated = _decode_text_part_with_warnings(payload, part)
    return text


def sanitize_filename(value: str) -> str:
    """Return a single portable filename without caller-controlled paths."""
    normalized = unicodedata.normalize("NFKC", value).replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1].strip()
    basename = _WINDOWS_UNSAFE_FILENAME.sub("_", basename).strip()
    if not basename or basename in {".", ".."}:
        return "attachment"
    return basename


def _collect_parts(node: list[_SExpression], *, prefix: str, output: list[BodyPart]) -> None:
    if node and isinstance(node[0], list):
        child_index = 0
        while child_index < len(node) and isinstance(node[child_index], list):
            child_index += 1
        if child_index == 0 or child_index >= len(node):
            raise MailProtocolError("BODYSTRUCTURE multipart response is incomplete")
        for number, child in enumerate(node[:child_index], start=1):
            if not isinstance(child, list):
                raise MailProtocolError("BODYSTRUCTURE multipart child is invalid")
            part_id = f"{prefix}.{number}" if prefix else str(number)
            _collect_parts(child, prefix=part_id, output=output)
        return

    if len(node) < 7:
        raise MailProtocolError("BODYSTRUCTURE leaf response is incomplete")
    media_type = _as_text(node[0]).lower()
    media_subtype = _as_text(node[1]).lower()
    if not media_type or not media_subtype:
        raise MailProtocolError("BODYSTRUCTURE leaf media type is invalid")
    parameters = _parameter_map(node[2])
    charset = parameters.get("charset", "")
    transfer_encoding = _as_text(node[5]).lower()
    encoded_size = node[6] if isinstance(node[6], int) else 0

    if media_type == "text":
        extension_start = 8
    elif media_type == "message" and media_subtype in {"rfc822", "global"}:
        extension_start = 10
    else:
        extension_start = 7
    disposition = ""
    disposition_parameters: dict[str, str] = {}
    disposition_index = extension_start + 1
    disposition_field = node[disposition_index] if len(node) > disposition_index else None
    if isinstance(disposition_field, list):
        if disposition_field:
            disposition = _as_text(disposition_field[0]).lower()
        if len(disposition_field) > 1:
            disposition_parameters = _parameter_map(disposition_field[1])

    filename = disposition_parameters.get("filename", "") or parameters.get("name", "")
    if filename:
        filename = sanitize_filename(_decode_mime_header(filename)[0])
    output.append(
        BodyPart(
            part_id=prefix or "1",
            content_type=f"{media_type}/{media_subtype}",
            charset=charset,
            transfer_encoding=transfer_encoding,
            encoded_size=encoded_size,
            disposition=disposition,
            filename=filename,
        )
    )


def _parameter_map(value: _SExpression) -> dict[str, str]:
    if not isinstance(value, list):
        return {}
    raw_parameters: list[tuple[str, str]] = []
    for index in range(0, len(value) - 1, 2):
        key = _as_text(value[index]).lower()
        if key:
            raw_parameters.append((key, _as_text(value[index + 1])))
    try:
        decoded_parameters = decode_params([("", ""), *raw_parameters])
    except (IndexError, TypeError, ValueError):
        return dict(raw_parameters)
    result: dict[str, str] = {}
    for key, encoded_value in decoded_parameters:
        if not key:
            continue
        if isinstance(encoded_value, tuple):
            decoded_value = collapse_rfc2231_value(encoded_value, errors="replace")
        else:
            decoded_value = encoded_value
        result[key.lower()] = unquote(decoded_value)
    return result


def _as_text(value: _SExpression) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, int):
        return str(value)
    return ""


def _decode_mime_header(value: str) -> tuple[str, tuple[str, ...]]:
    fragments: list[str] = []
    warnings: list[str] = []
    try:
        decoded = decode_header(value)
    except (LookupError, ValueError):
        return value, ("MIME header could not be decoded",)
    for fragment, charset in decoded:
        if isinstance(fragment, str):
            fragments.append(fragment)
            continue
        codec = charset or "ascii"
        try:
            fragments.append(fragment.decode(codec, errors="replace"))
        except LookupError:
            fragments.append(fragment.decode("utf-8", errors="replace"))
            warnings.append("MIME header used an unknown charset")
    return "".join(fragments), tuple(warnings)


def _decode_transfer(
    payload: bytes,
    transfer_encoding: str,
    *,
    strict: bool,
    allow_truncated: bool = False,
) -> tuple[bytes, tuple[str, ...]]:
    encoding = transfer_encoding.strip().lower()
    if encoding == "base64":
        unpadded = re.sub(rb"\s+", b"", payload)
        compact = unpadded + b"=" * (-len(unpadded) % 4)
        try:
            return base64.b64decode(compact, validate=True), ()
        except (binascii.Error, ValueError):
            if strict:
                raise MailProtocolError("attachment transfer decoding failed") from None
            if allow_truncated:
                complete_quanta = unpadded[: len(unpadded) - (len(unpadded) % 4)]
                try:
                    decoded = base64.b64decode(complete_quanta, validate=True) if complete_quanta else b""
                except (binascii.Error, ValueError):
                    decoded = b""
                else:
                    return decoded, ("body base64 transfer encoding was truncated",)
            return b"", ("body base64 transfer encoding was malformed",)
    if encoding == "quoted-printable":
        return quopri.decodestring(payload), ()
    if encoding in {"", "7bit", "8bit", "binary"}:
        return payload, ()
    if strict:
        raise MailProtocolError("attachment transfer encoding is unsupported")
    return b"", ("body transfer encoding was unsupported",)


def _decode_text_part_with_warnings(
    payload: bytes,
    part: BodyPart,
    *,
    max_decoded_bytes: int | None = None,
    allow_truncated_transfer: bool = False,
) -> tuple[str, tuple[str, ...], bool]:
    decoded, transfer_warnings = _decode_transfer(
        payload,
        part.transfer_encoding,
        strict=False,
        allow_truncated=allow_truncated_transfer,
    )
    warnings = list(transfer_warnings)
    decoded_truncated = max_decoded_bytes is not None and len(decoded) > max_decoded_bytes
    if max_decoded_bytes is not None:
        decoded = decoded[:max_decoded_bytes]
    charset = part.charset or "utf-8"
    try:
        text = decoded.decode(charset, errors="replace")
    except LookupError:
        text = decoded.decode("utf-8", errors="replace")
        warnings.append("body used an unknown charset; UTF-8 replacement was used")
    if part.content_type == "text/html":
        parser = _TextExtractor()
        parser.feed(text)
        parser.close()
        text = "".join(parser.fragments)
    return _normalize_whitespace(text), tuple(warnings), decoded_truncated


def _normalize_whitespace(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[^\S\n]+", " ", normalized)
    normalized = re.sub(r" *\n *", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()
