"""Shared runtime-to-TUI tool event projection, also exercised by offline replay."""


def tool_result_event(event: dict) -> dict:
    tool_result_event = {
        "type": "tool_result",
        "name": event["name"],
        "call_id": event.get("id") or event.get("call_id", ""),
        "output": event.get("output", ""),
        "error": event.get("error", ""),
        "code": event.get("code", ""),
        "duration_ms": event.get("duration_ms", 0),
        "output_truncated": event.get("output_truncated", False),
        "artifact_path": event.get("artifact_path", ""),
        "error_type": event.get("error_type", ""),
        "recoverable": bool(event.get("recoverable")),
    }
    for key in (
        "retryable",
        "recovery_hint",
        "partial",
        "details",
        "artifact_ref",
        "computer_receipt",
    ):
        if key in event:
            tool_result_event[key] = event[key]
    return tool_result_event


def tool_progress_event(event: dict) -> dict:
    progress_event = {
        "type": "tool_progress",
        "call_id": event.get("id") or event.get("call_id", ""),
        "name": event.get("name", "tool"),
        "stage": event.get("stage", "running"),
        "status": event.get("status", "running"),
        "message": event.get("message", ""),
        "unit": event.get("unit", ""),
    }
    for key in ("current", "total", "percent"):
        if event.get(key) is not None:
            progress_event[key] = event[key]
    return progress_event
