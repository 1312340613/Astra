from __future__ import annotations

import json
from collections.abc import Callable

from ..memory import MemoryStore
from .registry import ToolDef, ToolRegistry


def register_plan_tools(
    registry: ToolRegistry,
    store: MemoryStore,
    *,
    session_id: Callable[[], str],
) -> None:
    def _plan_update(goal: str, steps: list[dict[str, str]]) -> str:
        data = store.replace_working_plan(session_id() or "default", goal, steps)
        counts = {
            "pending": sum(item["status"] == "pending" for item in data["steps"]),
            "in_progress": sum(item["status"] == "in_progress" for item in data["steps"]),
            "completed": sum(item["status"] == "completed" for item in data["steps"]),
        }
        return json.dumps({"goal": data["goal"], "steps": data["steps"], "counts": counts}, ensure_ascii=False)

    registry.register(ToolDef(
        name="plan_update",
        description=(
            "Replace the complete lightweight implementation plan for this session. "
            "Use 2-12 unique concrete steps. Keep exactly one step in_progress while work remains, "
            "and resend the whole list whenever progress changes. Skip this for trivial one-step work."
        ),
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "goal": {"type": "string"},
                "steps": {
                    "type": "array", "minItems": 2, "maxItems": 12,
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "text": {"type": "string"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                        },
                        "required": ["text", "status"],
                    },
                },
            },
            "required": ["goal", "steps"],
        },
        fn=_plan_update,
        risk="write",
        approval="never",
        group="core",
    ))
