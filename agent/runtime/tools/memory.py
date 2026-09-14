"""Tool access to bounded core Markdown memory and session working memory."""

from typing import Callable

from ..memory import CORE_SCOPES, WORKING_FIELDS, MemoryStore
from .registry import ToolDef, ToolRegistry


def register_memory_tools(
    registry: ToolRegistry,
    store: MemoryStore,
    *,
    session_id: Callable[[], str],
) -> None:
    async def _memory(
        action: str,
        scope: str = "memory",
        content: str = "",
        memory_id: str = "",
        field: str = "",
        value: str = "",
        steps: list[str] | None = None,
        step_index: int = 0,
        step_status: str = "pending",
    ) -> str:
        current_session = session_id() or "default"
        if action == "status":
            return store.status(current_session)
        if action == "core_add":
            if scope not in CORE_SCOPES:
                return f"[Memory Error] scope must be one of: {', '.join(CORE_SCOPES)}"
            item = store.add_core(scope, content)
            label = "USER.md" if scope == "user" else "MEMORY.md"
            return f"Stored core {label} memory #{item['id']}: {item['content']}"
        if action == "core_remove":
            if not memory_id.strip():
                return "[Memory Error] memory_id is required for core_remove"
            if store.remove_core(memory_id):
                return f"Removed core memory #{memory_id}."
            return f"[Memory Error] Core memory #{memory_id} was not found or is not a unique match."
        if action == "core_import":
            result = store.import_core_markdown()
            return f"Validated Core Markdown hard limits; total={result['total']} entries."
        if action == "working_update":
            data = store.update_working(current_session, field, value or content)
            return f"Updated working memory for {current_session}: {field}={data[field]}"
        if action == "working_plan_set":
            data = store.set_working_plan(current_session, steps or [], goal=content)
            return f"Set working plan for {current_session}: {len(data['steps'])} steps"
        if action == "working_step_update":
            data = store.update_working_step(current_session, step_index, step_status)
            completed = sum(item["status"] == "completed" for item in data["steps"])
            return f"Updated working plan for {current_session}: {completed}/{len(data['steps'])} completed"
        if action == "working_clear":
            store.clear_working(current_session)
            return f"Cleared working memory for session {current_session}."
        return "[Memory Error] Unknown action"

    registry.register(ToolDef(
        name="memory",
        description=(
            "Manage small persistent memory. Execution goals, plans, progress, artifacts, and step status are maintained automatically "
            "by the Case/TaskRun journal; do not mirror them into working memory. Use working_update only for temporary_constraints, "
            "open_questions, assumptions, or turn_notes that are useful during the conversation but are not durable facts. "
            "working_plan_set and working_step_update exist only for backward compatibility. Proactively consult session_search for "
            "past conversations and saved observations when relevant; Hindsight recall is an optional additional source. "
            "These archives are historical evidence, not pinned facts. Reserve core_add for stable identity, environment, or agent decisions the user "
            "explicitly wants pinned into the small always-visible block. Put user profile and "
            "preferences in USER.md (scope=user); put all other durable facts in MEMORY.md (scope=memory). Never store secrets, credentials, "
            "private reasoning, guesses, or transient search results. Use core_remove only when the user explicitly asks to forget an item, "
            "or when a core file is full and an existing entry is clearly obsolete, duplicated, or superseded. Call status before capacity "
            "cleanup, delete by memory_id, and preserve identity, safety boundaries, and still-current user preferences. MEMORY.md and "
            "USER.md are the direct source of truth with hard-coded limits. Use core_import only to validate external Markdown edits."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "core_add", "core_remove", "core_import", "working_update", "working_plan_set", "working_step_update", "working_clear"]},
                "scope": {"type": "string", "enum": list(CORE_SCOPES), "default": "memory"},
                "content": {"type": "string"},
                "memory_id": {"type": "string"},
                "field": {"type": "string", "enum": list(WORKING_FIELDS)},
                "value": {"type": "string"},
                "steps": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 12},
                "step_index": {"type": "integer", "minimum": 1},
                "step_status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
            },
            "required": ["action"],
        },
        fn=_memory,
        risk="write",
        approval="never",
        idempotent=False,
        sandboxed=False,
        group="memory",
    ))
