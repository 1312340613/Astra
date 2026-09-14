"""Reusable, resource-scoped approval previews for interactive frontends."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .registry import PermissionCleanup


def normalized_origin(url: str) -> str:
    """Return a stable HTTP(S) origin, or an empty string for invalid URLs."""
    try:
        parsed = urlsplit(str(url).strip())
    except ValueError:
        return ""
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    default_port = 80 if scheme == "http" else 443
    suffix = f":{port}" if port and port != default_port else ""
    return f"{scheme}://{host}{suffix}"


@dataclass
class ScopedApprovalStore:
    """Track explicit session grants without broadening them to a whole tool.

    ``enabled`` lets non-interactive frontends retain their existing policy
    behavior. Interactive backends pass a predicate tied to the approval
    broker, so a preflight can pause the exact original call.
    """

    enabled: Callable[[], bool] = lambda: True
    approved_scopes: set[str] = field(default_factory=set)

    def request(
        self,
        *,
        scope: str,
        kind: str,
        operation: str,
        target: str,
        reason: str,
        detail: str = "",
        arguments: dict[str, str] | None = None,
        approval_title: str = "",
        approval_summary: str = "",
        approval_effect: str = "",
        approval_boundary: str = "",
        approval_question: str = "",
        session_scope_label: str = "",
    ) -> dict[str, Any] | None:
        if not self.enabled() or scope in self.approved_scopes:
            return None
        payload: dict[str, Any] = {
            "kind": kind,
            "scope": scope,
            "operation": operation,
            "target": target,
            "reason": reason,
        }
        if detail:
            payload["detail"] = detail
        if arguments is not None:
            payload["arguments"] = arguments
        for key, value in {
            "approval_title": approval_title,
            "approval_summary": approval_summary,
            "approval_effect": approval_effect,
            "approval_boundary": approval_boundary,
            "approval_question": approval_question,
            "session_scope_label": session_scope_label,
        }.items():
            if value:
                payload[key] = value
        return payload

    def request_many(
        self,
        *,
        scopes: list[str],
        kind: str,
        operation: str,
        target: str,
        reason: str,
        detail: str = "",
        arguments: dict[str, str] | None = None,
        approval_title: str = "",
        approval_summary: str = "",
        approval_effect: str = "",
        approval_boundary: str = "",
        approval_question: str = "",
        session_scope_label: str = "",
    ) -> dict[str, Any] | None:
        normalized = sorted({scope for scope in scopes if scope})
        pending = [scope for scope in normalized if scope not in self.approved_scopes]
        if not self.enabled() or not pending:
            return None
        payload = self.request(
            scope=pending[0],
            kind=kind,
            operation=operation,
            target=target,
            reason=reason,
            detail=detail,
            arguments=arguments,
            approval_title=approval_title,
            approval_summary=approval_summary,
            approval_effect=approval_effect,
            approval_boundary=approval_boundary,
            approval_question=approval_question,
            session_scope_label=session_scope_label,
        )
        if payload is not None:
            payload["scopes"] = pending
        return payload

    def grant(
        self,
        _args: dict,
        request: dict[str, Any],
        decision: str,
    ) -> PermissionCleanup | None:
        raw_scopes = request.get("scopes")
        if isinstance(raw_scopes, list):
            scopes = {str(scope).strip() for scope in raw_scopes if str(scope).strip()}
        else:
            scope = str(request.get("scope") or "").strip()
            scopes = {scope} if scope else set()
        if not scopes:
            raise ValueError("approval request is missing a scope")
        newly_approved = scopes - self.approved_scopes
        self.approved_scopes.update(scopes)
        if decision == "once":
            return lambda: self.approved_scopes.difference_update(newly_approved)
        return None
