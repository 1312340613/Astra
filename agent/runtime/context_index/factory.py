"""Lazy construction for the process-scoped Context Index broker."""

from __future__ import annotations

import os
from os import PathLike
from pathlib import Path
from typing import Protocol

import session_recall
from agent.runtime import activity_store

from .activity_source import ActivityRecommendationReader
from .broker import ContextIndexBroker
from .session_source import SessionRecommendationSource
from .semantic_reader import SemanticReader


class _Preferences(Protocol):
    @property
    def mode(self) -> str: ...

    @property
    def char_budget(self) -> int: ...


def create_context_index_broker(
    preferences: _Preferences,
    workdir: str | PathLike[str],
) -> ContextIndexBroker:
    """Create readers without opening, creating, or migrating their databases.

    The readers follow the paths used by the canonical Session Recall and
    Activity Store writers.  ``workdir`` remains part of the public factory
    contract for callers, but must not quietly create a second cwd-derived
    archive when Astra is launched from another directory.
    """
    del workdir
    configured_session = os.getenv("ASTRA_CONTEXT_INDEX_SESSIONS_DB", "").strip()
    writer_session = os.getenv("ASTRA_SESSION_RECALL_DB", "").strip()
    configured_activity = os.getenv("ASTRA_CONTEXT_INDEX_ACTIVITY_DB", "").strip()
    writer_activity = os.getenv("ASTRA_ACTIVITY_DB", "").strip()
    session_path = (
        Path(configured_session).expanduser()
        if configured_session
        else (
            Path(writer_session).expanduser()
            if writer_session
            else session_recall.DB_PATH
        )
    )
    activity_path = (
        Path(configured_activity).expanduser()
        if configured_activity
        else (
            Path(writer_activity).expanduser()
            if writer_activity
            else activity_store.default_activity_db_path()
        )
    )
    return ContextIndexBroker(
        mode=preferences.mode,
        char_budget=preferences.char_budget,
        session_source=SessionRecommendationSource(
            session_path, deadline_ms=_budget("ASTRA_CONTEXT_INDEX_SESSION_MS", 75),
        ),
        activity_source=ActivityRecommendationReader(activity_path),
        semantic_reader=SemanticReader(session_path),
        source_deadline_seconds=_budget("ASTRA_CONTEXT_INDEX_SOURCE_MS", 200) / 1000,
        feedback_path=Path(os.getenv("ASTRA_CONTEXT_INDEX_FEEDBACK_DB") or session_path.with_name("context-index-feedback.db")),
    )


def _budget(name: str, default: int) -> int:
    try:
        return max(25, min(2000, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


__all__ = ["create_context_index_broker"]
