"""Stable, request-local tools for inspecting and opening Context Index evidence."""

from __future__ import annotations

import json

from ..context_index.broker import ContextIndexBroker, OPEN_MAX_CALLS, OPEN_MAX_HANDLES
from .registry import ToolDef, ToolRegistry


def register_context_index_tools(
    registry: ToolRegistry,
    broker: ContextIndexBroker,
) -> None:
    """Register mode-stable inspection and bounded request-local evidence tools."""

    def context_inspect() -> str:
        return broker.inspect()

    registry.register(ToolDef(
        name="context_inspect",
        description=(
            "Inspect the Context Index recommendations already prepared for this turn. "
            "Use to answer whether anything was injected, what it contains, or questions "
            "about counts, limits, omissions, channels, latency and errors; returns exact "
            "current handles. Reads existing state only: does not rerun retrieval, encode "
            "a query or change recommendations. Previous-turn metadata is labeled "
            "separately; core MD memory is not included."
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        fn=context_inspect, risk="read", approval="never", sandboxed=True,
        max_calls_per_turn=2, cache_results=False, result_persistence="request_local",
        memory_evidence=None, strict_schema=True,
    ))

    def context_open(handles: object = None, window: object = 2) -> str:
        # Registry schema validation is authoritative for model calls.  Keep
        # this boundary defensive as the callable is also used directly by
        # local Python integrations and focused tests.
        if not isinstance(handles, list) or not 1 <= len(handles) <= OPEN_MAX_HANDLES:
            return "invalid_request"
        if any(not isinstance(handle, str) for handle in handles):
            return "invalid_request"
        if len(set(handles)) != len(handles):
            return "invalid_request"
        if not isinstance(window, int) or isinstance(window, bool):
            return "invalid_request"
        bounded_window = max(0, min(window, 5))
        result = broker.open(list(handles), bounded_window)
        if result == "invalid_or_expired_handle":
            return json.dumps({
                "status": result,
                "hint": "Copy exact current handles from context_inspect; do not substitute session IDs or extend handles. "
                        "This error does not establish expiry. Old-turn handles cannot be reopened.",
            })
        return result

    registry.register(
        ToolDef(
            name="context_open",
            description=(
                "Open only useful evidence from handles in the current model-only "
                "Context Index. Copy handles exactly; do not invent or extend them. "
                "Use context_inspect for current handles and diagnostics. Three handles is "
                "the per-call opening limit, not the recommendation limit."
            ),
            parameters={
                "type": "object",
                "required": ["handles"],
                "properties": {
                    "handles": {
                        "type": "array",
                        "items": {"type": "string", "pattern": "^ctx:[sahm]:[0-9a-f]{4}$", "minLength": 10, "maxLength": 10},
                        "minItems": 1,
                        "maxItems": OPEN_MAX_HANDLES,
                    },
                    "window": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 5,
                        "default": 2,
                    },
                },
                "additionalProperties": False,
            },
            fn=context_open,
            risk="read",
            approval="never",
            sandboxed=True,
            max_calls_per_turn=OPEN_MAX_CALLS,
            cache_results=False,
            result_persistence="request_local",
            # Opaque handles must stay literal in replayed calls. Redacting
            # them creates invalid examples that models copy into new calls.
            argument_persistence="durable",
            memory_evidence=None,
            strict_schema=True,
        )
    )


__all__ = ["register_context_index_tools"]
