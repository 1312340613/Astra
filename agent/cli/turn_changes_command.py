"""Shared ``/changes`` behavior for the backend and the terminal client (M3 · T4).

Read-only by contract: the command never writes, never seals a turn, never
becomes a model turn, and never creates the ledger store.  The store accessor
``agent.turn_change_store()`` returns the live session store or ``None``; when
it is ``None`` the command still answers with the honest "index unavailable"
wording instead of pretending there is nothing to show.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from agent.runtime.turn_changes_view import (
    INDEX_UNAVAILABLE,
    NO_TURNS,
    USAGE,
    format_file_diff,
    format_turn_list,
    parse_changes_args,
    resolve_selector,
)

if TYPE_CHECKING:
    from agent.runtime.turn_change_store import TurnChangeStore

logger = logging.getLogger(__name__)


def execute_changes_command(agent, argument: str) -> tuple[str, str]:
    """Render the ledger for ``/changes [@k] [n|path]``; returns ``(output, error)``."""
    k, selector, error = parse_changes_args(argument)
    if error:
        return "", error
    store = _store_for(agent)
    if store is None:
        return INDEX_UNAVAILABLE, ""
    try:
        index = store.completed_turns()
    except Exception:
        logger.exception("turn-change index read failed")
        return INDEX_UNAVAILABLE, ""
    if not index.ok:
        return INDEX_UNAVAILABLE, ""
    if not index.records:
        return NO_TURNS, ""
    if k > len(index.records):
        return "", f"{USAGE} · 只有 {len(index.records)} 个完成回合可查看。"

    record = index.records[k - 1]
    manifest = None
    if not record.empty and record.available:
        try:
            manifest = store.manifest_for(record.turn_seq)
        except Exception:
            logger.exception("turn-change manifest read failed")
            manifest = None
    if selector is None:
        return format_turn_list(k, record, manifest), ""
    if manifest is None:
        return format_turn_list(k, record, manifest), ""

    entries = [*manifest.files, *manifest.unknown]
    match = resolve_selector(entries, selector)
    if match.entry_index is None:
        return "", match.error
    try:
        sides = store.load_sides_for(record.turn_seq, match.entry_index)
    except Exception:
        logger.exception("turn-change snapshot read failed")
        return INDEX_UNAVAILABLE, ""
    return format_file_diff(k, record, manifest, entries[match.entry_index], sides), ""


def _store_for(agent: Any) -> TurnChangeStore | None:
    """Read-only accessor; never creates or closes the store."""
    accessor = getattr(agent, "turn_change_store", None)
    if not callable(accessor):
        return None
    try:
        return cast("TurnChangeStore | None", accessor())
    except Exception:
        logger.exception("turn-change store accessor failed")
        return None
