"""Tool and session lifecycle hooks.

Hooks allow cross-cutting concerns (logging, auditing, safety verification,
memory retention) to observe and optionally modify tool execution without
coupling those concerns into the tool implementations themselves.

Hook types:
- before_tool(name, args, tool_def) -> args | None
    Called before structural validation and authorization.
    Return modified args to alter the call, or None to proceed unchanged.
    Raise HookReject to block execution with a reason.

- after_tool(name, args, result, tool_def) -> result | None
    Called after successful execution.
    Return modified result to alter the output, or None to proceed unchanged.

- tool_decision(name, args, tool_def) -> ToolDecision | None
    Called after structural validation and before policy authorization.
    Decisions are monotonic: deny wins over ask, and neither can be weakened
    by a later hook. An allow decision never bypasses the built-in policy.

- around_tool(name, args, tool_def, call_next) -> result
    Wraps each raw execution attempt. Around hooks are operational middleware;
    their exceptions follow the normal tool error/retry path.

- tool_result(name, args, result, tool_def) -> None
    Observes the final authoritative result. The observer receives copies and
    cannot modify the result returned to the caller.

- tool_error(name, args, error, tool_def) -> None
    Called after a failed execution (exception or timeout).
    Observational only — cannot modify the error.

- session_end(session_id, reason) -> None
    Called when a session is ending (user quit, timeout, handoff).
    Observational only.

- runtime_event(event) -> None
    Observes bounded, redacted runtime lifecycle events. Unknown fields and
    unsupported event types are discarded before observers are called.

- memory_retain(record) -> record | None
    Called before a memory record is persisted.
    Return modified record to alter what's stored, or None to proceed unchanged.
    Raise HookReject to block retention.

- pre_compact(event) / post_compact(event) -> None
    Observe compaction boundaries. These hooks cannot replace or block the
    canonical compression result.

Observer and transform hooks are best-effort. Around hooks are deliberately
part of execution, so their exceptions propagate into the normal tool error
and retry path. HookReject intentionally blocks the relevant operation.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class HookReject(Exception):
    """Raised by a hook to intentionally block an operation."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ToolDecision:
    """A typed, non-authoritative tool gate decision.

    ``allow`` means this hook has no objection; normal policy checks still run.
    ``ask`` requests interactive approval. ``deny`` blocks execution.
    """

    kind: str
    reason: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"allow", "ask", "deny"}:
            raise ValueError(f"invalid tool decision: {self.kind!r}")

    @classmethod
    def allow(cls, reason: str = "") -> ToolDecision:
        return cls("allow", reason)

    @classmethod
    def ask(cls, reason: str = "") -> ToolDecision:
        return cls("ask", reason)

    @classmethod
    def deny(cls, reason: str = "") -> ToolDecision:
        return cls("deny", reason)


# ---------------------------------------------------------------------------
# Hook protocols
# ---------------------------------------------------------------------------

@runtime_checkable
class BeforeToolHook(Protocol):
    def __call__(self, name: str, args: dict, tool_def: Any) -> dict | None: ...


@runtime_checkable
class AfterToolHook(Protocol):
    def __call__(self, name: str, args: dict, result: dict, tool_def: Any) -> dict | None: ...


@runtime_checkable
class ToolErrorHook(Protocol):
    def __call__(self, name: str, args: dict, error: str, tool_def: Any, /) -> None: ...


@runtime_checkable
class ToolDecisionHook(Protocol):
    def __call__(self, name: str, args: dict, tool_def: Any) -> ToolDecision | None: ...


@runtime_checkable
class AroundToolHook(Protocol):
    def __call__(
        self,
        name: str,
        args: dict,
        tool_def: Any,
        call_next: Callable[[], Awaitable[Any]],
    ) -> Awaitable[Any]: ...


@runtime_checkable
class ToolResultHook(Protocol):
    def __call__(self, name: str, args: dict, result: dict, tool_def: Any, /) -> None: ...


@runtime_checkable
class SessionEndHook(Protocol):
    def __call__(self, session_id: str, reason: str, /) -> None: ...


@runtime_checkable
class MemoryRetainHook(Protocol):
    def __call__(self, record: dict) -> dict | None: ...


@runtime_checkable
class CompactHook(Protocol):
    def __call__(self, event: dict) -> None: ...


@runtime_checkable
class RuntimeEventHook(Protocol):
    def __call__(self, event: dict) -> None: ...


_RUNTIME_EVENT_TYPES = frozenset({
    "foreground_takeover_begin",
    "foreground_takeover_end",
    "foreground_takeover_focus_restored",
    "foreground_takeover_focus_preserved",
    "foreground_takeover_restore_failed",
    "foreground_takeover_user_activity_paused",
})
_RUNTIME_ACTION_CLASSES = frozenset({"press", "text", "click", "double_click", "scroll", "drag"})


# ---------------------------------------------------------------------------
# Hook registry
# ---------------------------------------------------------------------------

@dataclass
class HookRegistry:
    """Central registry for lifecycle hooks.

    Hooks are called in registration order. Each hook category is
    independent — registering a before_tool hook does not affect
    after_tool hooks.
    """

    _before_tool: list[BeforeToolHook] = field(default_factory=list)
    _tool_decision: list[ToolDecisionHook] = field(default_factory=list)
    _around_tool: list[AroundToolHook] = field(default_factory=list)
    _after_tool: list[AfterToolHook] = field(default_factory=list)
    _tool_result: list[ToolResultHook] = field(default_factory=list)
    _tool_error: list[ToolErrorHook] = field(default_factory=list)
    _session_end: list[SessionEndHook] = field(default_factory=list)
    _memory_retain: list[MemoryRetainHook] = field(default_factory=list)
    _pre_compact: list[CompactHook] = field(default_factory=list)
    _post_compact: list[CompactHook] = field(default_factory=list)
    _runtime_event: list[RuntimeEventHook] = field(default_factory=list)

    # -- registration -------------------------------------------------------

    def on_before_tool(self, hook: BeforeToolHook) -> BeforeToolHook:
        self._before_tool.append(hook)
        return hook

    def on_after_tool(self, hook: AfterToolHook) -> AfterToolHook:
        self._after_tool.append(hook)
        return hook

    def on_tool_error(self, hook: ToolErrorHook) -> ToolErrorHook:
        self._tool_error.append(hook)
        return hook

    def on_session_end(self, hook: SessionEndHook) -> SessionEndHook:
        self._session_end.append(hook)
        return hook

    def on_memory_retain(self, hook: MemoryRetainHook) -> MemoryRetainHook:
        self._memory_retain.append(hook)
        return hook

    def on_tool_decision(self, hook: ToolDecisionHook) -> ToolDecisionHook:
        self._tool_decision.append(hook)
        return hook

    def on_around_tool(self, hook: AroundToolHook) -> AroundToolHook:
        self._around_tool.append(hook)
        return hook

    def on_tool_result(self, hook: ToolResultHook) -> ToolResultHook:
        self._tool_result.append(hook)
        return hook

    def on_pre_compact(self, hook: CompactHook) -> CompactHook:
        self._pre_compact.append(hook)
        return hook

    def on_post_compact(self, hook: CompactHook) -> CompactHook:
        self._post_compact.append(hook)
        return hook

    def on_runtime_event(self, hook: RuntimeEventHook) -> RuntimeEventHook:
        self._runtime_event.append(hook)
        return hook

    # -- dispatch -----------------------------------------------------------

    def dispatch_before_tool(self, name: str, args: dict, tool_def: Any) -> dict:
        """Run before_tool hooks. Returns (possibly modified) args.

        Raises HookReject if any hook blocks the call.
        """
        current_args = args
        for hook in self._before_tool:
            try:
                result = hook(name, current_args, tool_def)
                if result is not None:
                    current_args = result
            except HookReject:
                raise
            except Exception:
                logger.exception("before_tool hook failed for %s", name)
        return current_args

    def dispatch_after_tool(self, name: str, args: dict, result: dict, tool_def: Any) -> dict:
        """Run after_tool hooks. Returns (possibly modified) result."""
        current_result = result
        for hook in self._after_tool:
            try:
                modified = hook(name, args, current_result, tool_def)
                if modified is not None:
                    current_result = modified
            except HookReject:
                raise
            except Exception:
                logger.exception("after_tool hook failed for %s", name)
        return current_result

    def dispatch_tool_decision(self, name: str, args: dict, tool_def: Any) -> ToolDecision:
        """Combine decision hooks without allowing a later hook to weaken one."""
        decision = ToolDecision.allow()
        for hook in self._tool_decision:
            try:
                candidate = hook(name, dict(args), tool_def)
                if candidate is None or candidate.kind == "allow":
                    continue
                if candidate.kind == "deny":
                    return candidate
                if decision.kind == "allow":
                    decision = candidate
            except HookReject as exc:
                return ToolDecision.deny(exc.reason)
            except Exception:
                logger.exception("tool_decision hook failed for %s", name)
        return decision

    async def dispatch_around_tool(
        self,
        name: str,
        args: dict,
        tool_def: Any,
        invoke: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Compose around hooks in registration order (first is outermost)."""
        async def call_at(index: int) -> Any:
            if index >= len(self._around_tool):
                return await invoke()
            hook = self._around_tool[index]
            return await hook(name, dict(args), tool_def, lambda: call_at(index + 1))

        return await call_at(0)

    def dispatch_tool_result(self, name: str, args: dict, result: dict, tool_def: Any) -> None:
        """Observe a final result without exposing the authoritative objects."""
        for hook in self._tool_result:
            try:
                hook(name, copy.deepcopy(args), copy.deepcopy(result), tool_def)
            except Exception:
                logger.exception("tool_result hook failed for %s", name)

    def dispatch_tool_error(self, name: str, args: dict, error: str, tool_def: Any) -> None:
        """Run tool_error hooks. Observational only."""
        for hook in self._tool_error:
            try:
                hook(name, args, error, tool_def)
            except Exception:
                logger.exception("tool_error hook failed for %s", name)

    def dispatch_session_end(self, session_id: str, reason: str) -> None:
        """Run session_end hooks. Observational only."""
        for hook in self._session_end:
            try:
                hook(session_id, reason)
            except Exception:
                logger.exception("session_end hook failed for session %s", session_id)

    def dispatch_memory_retain(self, record: dict) -> dict:
        """Run memory_retain hooks. Returns (possibly modified) record.

        Raises HookReject if any hook blocks retention.
        """
        current_record = record
        for hook in self._memory_retain:
            try:
                modified = hook(current_record)
                if modified is not None:
                    current_record = modified
            except HookReject:
                raise
            except Exception:
                logger.exception("memory_retain hook failed")
        return current_record

    def dispatch_pre_compact(self, event: dict) -> None:
        for hook in self._pre_compact:
            try:
                hook(dict(event))
            except Exception:
                logger.exception("pre_compact hook failed")

    def dispatch_post_compact(self, event: dict) -> None:
        for hook in self._post_compact:
            try:
                hook(dict(event))
            except Exception:
                logger.exception("post_compact hook failed")

    def dispatch_runtime_event(self, event: dict) -> None:
        """Publish one bounded event without private Computer Use state."""

        if not isinstance(event, dict) or event.get("type") not in _RUNTIME_EVENT_TYPES:
            return
        application = event.get("application")
        raw_classes = event.get("action_classes")
        safe: dict[str, Any] = {"type": event["type"]}
        if isinstance(application, str) and application:
            label = " ".join(application.split())[:160]
            if label and not any(marker in label for marker in ("/", "\\", "\x00")):
                safe["application"] = label
        if isinstance(raw_classes, (list, tuple)):
            safe["action_classes"] = list(dict.fromkeys(
                value for value in raw_classes
                if isinstance(value, str) and value in _RUNTIME_ACTION_CLASSES
            ))[: len(_RUNTIME_ACTION_CLASSES)]
        for hook in self._runtime_event:
            try:
                hook(copy.deepcopy(safe))
            except Exception:
                logger.exception("runtime_event hook failed for %s", safe["type"])

    # -- introspection ------------------------------------------------------

    @property
    def counts(self) -> dict[str, int]:
        return {
            "before_tool": len(self._before_tool),
            "tool_decision": len(self._tool_decision),
            "around_tool": len(self._around_tool),
            "after_tool": len(self._after_tool),
            "tool_result": len(self._tool_result),
            "tool_error": len(self._tool_error),
            "session_end": len(self._session_end),
            "memory_retain": len(self._memory_retain),
            "pre_compact": len(self._pre_compact),
            "post_compact": len(self._post_compact),
            "runtime_event": len(self._runtime_event),
        }

    def clear(self) -> None:
        """Remove all hooks (useful for testing)."""
        self._before_tool.clear()
        self._tool_decision.clear()
        self._around_tool.clear()
        self._after_tool.clear()
        self._tool_result.clear()
        self._tool_error.clear()
        self._session_end.clear()
        self._memory_retain.clear()
        self._pre_compact.clear()
        self._post_compact.clear()
        self._runtime_event.clear()
