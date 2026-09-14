"""Extract replay-able turns from the session DB into shadow-replay case records.

Output records satisfy ``agent.evals.context_index_eval.load_shadow_cases`` verbatim,
plus a ``session_id`` field the benchmark uses for per-session caps and report joins.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

_MIN_CHARS = 12
_PER_SESSION_CAP = 2
_MESSAGE_TIME_RE = re.compile(r"^\s*<message_time>")
_CASE_ID_RE = re.compile(r"[^a-z0-9_-]+")


def _case_id(session_id: str, msg_index: int) -> str:
    raw = f"case-{session_id}-{msg_index}".lower()
    cleaned = _CASE_ID_RE.sub("-", raw).strip("-_")[:79]
    if not cleaned or not cleaned[0].isalnum():
        cleaned = f"c-{cleaned}"
    return cleaned[:80]


def _workspace_label(root: str) -> str:
    name = Path(root).name or "workspace"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return cleaned[:80] or "workspace"


def extract_cases(db_path: Path, *, max_cases: int = 100) -> list[dict[str, Any]]:
    """Select user turns from workspace-bound sessions, deterministically capped."""

    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT s.id AS session_id, s.workspace_root AS root,
                   m.rowid AS message_id, m.content AS content, m.msg_index AS msg_index, m.timestamp AS ts
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE m.role = 'user'
              AND s.workspace_root IS NOT NULL
              AND length(m.content) >= ?
            ORDER BY s.id, m.msg_index
            """,
            (_MIN_CHARS,),
        ).fetchall()
    finally:
        connection.close()

    per_session: dict[str, int] = {}
    records: list[dict[str, Any]] = []
    for row in rows:
        content = (row["content"] or "").strip()
        if not content or _MESSAGE_TIME_RE.match(content):
            continue
        # Pure metadata echoes (时间戳前缀行剥掉后为空) 已被上一条覆盖；
        # 再跳过明显的命令回显型单 token。
        if len(content.split()) < 2 and not re.search(r"[\u4e00-\u9fff]{2,}", content):
            continue
        taken = per_session.get(row["session_id"], 0)
        if taken >= _PER_SESSION_CAP:
            continue
        per_session[row["session_id"]] = taken + 1
        local_time = datetime.fromtimestamp(float(row["ts"])).astimezone().isoformat()
        records.append(
            {
                "case_id": _case_id(str(row["session_id"]), int(row["msg_index"])),
                "session_id": str(row["session_id"]),
                "message_id": int(row["message_id"]),
                "user_text": content[:2000],
                "workspace_label": _workspace_label(str(row["root"])),
                "workspace_key": str(row["root"]).casefold(),
                "local_time": local_time,
            }
        )
        if len(records) >= max_cases:
            break
    return records
