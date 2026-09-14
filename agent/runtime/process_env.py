"""Consistent provenance markers for processes spawned by Astra."""

from __future__ import annotations

import os
import subprocess
from collections.abc import MutableMapping


def mark_agent_environment(
    env: MutableMapping[str, str] | None = None,
) -> MutableMapping[str, str]:
    """Set Astra attribution without overwriting an outer agent harness."""
    target = os.environ if env is None else env
    target.setdefault("AI_AGENT", "astra")
    target.setdefault("ASTRA_AGENT", "true")
    return target


def hidden_process_creationflags(*, new_process_group: bool = False) -> int:
    """Return Windows flags for a console-free child process.

    ``CREATE_NO_WINDOW`` must not be combined with ``DETACHED_PROCESS``;
    Windows explicitly ignores the former in that combination.  Callers that
    need an independently addressable control group can request one without
    invalidating the no-window guarantee.
    """
    if os.name != "nt":
        return 0
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if new_process_group:
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return flags
