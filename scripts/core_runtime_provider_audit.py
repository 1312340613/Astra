"""Bounded, content-safe audit probes for a configured real provider."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import re
import socket
import ssl
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.cli.environment import load_project_env
from agent.cli.model_catalog import discover_model_catalog
from agent.cli.model_preferences import read_selected_model
from agent.core.msg import ContentBlock, Msg
from agent.runtime.llm import TRANSPORT_REQUEST_ERRORS, APIError, LLMClient, LLMConfig
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from scripts.core_runtime_audit_files import AuditRun, create_audit_run, open_audit_run
from scripts.core_runtime_audit_results import normalize_worker_result
from scripts.core_runtime_audit_worker import ProbeManagementError, send_worker_result
from scripts.core_runtime_audit_worker import run_probe_worker as _run_isolated_worker

_SECRET_PATTERNS = (
    re.compile(
        r"(?i)([\"']?authorization[\"']?\s*[=:]\s*[\"']?bearer\s+)"
        r"[^\s,;}\"']+"
    ),
    re.compile(
        r"(?i)([\"']?(?:api[_-]?key|token|password)[\"']?\s*[=:]\s*[\"']?)"
        r"[^\s,;}\"']+"
    ),
)

PROBE_TIMEOUT_SECONDS = 180.0
CANCEL_AFTER_SECONDS = 15.0
CANCEL_FINISH_SECONDS = 5.0
_PROBE_IDS = ("LIVE-01", "LIVE-02", "LIVE-03")
_EVENT_TYPES = {
    "chunk",
    "done",
    "error",
    "reasoning",
    "tool_calls",
    "tool_result",
}
_FINISH_REASONS = {
    "stop",
    "length",
    "tool_calls",
    "content_filter",
    "max_tokens",
    "max_output_tokens",
}
_EXTERNAL_ERROR_NAMES = {
    "APIConnectionError",
    "APIError",
    "APITimeoutError",
    "ConnectError",
    "ConnectTimeout",
    "HTTPError",
    "HTTPStatusError",
    "NetworkError",
    "PoolTimeout",
    "ProxyError",
    "RateLimitError",
    "ReadError",
    "ReadTimeout",
    "RemoteProtocolError",
    "SSLError",
    "TimeoutException",
    "TransportError",
    "WriteError",
    "WriteTimeout",
}
_EXTERNAL_EVENT_CODES = {
    "api_error",
    "connection_error",
    "dns_error",
    "http_5xx",
    "idle_timeout",
    "overall_timeout",
    "rate_limit",
    "tls_error",
    "transport_error",
}


def redact_error(value: object) -> str:
    text = " ".join(str(value or "").split())[:2_000]
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1<redacted>", text)
    return text


def probes_pass(probes: list[dict]) -> bool:
    return (
        sum(probe.get("status") == "passed" for probe in probes) >= 2
        and all(probe.get("status") != "product_failure" for probe in probes)
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _duration_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1_000))


def _safe_identifier(value: object, *, fallback: str = "unknown") -> str:
    text = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(value or ""))[:120]
    return text or fallback


def _safe_model_key(value: object) -> str:
    return _safe_identifier(redact_error(value))


def _safe_event_type(value: object) -> str:
    event_type = str(value or "")
    return event_type if event_type in _EVENT_TYPES else "unknown"


def _safe_finish_reason(value: object) -> str:
    reason = str(value or "").lower()
    return reason if reason in _FINISH_REASONS else ("other" if reason else "")


def _is_external_error_event(event: dict) -> bool:
    return str(event.get("code") or event.get("error_type") or "").lower() in (
        _EXTERNAL_EVENT_CODES
    )


def _usage_totals(value: object) -> dict[str, int]:
    usage = value if isinstance(value, dict) else {}

    def count(name: str) -> int:
        try:
            return max(0, int(usage.get(name, 0) or 0))
        except (TypeError, ValueError):
            return 0

    return {
        "prompt_tokens": count("prompt_tokens"),
        "completion_tokens": count("completion_tokens"),
        "total_tokens": count("total_tokens"),
    }


def _is_external_failure(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            asyncio.TimeoutError,
            ConnectionError,
            APIError,
            *TRANSPORT_REQUEST_ERRORS,
            httpx.RequestError,
            socket.gaierror,
            ssl.SSLError,
            TimeoutError,
        ),
    ):
        return True
    name = type(exc).__name__
    normalized_name = name.replace("_", "").lower()
    if (
        name in _EXTERNAL_ERROR_NAMES
        or name.endswith("APIError")
        or "ratelimit" in normalized_name
    ):
        return True
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    try:
        return int(status_code) == 429 or int(status_code) >= 500
    except (TypeError, ValueError):
        return False


def _failure_probe(probe_id: str, exc: BaseException, duration_ms: int) -> dict:
    return {
        "id": probe_id,
        "status": "external_failure" if _is_external_failure(exc) else "product_failure",
        "duration_ms": duration_ms,
        # Exception messages can echo prompts or provider headers. Persist only
        # the bounded class identifier; redact_error remains for console callers.
        "error_type": _safe_identifier(type(exc).__name__),
    }


async def _resolve_entry(args: argparse.Namespace):
    catalog = await discover_model_catalog(timeout=float(args.discovery_timeout))
    selected = str(args.model_key or read_selected_model() or "").strip()
    if not selected:
        raise LookupError("No model selected for the provider audit")
    entry = catalog.resolve_persisted(selected)
    if entry is None:
        raise LookupError("The selected model is not present in the configured catalog")
    return entry


def _build_client(entry, args: argparse.Namespace) -> LLMClient:
    profile = entry.profile
    config = LLMConfig(
        provider=profile.provider,
        model=entry.model_id,
        api_key=profile.api_key() or os.getenv("LLM_API_KEY", "") or "local-no-key",
        base_url=entry.base_url,
        capabilities=profile.capabilities,
        temperature=profile.temperature,
        max_tokens=min(512, max(64, int(profile.max_tokens))),
        top_p=profile.top_p,
        top_k=profile.top_k,
        min_p=profile.min_p,
        presence_penalty=profile.presence_penalty,
        repetition_penalty=profile.repetition_penalty,
        repetition_penalty_parameter=profile.repetition_penalty_parameter,
        connect_timeout=30.0,
        idle_timeout=float(args.idle_timeout),
        overall_timeout=PROBE_TIMEOUT_SECONDS,
    )
    return LLMClient(config)


async def _close_one(target: object) -> bool:
    for name in ("aclose", "close"):
        close = getattr(target, name, None)
        if not callable(close):
            continue
        result = close()
        if inspect.isawaitable(result):
            await result
        return True
    return False


async def _close_client(client: object) -> None:
    if await _close_one(client):
        return
    provider = getattr(client, "provider", None)
    if provider is None or await _close_one(provider):
        return
    route_clients = getattr(provider, "_route_clients", None)
    candidates = (
        list(route_clients.values())
        if isinstance(route_clients, dict)
        else [getattr(provider, "_client", None)]
    )
    closed: set[int] = set()
    for candidate in candidates:
        if candidate is None or id(candidate) in closed:
            continue
        closed.add(id(candidate))
        await _close_one(candidate)


def _build_read_agent(
    client: LLMClient,
    audit_run: AuditRun,
    marker_identity: tuple[int, int, int, str],
    args: argparse.Namespace,
) -> ReActAgent:
    output_dir = audit_run.path
    registry = _build_audit_read_registry(audit_run, marker_identity)
    registry.artifact_dir = output_dir / "tool-results"
    agent = ReActAgent(
        "core-runtime-provider-audit",
        client,
        registry,
        system_prompt=(
            "This is an isolated read-only runtime audit. Use read_file exactly once on the "
            "requested relative path. Do not call any other tool."
        ),
        max_iterations=max(1, int(getattr(args, "max_iterations", 2))),
        progressive_tools=False,
        vision_cache_root=output_dir / "image-cache" / "tiles",
        timing_log_enabled=False,
        minimal_mode=True,
        query_profile_enabled=False,
    )
    agent.tool_allowlist = {"read_file"}
    agent._sandbox = SimpleNamespace(workdir=str(output_dir))

    def finish_after_read(
        full_content: str,
        calls: list[dict],
        events: list[dict],
    ) -> bool:
        del full_content
        return (
            len(calls) == 1
            and str(calls[0].get("name") or "") == "read_file"
            and len(events) == 1
            and str(events[0].get("name") or "") == "read_file"
            and not events[0].get("error")
        )

    agent.finalize_after_tools_provider = finish_after_read
    return agent


def _build_audit_read_registry(
    audit_run: AuditRun,
    marker_identity: tuple[int, int, int, str],
) -> ToolRegistry:
    """Expose only the descriptor-anchored audit read contract."""
    registry = ToolRegistry(artifact_dir=audit_run.path / "tool-results")

    def read_file(path: str) -> str:
        return audit_run.read_verified_probe_input(path, marker_identity)

    registry.register(
        ToolDef(
            name="read_file",
            description="Read the audit probe input file.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            fn=read_file,
            risk="read",
            approval="never",
            replay="safe",
            cache_results=False,
        )
    )
    return registry


async def _normal_stream_probe(entry, args: argparse.Namespace) -> dict:
    started = time.perf_counter()
    client = _build_client(entry, args)
    try:
        event_types: list[str] = []
        streamed_text_length = 0
        final_text_length = 0
        finish_reason = ""
        usage_totals = _usage_totals(None)
        completed = False
        async for event in client.chat_stream(
            [{"role": "user", "content": "Reply briefly with the single word OK."}],
            None,
        ):
            event_type = _safe_event_type(event.get("type"))
            if event_type not in event_types:
                event_types.append(event_type)
            if event_type == "chunk":
                streamed_text_length += len(str(event.get("content") or ""))
            if event_type == "done":
                completed = True
                done_content = str(event.get("content") or "")
                final_text_length = (
                    len(done_content) if done_content else streamed_text_length
                )
                finish_reason = _safe_finish_reason(
                    event.get("finish_reason") or event.get("stop_reason")
                )
                usage_totals = _usage_totals(event.get("usage"))
        if not final_text_length:
            final_text_length = streamed_text_length
        passed = completed and final_text_length > 0 and "error" not in event_types
        return {
            "id": "LIVE-01",
            "status": "passed" if passed else "product_failure",
            "duration_ms": _duration_ms(started),
            "event_types": event_types,
            "finish_reason": finish_reason,
            "usage_totals": usage_totals,
            "final_text_length": final_text_length,
        }
    finally:
        await _close_client(client)


async def _read_tool_probe(
    entry,
    args: argparse.Namespace,
    audit_run: AuditRun,
    marker: str,
    marker_identity: tuple[int, int, int, str],
) -> dict:
    started = time.perf_counter()
    client = _build_client(entry, args)
    try:
        agent = _build_read_agent(client, audit_run, marker_identity, args)
        tool_names: list[str] = []
        marker_observed = False
        result_status = "missing"
        saw_error = False
        external_error = False
        read_call_count = 0
        async for event in agent.reply_stream(
            Msg(
                content=[ContentBlock.text(
                    "Call read_file exactly once for probe-input.txt, then stop after the result."
                )]
            )
        ):
            event_type = _safe_event_type(event.get("type"))
            if event_type == "tool_calls":
                for call in event.get("calls") or []:
                    name = str(call.get("name") or "")
                    safe_name = "read_file" if name == "read_file" else "<unexpected>"
                    if safe_name not in tool_names:
                        tool_names.append(safe_name)
            elif event_type == "tool_result":
                name = str(event.get("name") or event.get("tool_name") or "")
                safe_name = "read_file" if name == "read_file" else "<unexpected>"
                if safe_name not in tool_names:
                    tool_names.append(safe_name)
                if name == "read_file":
                    read_call_count += 1
                if event.get("error"):
                    result_status = "failed"
                    saw_error = True
                elif name == "read_file":
                    result_status = "succeeded"
                    output = str(event.get("output") or event.get("tool_output") or "")
                    marker_observed = marker_observed or marker in output
            elif event_type == "error":
                saw_error = True
                external_error = external_error or _is_external_error_event(event)
        passed = (
            tool_names == ["read_file"]
            and read_call_count == 1
            and result_status == "succeeded"
            and marker_observed
            and not saw_error
        )
        return {
            "id": "LIVE-02",
            "status": (
                "passed"
                if passed
                else ("external_failure" if external_error else "product_failure")
            ),
            "duration_ms": _duration_ms(started),
            "tool_names": tool_names,
            "result_status": "external_failure" if external_error else result_status,
            "read_call_count": read_call_count,
            "marker_observed": marker_observed,
        }
    finally:
        await _close_client(client)


def _observable_delta(event: dict) -> bool:
    event_type = _safe_event_type(event.get("type"))
    if event_type in {"chunk", "reasoning", "done"}:
        return bool(str(event.get("content") or ""))
    return event_type == "tool_calls" and bool(event.get("calls"))


async def _cancel_and_drain_task(task: asyncio.Task) -> bool:
    if not task.done():
        task.cancel()
    drain = asyncio.gather(task, return_exceptions=True)
    interrupted = False
    while not drain.done():
        try:
            await asyncio.shield(drain)
        except asyncio.CancelledError:
            interrupted = True
            if not task.done():
                task.cancel()
    return interrupted


async def _cancellation_health_probe(entry, args: argparse.Namespace) -> dict:
    started = time.perf_counter()
    client = _build_client(entry, args)
    client_closed = False
    delta_seen = asyncio.Event()

    async def close_client_once() -> None:
        nonlocal client_closed
        if client_closed:
            return
        await _close_client(client)
        client_closed = True

    async def consume_cancellable_stream() -> None:
        async for event in client.chat_stream(
            [
                {
                    "role": "user",
                    "content": (
                        "Start with READY, then produce a long numbered list so the stream "
                        "remains active until it is cancelled."
                    ),
                }
            ],
            None,
        ):
            if _observable_delta(event):
                delta_seen.set()

    try:
        stream_task = asyncio.create_task(consume_cancellable_stream())
        delta_wait = asyncio.create_task(delta_seen.wait())
        try:
            await asyncio.wait(
                {stream_task, delta_wait},
                timeout=CANCEL_AFTER_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not delta_wait.done():
                delta_wait.cancel()
            await asyncio.gather(delta_wait, return_exceptions=True)

        stream_was_active = not stream_task.done()
        stream_task.cancel()
        cancel_started = time.perf_counter()
        done, _ = await asyncio.wait({stream_task}, timeout=CANCEL_FINISH_SECONDS)
        cancellation_completed = stream_task in done
        cancel_wait_ms = _duration_ms(cancel_started)
        cancel_elapsed = time.perf_counter() - cancel_started
        if cancellation_completed and not stream_task.cancelled():
            failure = stream_task.exception()
            if failure is not None:
                raise failure
        if not cancellation_completed:
            await close_client_once()
            if await _cancel_and_drain_task(stream_task):
                raise asyncio.CancelledError
            return {
                "id": "LIVE-03",
                "status": "product_failure",
                "duration_ms": _duration_ms(started),
                "delta_observed": delta_seen.is_set(),
                "stream_was_active": stream_was_active,
                "cancellation_completed": False,
                "cancel_wait_ms": cancel_wait_ms,
                "health_request_succeeded": False,
            }

        health_completed = False
        health_text_length = 0
        async for event in client.chat_stream(
            [{"role": "user", "content": "Reply briefly with OK."}],
            None,
        ):
            event_type = _safe_event_type(event.get("type"))
            if event_type == "chunk":
                health_text_length += len(str(event.get("content") or ""))
            elif event_type == "done":
                health_completed = True
                content = str(event.get("content") or "")
                if content:
                    health_text_length = len(content)
        health_request_succeeded = health_completed and health_text_length > 0
        passed = (
            stream_was_active
            and cancellation_completed
            and cancel_elapsed <= CANCEL_FINISH_SECONDS
            and health_request_succeeded
        )
        return {
            "id": "LIVE-03",
            "status": "passed" if passed else "product_failure",
            "duration_ms": _duration_ms(started),
            "delta_observed": delta_seen.is_set(),
            "stream_was_active": stream_was_active,
            "cancellation_completed": cancellation_completed,
            "cancel_wait_ms": cancel_wait_ms,
            "health_request_succeeded": health_request_succeeded,
        }
    finally:
        cleanup_interrupted = False
        stream_task = locals().get("stream_task")
        if isinstance(stream_task, asyncio.Task) and not stream_task.done():
            try:
                await close_client_once()
            finally:
                cleanup_interrupted = await _cancel_and_drain_task(stream_task)
        await close_client_once()
        if cleanup_interrupted:
            raise asyncio.CancelledError


def _audit_output_dir() -> AuditRun:
    return create_audit_run(PROJECT_ROOT)


async def _probe_operation(
    probe_id: str,
    entry: object,
    args: argparse.Namespace,
    audit_run: AuditRun,
    marker: str,
    marker_identity: tuple[int, int, int, str],
) -> dict:
    if probe_id == "LIVE-01":
        return await _normal_stream_probe(entry, args)
    if probe_id == "LIVE-02":
        return await _read_tool_probe(entry, args, audit_run, marker, marker_identity)
    if probe_id == "LIVE-03":
        return await _cancellation_health_probe(entry, args)
    raise ValueError("Unknown audit probe")


def _probe_worker_entry(connection, probe_id: str, payload: dict[str, Any]) -> None:
    """Spawn entrypoint: provider activity never crosses into the parent process."""
    started = time.perf_counter()
    try:
        with open_audit_run(
            Path(payload["project_root"]),
            str(payload["run_name"]),
            device=int(payload["run_device"]),
            inode=int(payload["run_inode"]),
        ) as audit_run:
            audit_run.prepare_runtime()
            args = argparse.Namespace(**payload["args"])
            result = asyncio.run(
                _probe_operation(
                    probe_id,
                    payload["entry"],
                    args,
                    audit_run,
                    str(payload["marker"]),
                    (
                        int(payload["marker_identity"][0]),
                        int(payload["marker_identity"][1]),
                        int(payload["marker_identity"][2]),
                        str(payload["marker_identity"][3]),
                    ),
                )
            )
    except BaseException as exc:
        result = _failure_probe(probe_id, exc, _duration_ms(started))
    try:
        send_worker_result(connection, result)
    finally:
        connection.close()


async def _run_probe_worker(
    probe_id: str,
    payload: dict[str, Any],
    timeout: float,
    *,
    worker_target=None,
) -> dict:
    target = worker_target or _probe_worker_entry
    result = await _run_isolated_worker(
        probe_id,
        payload,
        timeout,
        worker_target=target,
    )
    return normalize_worker_result(probe_id, result)


async def run_audit(args: argparse.Namespace) -> dict:
    started_at = _utc_now()
    load_project_env(PROJECT_ROOT)
    with _audit_output_dir() as audit_run:
        output_dir = audit_run.path
        marker = f"ASTRA-CORE-RUNTIME-AUDIT-{uuid.uuid4().hex}"
        marker_text = marker + "\n"
        audit_run.write_private_text("probe-input.txt", marker_text)
        marker_identity = audit_run.regular_file_identity(
            "probe-input.txt",
            expected_content=marker_text.encode("utf-8"),
        )

        selected = "unresolved"
        probes: list[dict]
        try:
            entry = await _resolve_entry(args)
            selected = str(entry.key)
            payload = {
                "entry": entry,
                "args": {
                    "idle_timeout": float(args.idle_timeout),
                    "max_iterations": int(getattr(args, "max_iterations", 2)),
                },
                "project_root": str(audit_run.project_root),
                "run_name": audit_run.run_name,
                "run_device": audit_run.device,
                "run_inode": audit_run.inode,
                "output_dir": str(output_dir),
                "marker": marker,
                "marker_identity": marker_identity,
            }
            probes = []
            for probe_id in _PROBE_IDS:
                audit_run.prepare_runtime()
                probes.append(
                    await _run_probe_worker(
                        probe_id,
                        payload,
                        PROBE_TIMEOUT_SECONDS,
                    )
                )
        except asyncio.CancelledError:
            raise
        except ProbeManagementError:
            raise
        except Exception as exc:
            status = (
                "external_failure" if _is_external_failure(exc) else "product_failure"
            )
            probes = [
                {
                    "id": probe_id,
                    "status": status,
                    "duration_ms": 0,
                    "error_type": normalize_worker_result(
                        probe_id,
                        {
                            "status": status,
                            "error_type": type(exc).__name__,
                        },
                    ).get("error_type", "OtherError"),
                }
                for probe_id in _PROBE_IDS
            ]

        finished_at = _utc_now()
        filename = (
            "audit-"
            + started_at.replace(":", "").replace("-", "").replace(".", "")
            + f"-{uuid.uuid4().hex[:8]}.json"
        )
        report_path = output_dir / filename
        # Never add prompts, headers, API keys, or full model text to this payload.
        report: dict[str, Any] = {
            "passed": probes_pass(probes),
            "model_key": (
                _safe_model_key(selected)
                if selected != "unresolved"
                else "unresolved"
            ),
            "probes": probes,
            "started_at": started_at,
            "finished_at": finished_at,
            "report_path": str(report_path),
        }
        audit_run.write_private_text(
            filename,
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run three bounded, content-safe core-runtime provider probes."
    )
    parser.add_argument("--model-key", default="")
    parser.add_argument("--discovery-timeout", type=float, default=8.0)
    parser.add_argument("--idle-timeout", type=float, default=60.0)
    parser.add_argument("--max-iterations", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    report = asyncio.run(run_audit(parse_args()))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
