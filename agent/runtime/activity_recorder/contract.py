"""Recorder ↔ sync 桶格式契约（M1）。

形状来源：2026-09-05 对实桶的解剖（7 类事件 + 10min 桶 + metadata 封条），
文本全合成。validate_event 是宽松忠实校验——只查 normalize_event 之下还能
存活所需的最低结构，过度模式化会把契约脆化。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.runtime.activity_sync import is_valid_timestamp

EVENT_KINDS = frozenset({
    "selection.changed",
    "keyboard.text_input",
    "keyboard.shortcut",
    "mouse.click",
    "window.changed",
    "session.started",
    "session.ended",
})

# kind → 必需专属体字段（None = 无专属体）
KIND_BODY: dict[str, str | None] = {
    "selection.changed": "selection",
    "keyboard.text_input": "keyboard",
    "keyboard.shortcut": "keyboard",
    "mouse.click": "mouse",
    "window.changed": "ax",
    "session.started": None,
    "session.ended": None,
}

BUCKET_ID_FORMAT = "%Y-%m-%dT%H-%M-%SZ"  # e.g. 2026-09-05T02-00-00Z


def validate_event(payload: Any) -> list[str]:
    """Return contract violations; [] means the event may be recorded."""
    if not isinstance(payload, dict):
        return ["payload_not_object"]
    errors: list[str] = []

    event_id = payload.get("id")
    if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
        errors.append("id")

    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, str) or not is_valid_timestamp(timestamp):
        errors.append("timestamp")

    kind = payload.get("kind")
    if kind not in EVENT_KINDS:
        errors.append("kind")
    else:
        body_field = KIND_BODY[str(kind)]
        if body_field is not None:
            body = payload.get(body_field)
            if not isinstance(body, dict) or not body:
                errors.append(body_field)

    app = payload.get("app")
    if not isinstance(app, dict):
        errors.append("app")
    else:
        for field in ("bundleIdentifier", "name"):
            value = app.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"app.{field}")

    window = payload.get("window")
    if window is not None and not isinstance(window, dict):
        errors.append("window")

    return errors


def write_metadata(
    bucket_dir: Path,
    *,
    started: datetime,
    events: int,
    suppressed: int,
    ended: datetime | None = None,
) -> Path:
    """Seal a 10-minute bucket (metadata.json appearance = safe to import)."""
    bucket_dir = Path(bucket_dir)
    started_utc = started.astimezone(timezone.utc)
    ended_utc = (ended or started_utc).astimezone(timezone.utc)
    events_path = bucket_dir / "events.jsonl"
    payload = {
        "id": bucket_dir.name,
        "startedAt": started_utc.strftime(BUCKET_ID_FORMAT),
        "endedAt": ended_utc.strftime(BUCKET_ID_FORMAT),
        "eventsPath": str(events_path),
        "eventCount": events,
        "suppressedEventCount": suppressed,
    }
    (bucket_dir / "metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return bucket_dir / "metadata.json"
