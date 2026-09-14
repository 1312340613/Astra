"""Slash-command contract for the proactive Context Index."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .context_index_preferences import load_context_index_preferences, save_context_index_preferences


USAGE = "Usage: /context-index [on|off|session|all|status|why] or /context-index feedback R1 useful|irrelevant|outdated"


class ContextIndexBrokerLike(Protocol):
    @property
    def mode(self) -> str: ...

    def set_mode(self, mode: str) -> None: ...

    def format_last_trace(self) -> str: ...


class ContextIndexAgent(Protocol):
    @property
    def context_index_broker(self) -> ContextIndexBrokerLike | None: ...


def execute_context_index_command(agent: ContextIndexAgent, args: Sequence[str]) -> tuple[str, str]:
    """Apply a Context Index slash command, returning output and error text."""
    if len(args) == 3 and args[0].lower() == "feedback":
        feedback = getattr(agent.context_index_broker, "record_feedback", None)
        if callable(feedback):
            return str(feedback(args[1], args[2].lower())), ""
        return "", "Context Index feedback is unavailable."
    if len(args) > 1:
        return "", USAGE

    action = args[0].lower() if args else "status"
    if action == "why":
        broker = agent.context_index_broker
        return (broker.format_last_trace() if broker is not None else "No Context Index decision has been recorded."), ""
    if action in {"status"}:
        return _status_message(agent), ""

    target_mode = "all" if action == "on" else action
    if target_mode not in {"off", "session", "all"}:
        return "", USAGE

    try:
        save_context_index_preferences(target_mode)
    except OSError as exc:
        return "", str(exc)
    broker = agent.context_index_broker
    if broker is not None:
        broker.set_mode(target_mode)
    return _status_message(agent), ""


def _status_message(agent: ContextIndexAgent) -> str:
    preferences = load_context_index_preferences()
    broker = agent.context_index_broker
    active_mode = broker.mode.upper() if broker is not None else "unavailable"
    lines = [
        f"Saved mode: {preferences.mode.upper()}",
        f"Active mode: {active_mode}",
        f"Character budget: {preferences.char_budget}",
    ]
    from agent.runtime.context_index.embedder import backend_name, _enabled
    from agent.runtime.context_index.vector_index import default_vectors_db_path
    from agent.runtime.activity_store import default_activity_db_path
    import os
    from pathlib import Path

    activity_path = Path(os.getenv("ASTRA_CONTEXT_INDEX_ACTIVITY_DB") or os.getenv("ASTRA_ACTIVITY_DB") or default_activity_db_path())
    lines.extend([
        f"Embedding backend: {backend_name()} ({'enabled' if _enabled() else 'disabled'})",
        f"Vector index: {'present' if default_vectors_db_path().is_file() else 'absent'}",
        f"Activity archive: {'present' if activity_path.is_file() else 'absent'}",
    ])
    if preferences.invalid_mode:
        lines.append(f"Invalid saved mode ignored: {preferences.invalid_mode}")
    return "\n".join(lines)
