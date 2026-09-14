"""Provider-only message-time marker filtering."""

from __future__ import annotations

import inspect
import re
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

_OPEN = "<message_time>"
_CLOSE = "</message_time>"
_MAX_MARKER_CHARS = 96
_LEADING_MARKER = re.compile(
    r"^<message_time>"
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:Z|[+-]\d{2}:\d{2})"
    r"(?: 周[一二三四五六日])?"
    r"</message_time>(?:\r?\n)?"
)


def strip_leading_message_time_marker(text: str) -> str:
    """Remove one valid internal marker from the start of complete text."""
    return _LEADING_MARKER.sub("", text, count=1)


class LeadingMessageTimeFilter:
    """Suppress one valid leading marker without leaking split chunks."""

    def __init__(self) -> None:
        self._buffer = ""
        self._resolved = False

    def feed(self, chunk: str) -> str:
        if self._resolved or not chunk:
            return chunk

        self._buffer += chunk
        if _OPEN.startswith(self._buffer):
            return ""
        if not self._buffer.startswith(_OPEN):
            return self._release()
        if _CLOSE not in self._buffer and len(self._buffer) <= _MAX_MARKER_CHARS:
            return ""

        close_end = self._buffer.find(_CLOSE) + len(_CLOSE)
        if (
            self._buffer[close_end:] in {"", "\r"}
            and len(self._buffer) <= _MAX_MARKER_CHARS
        ):
            # Wait for one more chunk so a split LF or CRLF is suppressed too.
            return ""

        candidate = strip_leading_message_time_marker(self._buffer)
        if candidate == self._buffer:
            return self._release()
        self._buffer = ""
        self._resolved = True
        return candidate

    def finish(self) -> str:
        candidate = strip_leading_message_time_marker(self._buffer)
        if candidate != self._buffer:
            self._buffer = ""
            self._resolved = True
            return candidate
        return self._release()

    def _release(self) -> str:
        value = self._buffer
        self._buffer = ""
        self._resolved = True
        return value


async def filter_message_time_events(
    events: AsyncIterator[dict[str, Any]],
) -> AsyncGenerator[dict[str, Any], None]:
    """Filter assistant prose while preserving event protocol and ownership."""
    boundary = LeadingMessageTimeFilter()
    try:
        async for event in events:
            filtered = dict(event)
            event_type = filtered.get("type")
            if event_type == "chunk":
                content = boundary.feed(str(filtered.get("content") or ""))
                if content:
                    filtered["content"] = content
                    yield filtered
                continue
            if event_type in {"tool_calls", "done"}:
                trailing = boundary.finish()
                if trailing:
                    yield {"type": "chunk", "content": trailing}
                if isinstance(filtered.get("content"), str):
                    filtered["content"] = strip_leading_message_time_marker(
                        filtered["content"]
                    )
            yield filtered
    finally:
        close = getattr(events, "aclose", None)
        if callable(close):
            closing = close()
            if not inspect.isawaitable(closing):
                raise TypeError("Event stream aclose() must return an awaitable")
            await closing
