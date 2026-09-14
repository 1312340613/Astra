"""Safe, declarative project hook configuration.

Unlike arbitrary shell hooks, these rules only block a tool, inject bounded
arguments/context, annotate results, and write a local audit record.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .bounded_artifacts import append_bounded_text, positive_int_env
from .hooks import HookReject


def install_project_hooks(registry, workdir: str | Path) -> int:
    path = Path(workdir) / ".astra" / "hooks.json"
    if not path.is_file():
        return 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        rules = raw.get("rules", []) if isinstance(raw, dict) else []
    except (OSError, ValueError, json.JSONDecodeError):
        return 0
    valid = [item for item in rules if isinstance(item, dict) and isinstance(item.get("event"), str)]
    if not valid:
        return 0
    audit_path = path.parent / "hook-events.jsonl"

    def matching(event: str, name: str) -> list[dict[str, Any]]:
        return [rule for rule in valid if rule.get("event") == event and rule.get("tool", "*") in {"*", name}]

    def audit(event: str, name: str, detail: str = "") -> None:
        try:
            append_bounded_text(
                audit_path,
                json.dumps(
                    {
                        "time": time.time(),
                        "event": event,
                        "tool": name,
                        "detail": detail[:1000],
                    },
                    ensure_ascii=False,
                ) + "\n",
                max_bytes=positive_int_env(
                    "ASTRA_HOOK_AUDIT_MAX_BYTES", 2 * 1024 * 1024
                ),
                backup_count=positive_int_env("ASTRA_HOOK_AUDIT_BACKUPS", 2),
            )
        except OSError:
            pass

    def before(name: str, args: dict, _tool) -> dict:
        current = dict(args)
        for rule in matching("before_tool", name):
            action = str(rule.get("action", "")).strip()
            if action == "block":
                raise HookReject(str(rule.get("message") or f"Project hook blocked {name}"))
            if action == "inject_args" and isinstance(rule.get("args"), dict):
                current.update({str(key): value for key, value in rule["args"].items()})
            audit("before_tool", name, action)
        return current

    def after(name: str, _args: dict, result: dict, _tool) -> dict:
        current = dict(result)
        for rule in matching("after_tool", name):
            if rule.get("action") == "annotate":
                note = str(rule.get("message") or "").strip()[:1000]
                if note:
                    current["output"] = (str(current.get("output") or "") + "\n[Project hook] " + note).strip()
            audit("after_tool", name, str(rule.get("action", "")))
        return current

    def failed(name: str, _args: dict, error: str, _tool) -> None:
        for rule in matching("tool_error", name):
            audit("tool_error", name, str(rule.get("message") or error))

    def compacted(event: str):
        def observe(payload: dict) -> None:
            for rule in matching(event, "compact"):
                audit(event, "compact", json.dumps(payload, ensure_ascii=False))
        return observe

    registry.hooks.on_before_tool(before)
    registry.hooks.on_after_tool(after)
    registry.hooks.on_tool_error(failed)
    registry.hooks.on_pre_compact(compacted("pre_compact"))
    registry.hooks.on_post_compact(compacted("post_compact"))
    return len(valid)
