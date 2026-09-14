"""Content-free decision metadata shared by inspection and local audit events."""

from __future__ import annotations

import math

from .models import ContextIndexTrace, RetrievalStage
from .ranking import INDEX_TOKEN_BUDGET

CHANNELS = frozenset({
    "session-lexical", "session-vector", "session-recent",
    "memory-lexical", "memory-vector", "memory-recent",
    "activity-lexical", "activity-vector", "activity-summary", "activity-event",
    "activity-recent", "habit",
})
ERROR_CATEGORIES = frozenset({
    "absent", "incompatible_schema", "deadline", "database_error", "relevance_omitted",
    "recency_omitted", "habit_omitted", "source_timeout", "source_error", "broker_error",
})
_STATUSES = frozenset({"available", "absent", "error", "timeout", "disabled", "cold"})
_OMISSIONS = frozenset({"empty_preview", "character_budget", "token_budget"})
_SLOTS = frozenset(f"R{i}" for i in range(1, 7))
_STAGES = frozenset({
    "recency", "relevance", "habit", "summary", "event", "summary_recent", "event_recent",
    "vector_wait", "vector_validate", "snapshot", "encode", "search", "validate",
})


def _stage_metadata(values: tuple[RetrievalStage, ...]) -> list[dict[str, object]]:
    return [
        {"stage": value.name,
         "latency_ms": round(min(60_000, max(0.0, value.latency_ms)), 3) if math.isfinite(value.latency_ms) else 0.0,
         "error": value.error_category if value.error_category in ERROR_CATEGORIES else ("source_error" if value.error_category else "")}
        for value in values[:16] if value.name in _STAGES
    ]


def trace_metadata(trace: ContextIndexTrace) -> dict[str, object]:
    """No queries, previews, handles, archive locators, paths or exception text."""
    return {
        "decision": trace.decision if trace.decision in {"selected", "no-match", "not-needed", "inspection", "error"} else "error",
        "intent": trace.intent if trace.intent in {"lookup", "resume", "history", "preference", "temporal", "inspect"} else "lookup",
        "candidate_count": max(0, trace.candidate_count),
        "selected_count": max(0, trace.selected_count),
        "displayed_count": len(trace.displayed[:6]),
        "submitted": trace.submitted,
        "injected_count": len(trace.displayed[:6]) if trace.submitted else 0,
        "latency_ms": round(max(0, trace.latency_ms), 3),
        "limits": {
            "max_items": trace.max_items,
            "characters": trace.char_budget,
            "estimated_tokens": INDEX_TOKEN_BUDGET,
        },
        "rendered_chars": max(0, trace.rendered_chars),
        "rendered_tokens_estimate": max(0, trace.rendered_tokens),
        "opened_tokens_estimate": max(0, trace.opened_tokens),
        "render_omissions": {
            slot: [reason for reason in reasons if reason in _OMISSIONS]
            for slot, reasons in trace.render_omissions.items() if slot in _SLOTS
        },
        "source_status": {
            name: value if value in _STATUSES else "error"
            for name, value in trace.source_status.items() if name in {"session", "activity", "memory"}
        },
        "semantic_status": {
            name: value if value in _STATUSES else "error"
            for name, value in trace.semantic_status.items() if name in {"session", "memory"}
        },
        "semantic_errors": {
            name: value if value in ERROR_CATEGORIES else "source_error"
            for name, value in trace.semantic_errors.items() if name in {"session", "memory"}
        },
        "source_stages": {
            name: _stage_metadata(values) for name, values in trace.source_stages.items()
            if name in {"session", "activity", "memory"}
        },
        "semantic_stages": {
            name: _stage_metadata(values) for name, values in trace.semantic_stages.items()
            if name in {"session", "memory"}
        },
        "source_errors": {
            name: value if value in ERROR_CATEGORIES else "source_error"
            for name, value in trace.source_errors.items() if name in {"session", "activity", "memory", "broker"}
        },
        "source_diagnostics": {
            name: [value for value in values[:2] if value in ERROR_CATEGORIES]
            for name, values in trace.source_diagnostics.items() if name in {"session", "activity", "memory"}
        },
        "channels_by_slot": {
            row.slot: sorted(set(row.channels) & CHANNELS)
            for row in trace.displayed[:6] if row.slot in _SLOTS
        },
    }
