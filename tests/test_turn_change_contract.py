"""Structural contract for the turn-change M1 interface (frozen 2026-09-18).

This module pins the interface surface only: constants, dataclass fields,
signatures, and default limits. Behavior tests live in
``tests/test_turn_diff.py`` (Subagent B) and
``tests/test_turn_change_store.py`` (Subagent A).

Source of truth: docs/superpowers/plans/2026-09-18-turn-change-ledger-m1.md
section 3 (interface contract).
"""

from __future__ import annotations

import inspect

from agent.runtime import turn_change_store as store
from agent.runtime import turn_diff as diff


# --------------------------------------------------------------------------
# turn_diff surface
# --------------------------------------------------------------------------

def test_diff_quality_and_reason_constants():
    assert diff.DIFF_FULL == "full"
    assert diff.DIFF_COARSE == "coarse"
    assert diff.DIFF_NONE == "none"
    assert diff.REASON_NONE == ""
    assert diff.REASON_BINARY == "binary"
    assert diff.REASON_OVERSIZED == "oversized"
    assert diff.REASON_TIMEOUT == "timeout"
    assert diff.REASON_CANCELLED == "cancelled"
    assert diff.DEFAULT_MAX_FILE_BYTES == 2 * 1024 * 1024


def test_diff_stats_fields():
    assert set(diff.DiffStats.__dataclass_fields__) == {
        "added",
        "removed",
        "quality",
        "reason",
        "truncated",
    }


def test_diff_bytes_signature():
    parameters = inspect.signature(diff.diff_bytes).parameters
    assert list(parameters) == ["before", "after", "max_bytes", "deadline", "cancelled"]
    assert parameters["max_bytes"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["max_bytes"].default == diff.DEFAULT_MAX_FILE_BYTES
    assert parameters["deadline"].default is None
    assert parameters["cancelled"].default is None


# --------------------------------------------------------------------------
# turn_change_store surface
# --------------------------------------------------------------------------

def test_store_state_constants():
    assert store.STATE_MODIFIED == "modified"
    assert store.STATE_ADDED == "added"
    assert store.STATE_DELETED == "deleted"
    assert store.STATE_UNCHANGED == "unchanged"
    assert store.STATE_UNKNOWN == "unknown"

    assert store.SIDE_CAPTURED == "captured"
    assert store.SIDE_ABSENT == "absent"
    assert store.SIDE_UNCAPTURED == "uncaptured"

    assert store.COMPARE_FULL == "full"
    assert store.COMPARE_COARSE == "coarse"
    assert store.COMPARE_NONE == "none"

    assert store.REASON_QUOTA == "quota"
    assert store.REASON_TRACKED == "tracked"
    assert store.REASON_ERROR == "error"


def test_turn_change_limits_defaults():
    limits = store.TurnChangeLimits()
    assert limits.max_file_bytes == 2 * 1024 * 1024
    assert limits.max_paths_per_turn == 500
    assert limits.max_turn_bytes == 32 * 1024 * 1024
    assert limits.max_session_bytes == 256 * 1024 * 1024
    assert limits.max_turns_retained == 10
    assert limits.diff_deadline_ms == 100
    assert limits.compute_budget_ms == 500


def test_file_change_and_manifest_fields():
    assert set(store.FileChange.__dataclass_fields__) == {
        "path",
        "display",
        "state",
        "before_state",
        "after_state",
        "added",
        "removed",
        "compare",
        "reason",
        "checkpoint_ids",
    }
    assert set(store.TurnChangesManifest.__dataclass_fields__) == {
        "session_id",
        "request_id",
        "turn_seq",
        "created_at",
        "files",
        "unknown",
        "totals",
    }
    assert set(store.LoadedSides.__dataclass_fields__) == {
        "before",
        "after",
        "before_state",
        "after_state",
    }


def test_store_api_surface():
    for name in (
        "begin_turn",
        "note_paths",
        "note_capture",
        "note_absent",
        "note_tracked",
        "seal",
        "manifest",
        "load_sides",
        "close",
        "cleanup_orphans",
    ):
        assert callable(getattr(store.TurnChangeStore, name, None)), name

    seal_parameters = inspect.signature(store.TurnChangeStore.seal).parameters
    assert "cancelled" in seal_parameters
    assert seal_parameters["cancelled"].default is None

    load_sides_parameters = inspect.signature(store.TurnChangeStore.load_sides).parameters
    assert list(load_sides_parameters) == ["self", "turn_offset", "path"]
    # 契约（plan §3.2）：load_sides 的 turn_offset 无默认值（与 manifest 不同）。
    assert load_sides_parameters["turn_offset"].default is inspect.Parameter.empty

    assert callable(store.current_turn_change_store)
    assert callable(store.turn_store_scope)
