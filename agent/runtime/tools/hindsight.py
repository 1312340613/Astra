"""Agent tool access to the optional Hindsight provider."""

from __future__ import annotations

from ..hindsight_provider import HindsightMemoryProvider
from .registry import ToolDef, ToolRegistry


def register_hindsight_tools(
    registry: ToolRegistry,
    provider: HindsightMemoryProvider,
) -> None:
    async def hindsight_retain(
        content: str,
        context: str = "",
        tags: list[str] | None = None,
    ) -> str:
        try:
            record = await provider.retain(
                kind="observation",
                content=content,
                context=context,
                tags=tags or (),
            )
        except Exception as exc:
            return f"[Hindsight Error] {type(exc).__name__}: {exc}"
        return f"Hindsight stored non-authoritative memory in {record.metadata['document_id']}."

    async def hindsight_recall(query: str, limit: int = 5) -> str:
        try:
            records = await provider.recall(query, limit=limit)
        except Exception as exc:
            return f"[Hindsight Error] {type(exc).__name__}: {exc}"
        if not records:
            return "No relevant Hindsight memories found."
        lines = [
            "Hindsight results (historical, non-authoritative; builtin current facts override conflicts):"
        ]
        lines.extend(
            f"{index}. [{record.kind}] {record.content}"
            for index, record in enumerate(records, 1)
        )
        return "\n".join(lines)

    async def hindsight_reflect(query: str) -> str:
        try:
            result = await provider.reflect(query)
        except Exception as exc:
            return f"[Hindsight Error] {type(exc).__name__}: {exc}"
        return result or "No relevant Hindsight memories found."

    registry.register(ToolDef(
        name="hindsight_retain",
        description=(
            "Store explicitly useful information in the shared local Hindsight bank. "
            "This history is non-authoritative and does not replace current builtin facts."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "minLength": 1},
                "context": {"type": "string", "default": ""},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 32,
                },
            },
            "required": ["content"],
        },
        fn=hindsight_retain,
        risk="write",
        approval="never",
        idempotent=False,
        group="memory",
        max_calls_per_turn=2,
    ))

    registry.register(ToolDef(
        name="hindsight_recall",
        description=(
            "Search the shared local Hindsight bank for older cross-session context. "
            "Results are historical and non-authoritative; current builtin memory overrides conflicts."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12, "default": 5},
            },
            "required": ["query"],
        },
        fn=hindsight_recall,
        risk="read",
        approval="never",
        idempotent=True,
        cache_ttl=30,
        group="memory",
        max_calls_per_turn=2,
    ))

    registry.register(ToolDef(
        name="hindsight_reflect",
        description=(
            "Synthesize a reasoned answer across the shared local Hindsight bank. "
            "Use for cross-session patterns or questions requiring multiple memories."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
            },
            "required": ["query"],
        },
        fn=hindsight_reflect,
        risk="read",
        approval="never",
        idempotent=True,
        cache_ttl=30,
        group="memory",
        max_calls_per_turn=2,
        timeout=provider.reflect_timeout + 10,
    ))
