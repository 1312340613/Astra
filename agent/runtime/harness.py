"""Harness-level request state, diagnostics, and runtime policy helpers."""

from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse

from .time_utils import current_datetime


class RequestStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    DONE = "done"
    ERROR = "error"


@dataclass
class RequestLifecycle:
    request_id: str
    status: RequestStatus = RequestStatus.QUEUED
    error: str = ""
    events: list[str] = field(default_factory=list)

    def start(self):
        self.status = RequestStatus.RUNNING
        self.events.append("start")

    def cancel(self):
        if self.status not in {RequestStatus.DONE, RequestStatus.ERROR}:
            self.status = RequestStatus.CANCELLING
            self.events.append("cancel")

    def finish(self):
        if self.status != RequestStatus.ERROR:
            self.status = RequestStatus.DONE
        self.events.append("finish")

    def fail(self, message: str):
        self.status = RequestStatus.ERROR
        self.error = message
        self.events.append("error")


def is_local_base_url(base_url: str) -> bool:
    parsed = urlparse(base_url)
    return parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def default_llm_timeout(base_url: str, env_timeout: str | None, fallback: float) -> float:
    if env_timeout is not None:
        try:
            return float(env_timeout)
        except ValueError:
            return fallback
    if is_local_base_url(base_url):
        return 0.0
    return fallback


def build_health_report(llm_config, tools, context_limit: int, sandbox=None) -> str:
    timeout = getattr(llm_config, "timeout", 0)
    timeout_text = "disabled" if not timeout or timeout <= 0 else f"{timeout:g}s"
    sandbox_name = getattr(sandbox, "description", type(sandbox).__name__) if sandbox is not None else "none"
    tool_names = sorted(getattr(tools, "tool_names", []))
    now = current_datetime()
    lines = [
        "Harness health:",
        f"Model: {llm_config.model}",
        f"Provider: {getattr(llm_config, 'provider', 'openai-compatible')}",
        f"Base URL: {llm_config.base_url}",
        f"Local model: {'yes' if is_local_base_url(llm_config.base_url) else 'no'}",
        f"LLM timeout: {timeout_text}",
        f"Context limit: {context_limit}",
        f"Tools: {len(tool_names)} registered",
        f"Tool policy: {getattr(getattr(tools, 'policy', None), 'mode', 'unknown')}",
        f"Sandbox: {sandbox_name}",
        f"Runtime time: {now.strftime('%Y-%m-%d %H:%M:%S %Z %z')}",
    ]
    if tool_names:
        lines.append("Tool names: " + ", ".join(tool_names))
    return "\n".join(lines)
