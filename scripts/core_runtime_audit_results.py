"""Allowlisted normalization for untrusted provider-audit worker IPC."""

from __future__ import annotations

from typing import Any

STATUSES = {"passed", "external_failure", "product_failure"}
EVENT_TYPES = {
    "chunk",
    "done",
    "error",
    "reasoning",
    "tool_calls",
    "tool_result",
}
FINISH_REASONS = {
    "stop",
    "length",
    "tool_calls",
    "content_filter",
    "max_tokens",
    "max_output_tokens",
}
ERROR_TYPES = {
    "APIConnectionError",
    "APIError",
    "APITimeoutError",
    "AssertionError",
    "ConnectError",
    "ConnectTimeout",
    "ConnectionError",
    "HTTPError",
    "HTTPStatusError",
    "InvalidProbeResult",
    "LLMIdleTimeout",
    "LookupError",
    "NetworkError",
    "PoolTimeout",
    "ProbeIPCFailure",
    "ProbeStartFailure",
    "ProbeTimeout",
    "ProbeWorkerExit",
    "ProbeWorkerFailure",
    "ProxyError",
    "RateLimitError",
    "ReadError",
    "ReadTimeout",
    "RemoteProtocolError",
    "RuntimeError",
    "SSLError",
    "TimeoutError",
    "TimeoutException",
    "TransportError",
    "ValueError",
    "WriteError",
    "WriteTimeout",
}
PRODUCT_RUNTIME_ERRORS = {
    "InvalidProbeResult",
    "ProbeIPCFailure",
    "ProbeStartFailure",
    "ProbeWorkerExit",
    "ProbeWorkerFailure",
}
_MAX_SAFE_INT = 2_147_483_647
_MAX_EVENT_TYPES = len(EVENT_TYPES)
_MAX_TOOL_NAMES = 4
_CANCEL_FINISH_MS = 5_000


def safe_error_type(value: object) -> str:
    return value if isinstance(value, str) and value in ERROR_TYPES else "OtherError"


def _safe_int(value: object) -> int:
    try:
        return min(_MAX_SAFE_INT, max(0, int(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_bool(value: object) -> bool:
    return value is True


def _required_int(
    result: dict,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> bool:
    value = result.get(field)
    return type(value) is int and minimum <= value <= maximum


def _usage(value: object) -> dict[str, int]:
    usage = value if isinstance(value, dict) else {}
    return {
        name: _safe_int(usage.get(name))
        for name in ("prompt_tokens", "completion_tokens", "total_tokens")
    }


def normalize_worker_result(probe_id: str, result: object) -> dict[str, Any]:
    """Drop every IPC field/value not in the fixed report schema."""
    raw_result = result if isinstance(result, dict) else {}
    numeric_evidence_valid = {
        "LIVE-01": _required_int(
            raw_result,
            "final_text_length",
            minimum=1,
            maximum=_MAX_SAFE_INT,
        ),
        "LIVE-02": _required_int(
            raw_result,
            "read_call_count",
            minimum=1,
            maximum=1,
        ),
        "LIVE-03": _required_int(
            raw_result,
            "cancel_wait_ms",
            minimum=0,
            maximum=_CANCEL_FINISH_MS,
        ),
    }
    status = result.get("status") if isinstance(result, dict) else None
    if not isinstance(status, str) or status not in STATUSES:
        result = {
            "status": "product_failure",
            "duration_ms": 0,
            "error_type": "InvalidProbeResult",
        }
    normalized: dict[str, Any] = {
        "id": probe_id,
        "status": result["status"],
        "duration_ms": _safe_int(result.get("duration_ms")),
    }
    if result.get("error_type"):
        error_type = safe_error_type(result["error_type"])
        normalized["error_type"] = error_type
        if error_type == "ProbeTimeout":
            normalized["status"] = "external_failure"
        elif error_type in PRODUCT_RUNTIME_ERRORS or normalized["status"] == "passed":
            normalized["status"] = "product_failure"
    if probe_id == "LIVE-01":
        raw_event_types = result.get("event_types", [])
        normalized.update(
            event_types=[
                item if isinstance(item, str) and item in EVENT_TYPES else "unknown"
                for item in (
                    raw_event_types[:_MAX_EVENT_TYPES]
                    if isinstance(raw_event_types, list)
                    else []
                )
            ],
            finish_reason=(
                result.get("finish_reason")
                if isinstance(result.get("finish_reason"), str)
                and result.get("finish_reason") in FINISH_REASONS
                else (
                    "other"
                    if isinstance(result.get("finish_reason"), str)
                    and result.get("finish_reason")
                    else ""
                )
            ),
            usage_totals=_usage(result.get("usage_totals")),
            final_text_length=_safe_int(result.get("final_text_length")),
        )
    elif probe_id == "LIVE-02":
        raw_tool_names = result.get("tool_names", [])
        normalized.update(
            tool_names=[
                item
                for item in (
                    raw_tool_names[:_MAX_TOOL_NAMES]
                    if isinstance(raw_tool_names, list)
                    else []
                )
                if isinstance(item, str)
                and item in {"read_file", "<unexpected>"}
            ],
            result_status=(
                result.get("result_status")
                if isinstance(result.get("result_status"), str)
                and result.get("result_status")
                in {"missing", "failed", "succeeded", "external_failure"}
                else "failed"
            ),
            read_call_count=_safe_int(result.get("read_call_count")),
            marker_observed=_safe_bool(result.get("marker_observed", False)),
        )
    elif probe_id == "LIVE-03":
        normalized.update(
            delta_observed=_safe_bool(result.get("delta_observed", False)),
            stream_was_active=_safe_bool(result.get("stream_was_active", False)),
            cancellation_completed=_safe_bool(
                result.get("cancellation_completed", False)
            ),
            cancel_wait_ms=_safe_int(result.get("cancel_wait_ms")),
            health_request_succeeded=_safe_bool(
                result.get("health_request_succeeded", False)
            ),
        )
    else:
        normalized["status"] = "product_failure"
        normalized["error_type"] = "InvalidProbeResult"

    if normalized["status"] == "passed":
        valid = {
            "LIVE-01": lambda: (
                "done" in normalized["event_types"]
                and "error" not in normalized["event_types"]
                and numeric_evidence_valid["LIVE-01"]
            ),
            "LIVE-02": lambda: (
                normalized["tool_names"] == ["read_file"]
                and normalized["result_status"] == "succeeded"
                and numeric_evidence_valid["LIVE-02"]
                and normalized["marker_observed"] is True
            ),
            "LIVE-03": lambda: (
                normalized["delta_observed"] is True
                and normalized["stream_was_active"] is True
                and normalized["cancellation_completed"] is True
                and numeric_evidence_valid["LIVE-03"]
                and normalized["health_request_succeeded"] is True
            ),
        }.get(probe_id)
        if valid is None or not valid():
            normalized["status"] = "product_failure"
    return normalized
