"""Session Handoff — structured continuity document generated at session end.

Produces a markdown handoff that another agent instance (or the same agent in a
new session) can consume to resume work without re-discovering state.

Design constraints:
- Deterministic: no LLM calls. Pure extraction from TaskStore + message history.
- Idempotent: generating twice for the same session overwrites, never duplicates.
- Safe: never includes secrets, tokens, or full message bodies (summaries only).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .browser_session import sanitize_snapshot

if TYPE_CHECKING:
    from .task_store import TaskStore


_HANDOFF_SECRET_PATTERNS = (
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b(?:api[_ -]?key|access[_ -]?token|secret)\s*[:=]\s*\S+", re.IGNORECASE),
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_handoff(
    *,
    session_id: str,
    task_store: "TaskStore | None",
    messages: list[dict[str, Any]] | None = None,
    model: str = "",
    persona_id: str = "",
    extra_notes: str = "",
) -> str:
    """Build a structured handoff markdown document.

    Parameters
    ----------
    session_id : str
        Current session identifier.
    task_store : TaskStore | None
        If provided, extracts blocked/scheduled runs and recent completions.
    messages : list[dict] | None
        Session message history (storage format). Only role + truncated text used.
    model : str
        Model identifier for this session.
    persona_id : str
        Active persona.
    extra_notes : str
        Free-form notes appended after credential redaction.

    Returns
    -------
    str
        Markdown handoff document.
    """
    now = datetime.now(timezone.utc)
    sections: list[str] = []

    # --- Header ---
    sections.append("# Session Handoff\n")
    sections.append(f"- **Generated**: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    sections.append(f"- **Session**: `{_truncate(session_id, 120)}`")
    if model:
        sections.append(f"- **Model**: {_truncate(model, 160)}")
    if persona_id:
        sections.append(f"- **Persona**: {_truncate(persona_id, 160)}")
    sections.append("")

    # --- Task State (from TaskStore) ---
    if task_store is not None:
        task_section = _build_task_section(task_store)
        if task_section:
            sections.append(task_section)

    # --- Conversation Summary (from messages) ---
    if messages:
        convo_section = _build_conversation_section(messages)
        if convo_section:
            sections.append(convo_section)

    # --- Extra Notes ---
    if extra_notes.strip():
        sections.append(f"## Notes\n\n{_redact(extra_notes.strip())}\n")

    # --- Resume Instructions ---
    sections.append(_build_resume_instructions(task_store))

    return "\n".join(sections)


def save_handoff(
    handoff_text: str,
    *,
    session_id: str,
    base_dir: str | Path = ".astra/handoffs",
) -> Path:
    """Persist handoff to disk. Returns the written path."""
    out_dir = Path(base_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_session_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(session_id)).strip("._") or "default"
    filename = f"{safe_session_id[:120]}_{ts}.md"
    path = out_dir / filename
    path.write_text(handoff_text, encoding="utf-8")
    # Also write a "latest" symlink/copy for easy discovery
    latest = out_dir / "latest.md"
    latest.write_text(handoff_text, encoding="utf-8")
    return path


def load_latest_handoff(base_dir: str | Path = ".astra/handoffs") -> str | None:
    """Load the most recent handoff document, if any."""
    latest = Path(base_dir) / "latest.md"
    if latest.exists():
        return latest.read_text(encoding="utf-8")
    return None


# ---------------------------------------------------------------------------
# Internal builders
# ---------------------------------------------------------------------------


def _build_task_section(store: "TaskStore") -> str:
    """Extract durable TaskRun state into markdown."""
    parts: list[str] = ["## Task State\n"]

    # Blocked runs
    user_blocked = store.blocked_runs(kind="blocked_on_user", limit=10)
    ext_blocked = store.blocked_runs(kind="waiting_external", limit=10)
    if user_blocked or ext_blocked:
        parts.append("### Blocked Runs\n")
        for task in user_blocked:
            text = _truncate(task["input_text"], 80)
            reason = task.get("block_reason") or ""
            parts.append(f"- ⏳ `{task['id']}` {text}")
            if reason:
                parts.append(f"  - Reason: {_truncate(reason, 240)}")
        for task in ext_blocked:
            text = _truncate(task["input_text"], 80)
            reason = task.get("block_reason") or ""
            parts.append(f"- 📬 `{task['id']}` {text}")
            if reason:
                parts.append(f"  - Reason: {_truncate(reason, 240)}")
        parts.append("")

    # Scheduled
    due = store.due_scheduled_runs(limit=10)
    if due:
        parts.append("### Scheduled (due)\n")
        for task in due:
            text = _truncate(task["input_text"], 80)
            parts.append(f"- 📅 `{task['id']}` {task.get('scheduled_at', '?')} — {text}")
        parts.append("")

    # Recent completions
    recent = store.list_tasks(limit=10)
    done = [t for t in recent if t["status"] in ("completed", "done_verified")]
    if done:
        parts.append("### Recently Completed\n")
        for task in done[:5]:
            text = _truncate(task["input_text"], 80)
            verified = " ✓" if task["status"] == "done_verified" else ""
            parts.append(f"- ✅ `{task['id']}` {text}{verified}")
        parts.append("")

    if len(parts) == 1:
        return ""  # Only header, nothing to report
    return "\n".join(parts)


def _build_conversation_section(messages: list[dict[str, Any]]) -> str:
    """Extract a lightweight conversation summary (role + truncated text)."""
    # Only look at user and assistant messages, skip system/tool
    relevant = [
        m for m in messages
        if m.get("role") in ("user", "assistant")
    ]
    if not relevant:
        return ""

    parts: list[str] = ["## Conversation Summary\n"]
    parts.append(f"_{len(relevant)} messages this session (showing last 20)_\n")

    # Show last 20 messages, truncated
    for msg in relevant[-20:]:
        role = msg.get("role", "?")
        # Extract text from storage format
        text = _extract_msg_text(msg)
        if not text:
            continue
        icon = "👤" if role == "user" else "🤖"
        parts.append(f"- {icon} **{role}**: {_truncate(text, 120)}")

    parts.append("")
    return "\n".join(parts)


def _build_resume_instructions(store: "TaskStore | None") -> str:
    """Standard resume instructions for the next session."""
    parts: list[str] = ["## Resume Instructions\n"]
    parts.append("1. Read this handoff to understand current state.")
    parts.append("2. Check `format_today()` / `/today` for live task dashboard.")
    parts.append("3. Resume interrupted work from its TaskRun checkpoint.")
    parts.append("4. Do NOT re-do completed items.")
    parts.append("5. If blocked items exist, check if the blocker is resolved before proceeding.")
    if store is not None:
        parts.append("6. TaskStore is the single source of truth for progress — trust it over this document if they conflict.")
    parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate(text: str, max_len: int) -> str:
    """Truncate to max_len, collapsing whitespace."""
    clean = " ".join(_redact(str(text)).split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "…"


def _redact(text: str) -> str:
    """Remove credential-shaped content before it reaches a handoff file."""
    cleaned = sanitize_snapshot(str(text))
    for pattern in _HANDOFF_SECRET_PATTERNS:
        cleaned = pattern.sub("[REDACTED]", cleaned)
    return cleaned


def _extract_msg_text(msg: dict[str, Any]) -> str:
    """Extract plain text from a stored message (handles both str and list content)."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif "text" in block:
                    parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts).strip()
    return ""
