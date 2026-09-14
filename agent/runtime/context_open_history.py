"""Keep legacy context_open redaction markers out of model call examples."""

from __future__ import annotations

import json
import re


CONTEXT_OPEN_RECOVERY_HINT = (
    "Call context_inspect to obtain the current handles, then copy the exact "
    "10-character handles into context_open.handles. Redaction placeholders "
    "are not handles. Do not guess their original values or reuse old-turn handles."
)
_REDACTED_HANDLE = re.compile(r"\[redacted \d+ chars\]")


def has_context_open_placeholder(arguments: object) -> bool:
    if not isinstance(arguments, dict):
        return False
    if "request_local_placeholder" in arguments:
        return True
    handles = arguments.get("handles")
    if isinstance(handles, str):
        handles = [handles]
    return isinstance(handles, list) and any(
        isinstance(handle, str) and _REDACTED_HANDLE.fullmatch(handle.strip())
        for handle in handles
    )


def without_context_open_placeholders(messages: list[dict]) -> list[dict]:
    """Project old call/result pairs out of a prompt without editing the archive.

    The old argument redactor stored markers in otherwise callable JSON. Keep
    the surrounding conversation and sibling tool calls, but never present the
    markers as context_open arguments the model can imitate.
    """
    removed_ids: set[str] = set()
    projected: list[dict] = []
    for message in messages:
        calls = message.get("tool_calls")
        if message.get("role") != "assistant" or not isinstance(calls, list):
            projected.append(message)
            continue
        kept = []
        for call in calls:
            function = call.get("function", {})
            if function.get("name") == "context_open":
                try:
                    arguments = json.loads(function.get("arguments", ""))
                except (TypeError, ValueError):
                    arguments = None
                if has_context_open_placeholder(arguments):
                    removed_ids.add(str(call.get("id") or ""))
                    continue
            kept.append(call)
        if len(kept) == len(calls):
            projected.append(message)
            continue
        replacement = dict(message)
        if kept:
            replacement["tool_calls"] = kept
        else:
            replacement.pop("tool_calls", None)
        note = "Earlier context_open arguments are unavailable in saved history. " + CONTEXT_OPEN_RECOVERY_HINT
        content = message.get("content")
        if isinstance(content, list):
            replacement["content"] = [*content, {"type": "text", "text": note}]
        else:
            replacement["content"] = f"{content}\n\n{note}" if content else note
        projected.append(replacement)
    return [
        message for message in projected
        if not (
            message.get("role") == "tool"
            and str(message.get("tool_call_id") or "") in removed_ids
        )
    ]
