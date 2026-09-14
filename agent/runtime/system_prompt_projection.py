"""Durable, optional request projection for models supporting system updates."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any
from urllib.parse import urlparse

from .deepseek import DEEPSEEK_FLASH


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def system_prompt_series(config: Any, tools: list[dict]) -> str:
    """Custom routes opt in; official V4.1 supports updates in history."""
    if os.getenv("ASTRA_SYSTEM_PROMPT_HISTORY", "1").lower() in {"0", "false", "off"}:
        return ""
    endpoint = str(getattr(config, "base_url", "")).rstrip("/")
    model = str(getattr(config, "model", ""))
    capabilities = getattr(config, "capabilities", ())
    explicit = isinstance(capabilities, (set, frozenset, list, tuple)) and "system-prompt-in-history" in capabilities
    official = urlparse(endpoint).hostname == "api.deepseek.com" and model == DEEPSEEK_FLASH
    if not (explicit or official):
        return ""
    return _digest([endpoint, model, getattr(config, "provider", ""),
                    getattr(config, "api_key", ""), tools])


class SystemPromptProjection:
    """Keep canonical history separate; anchor updates to the actual wire prefix.

    Only prompt text and digests are persisted, never transient images, tool
    arguments or credentials. Rewritten history cannot accidentally inherit a
    superseded prompt: a mismatched anchor rebases to current authority. A proven
    single addition can retain its unchanged head while moving its anchor.
    """

    def __init__(self, state: object = None):
        self.state: dict = state if isinstance(state, dict) else {}

    def reset(self) -> None:
        self.state = {}

    def project(self, messages: list[dict], series: str, *, addition: str = "") -> list[dict]:
        if not series or not messages or messages[0].get("role") != "system":
            self.reset()
            return messages
        current = messages[0].get("content")
        history = messages[1:]
        # Do not reinterpret externally supplied multi-system prompts.
        if not isinstance(current, str) or not current or any(m.get("role") == "system" for m in history):
            self.reset()
            return messages
        state = self.state
        updates = state.get("updates", [])
        valid = (state.get("version") == 1 and state.get("series") == series
                 and isinstance(state.get("head"), str) and isinstance(updates, list)
                 and len(updates) <= 16)
        previous = -1
        previous_content = state.get("head")
        if valid:
            for item in updates:
                if not isinstance(item, dict):
                    valid = False
                    break
                anchor = item.get("after")
                if (isinstance(anchor, bool) or not isinstance(anchor, int)
                        or not previous <= anchor <= len(history)
                        or not isinstance(item.get("content"), str)
                        or item.get("prefix") != _digest(history[:anchor])):
                    valid = False
                    break
                # An optional delta is safe only when it exactly accounts for
                # the whole prompt change. Revalidate on restore; edits, rule
                # removal and persona changes always require full authority.
                if "addition" in item:
                    delta = item["addition"]
                    if (not isinstance(delta, str) or not delta
                            or item["content"].count(delta) != 1
                            or item["content"].replace(delta, "", 1) != previous_content):
                        valid = False
                        break
                previous_content = item["content"]
                previous = anchor
        if not valid:
            # Request-local memory/skill observations can rewrite the previous
            # user message. Do not throw away a still-identical chat prefix:
            # re-anchor its one lossless work addition against current history.
            # Other edits and unproven/superseded deltas retain the full rebase.
            can_reanchor = (
                state.get("version") == 1 and state.get("series") == series
                and isinstance(updates, list) and len(updates) == 1
                and isinstance(updates[0], dict) and bool(addition)
                and updates[0].get("addition") == addition
                and updates[0].get("content") == current
                and current.count(addition) == 1
                and current.replace(addition, "", 1) == state.get("head")
            )
            state = {"version": 1, "series": series,
                     "head": state["head"] if can_reanchor else current, "updates": []}
        updates = list(state["updates"])
        latest = updates[-1]["content"] if updates else state["head"]
        if current != latest:
            # Rebase before unbounded prompt growth or a tool protocol split.
            if len(updates) >= 16 or (history and history[-1].get("tool_calls")):
                state = {"version": 1, "series": series, "head": current, "updates": []}
                updates = []
            else:
                update = {"after": len(history), "prefix": _digest(history), "content": current}
                if addition and current.count(addition) == 1 and current.replace(addition, "", 1) == latest:
                    update["addition"] = addition
                updates.append(update)
        self.state = {**state, "updates": updates}
        result = [{"role": "system", "content": state["head"]}]
        start = 0
        for item in updates:
            result.extend(history[start:item["after"]])
            result.append({"role": "system", "content": item.get("addition", item["content"])})
            start = item["after"]
        result.extend(history[start:])
        return result
