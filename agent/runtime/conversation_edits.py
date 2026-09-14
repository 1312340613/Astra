"""Conversation-history edits shared by the universal /undo and /retry commands.

The helpers operate on the active ``AgentContext`` regardless of which mode
owns it: normal, minimal and local sessions share the same message shapes,
so one implementation keeps every entry point in lockstep.
"""

from __future__ import annotations

from typing import Any

from .context import AgentContext


_RETRY_REFUSAL = "No request to retry."
_ATTACHMENT_REFUSAL = "This request includes an attachment; use /undo and resend it manually."
_TOOL_NOTE = " Tool side effects were not rolled back."


def _has_tool_activity(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        if message.get("role") == "tool" or message.get("tool_calls"):
            return True
    return False


def _is_user_request(message: dict[str, Any]) -> bool:
    # Tool images, delegate envelopes and notifications use role=user for
    # provider compatibility; they are still part of the surrounding turn.
    return message.get("role") == "user" and not message.get("provenance")


def _pop_trailing_reply(context: AgentContext) -> list[dict[str, Any]]:
    """Pop every trailing message after the last real user request."""
    popped: list[dict[str, Any]] = []
    while context.messages and not _is_user_request(context.messages[-1]):
        removed = context.remove_last_message()
        if removed is None:
            break
        popped.append(removed)
    return popped


def undo_last_reply(context: AgentContext) -> tuple[bool, str]:
    """Remove the trailing model reply segment (everything after the last request)."""
    popped = _pop_trailing_reply(context)
    if not popped:
        return False, "No model reply to remove."
    context.save(allow_empty=True)
    note = _TOOL_NOTE if _has_tool_activity(popped) else ""
    return True, f"Removed the last model reply.{note}"


def undo_last_exchanges(context: AgentContext, count: int) -> tuple[bool, str]:
    """Remove up to ``count`` exchanges; each is a user request plus its reply segment."""
    anchors = [index for index, message in enumerate(context.messages) if _is_user_request(message)]
    exchanges = min(count, len(anchors))
    if exchanges <= 0:
        return False, "No exchanges to remove."
    anchor = anchors[-exchanges]
    popped: list[dict[str, Any]] = []
    while len(context.messages) > anchor:
        removed = context.remove_last_message()
        if removed is None:
            break
        popped.append(removed)
    messages = len(popped)
    context.save(allow_empty=True)
    noun = "exchange" if exchanges == 1 else "exchanges"
    count_noun = "exchange" if count == 1 else "exchanges"
    msg_noun = "message" if messages == 1 else "messages"
    note = _TOOL_NOTE if _has_tool_activity(popped) else ""
    if exchanges < count:
        return True, f"Removed {exchanges} of {count} requested {count_noun} ({messages} {msg_noun}).{note}"
    return True, f"Removed {exchanges} {noun} ({messages} {msg_noun}).{note}"


def _user_message_text(message: dict[str, Any]) -> str | None:
    """Return the plain text of a user message, or None when it carries attachments."""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "text":
                return None
            parts.append(str(part.get("text", "")))
        return "\n".join(p for p in parts if p).strip() or None
    return None


def take_last_request(context: AgentContext) -> tuple[bool, str, str]:
    """Remove the last exchange and return its request text for a re-send.

    Returns ``(ok, message, text)``. The exchange is anchored on the last
    real user request; system-generated messages (notifications, delegate
    envelopes, tool images) belong to the surrounding turn and are removed
    with it, never re-sent. Nothing is removed when no real request exists
    or the request carries attachments.
    """
    anchor = None
    for index in range(len(context.messages) - 1, -1, -1):
        if _is_user_request(context.messages[index]):
            anchor = index
            break
    if anchor is None:
        return False, _RETRY_REFUSAL, ""
    message = context.messages[anchor]
    text = _user_message_text(message)
    if text is None:
        return False, _ATTACHMENT_REFUSAL, ""
    while len(context.messages) > anchor:
        if context.remove_last_message() is None:
            break
    context.save(allow_empty=True)
    return True, "", text
