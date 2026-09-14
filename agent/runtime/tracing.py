"""Optional OpenTelemetry tracing with a safe no-op fallback."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class TracingStatus:
    enabled: bool
    backend: str
    error: str = ""


@dataclass
class TraceContext:
    """Correlation IDs shared across all spans in a single request turn.

    Create one per ReAct loop iteration and pass it to trace_span calls
    so that LLM, tool, memory, and browser spans can be correlated.
    """
    session_id: str = ""
    task_id: str = ""
    request_id: str = ""
    tool_call_id: str = ""
    memory_id: str = ""
    browser_session_id: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def to_attributes(self) -> dict[str, str]:
        """Flatten to OTel-safe span attributes."""
        attrs: dict[str, str] = {}
        if self.session_id:
            attrs["agent.session_id"] = self.session_id
        if self.task_id:
            attrs["agent.task_id"] = self.task_id
        if self.request_id:
            attrs["agent.request_id"] = self.request_id
        if self.tool_call_id:
            attrs["agent.tool_call_id"] = self.tool_call_id
        if self.memory_id:
            attrs["agent.memory_id"] = self.memory_id
        if self.browser_session_id:
            attrs["agent.browser_session_id"] = self.browser_session_id
        for key, value in self.extra.items():
            attrs[f"agent.{key}"] = value
        return attrs

    def with_tool_call(self, tool_call_id: str) -> "TraceContext":
        """Return a copy scoped to a specific tool call."""
        return TraceContext(
            session_id=self.session_id,
            task_id=self.task_id,
            request_id=self.request_id,
            tool_call_id=tool_call_id,
            memory_id=self.memory_id,
            browser_session_id=self.browser_session_id,
            extra=dict(self.extra),
        )

    def with_memory_id(self, memory_id: str) -> "TraceContext":
        scoped = self.with_tool_call(self.tool_call_id)
        scoped.memory_id = memory_id
        return scoped

    def with_browser_session(self, browser_session_id: str) -> "TraceContext":
        scoped = self.with_tool_call(self.tool_call_id)
        scoped.browser_session_id = browser_session_id
        return scoped


_status = TracingStatus(False, "disabled")
_tracer = None
_configured = False


def configure_tracing(service_name: str = "agent-lab") -> TracingStatus:
    """Configure OTLP tracing once; failures never prevent agent startup."""
    global _configured, _status, _tracer
    if _configured:
        return _status
    _configured = True
    if not _truthy(os.getenv("AGENT_TRACE_ENABLED")):
        return _status
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        backend = "memory"
        if endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
            backend = endpoint
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(service_name)
        _status = TracingStatus(True, backend)
    except Exception as exc:  # optional integration must degrade cleanly
        _status = TracingStatus(False, "unavailable", f"{type(exc).__name__}: {exc}")
    return _status


def tracing_status() -> TracingStatus:
    return configure_tracing()


@contextmanager
def trace_span(
    name: str,
    attributes: dict[str, Any] | None = None,
    ctx: TraceContext | None = None,
) -> Iterator[Any]:
    status = configure_tracing()
    if not status.enabled or _tracer is None:
        yield None
        return
    merged: dict[str, Any] = {}
    if ctx is not None:
        merged.update(ctx.to_attributes())
    merged.update({
        key: value
        for key, value in (attributes or {}).items()
        if value is not None and isinstance(value, (str, bool, int, float))
    })
    with _tracer.start_as_current_span(name, attributes=merged) as span:
        try:
            yield span
        except BaseException as exc:
            span.record_exception(exc)
            raise

