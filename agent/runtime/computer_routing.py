"""Bounded routing suggestions; these carry no plan or permission authority."""
from __future__ import annotations

from .macos_computer_compatibility import (
    CompatibilityRegistryError,
    exact_dispatch_compatibility,
)

_BACKENDS = ("pid_pointer", "pid_keyboard", "foreground_keyboard")
_BROWSER_BUNDLES = frozenset({
    "com.apple.Safari", "com.google.Chrome", "com.microsoft.edgemac",
    "org.mozilla.firefox", "com.brave.Browser",
})


def computer_route_advice(bundle_id: str, app_version: str) -> dict[str, object]:
    """Suggest exact configured routes without inferring execution or browser sessions.

    The source registry may differ from a running helper's bundled registry.
    Native planning/approval remains authoritative; configured is not live-tested.
    """
    advice: dict[str, object] = {
        "advisory_only": True,
        "evidence": "unknown",
        "real_app_acceptance": "unknown",
        "routes": [],
    }
    if (
        not isinstance(bundle_id, str) or not 0 < len(bundle_id) <= 256
        or not isinstance(app_version, str) or not 0 < len(app_version) <= 128
    ):
        return advice
    advice["generic_foreground"] = {
        "suggested_mode": "foreground_takeover",
        "backends": ["foreground_pointer", "foreground_keyboard"],
        "exact_plan_required": True,
        "uses_real_pointer": True,
        "requires_readable_snapshot": True,
        "availability": "subject_to_native_planning_and_takeover_authorization",
    }
    routes: list[dict[str, object]] = []
    try:
        for backend in _BACKENDS:
            cell = exact_dispatch_compatibility(bundle_id, app_version, backend)
            if cell is None:
                continue
            routes.append({
                "backend": backend,
                "enabled_actions": sorted(cell.enabled_actions),
                "requires_active": cell.requires_active,
                "suggested_mode": (
                    "foreground_takeover"
                    if cell.requires_active or backend == "foreground_keyboard"
                    else "background"
                ),
                "exact_plan_required": True,
            })
    except CompatibilityRegistryError:
        routes = []
    if routes:
        advice["evidence"] = "configured"
        advice["routes"] = routes
    if bundle_id in _BROWSER_BUNDLES:
        advice["browser_advice"] = {
            "suggestion": "consider_browser_dom_tools",
            "session_binding": "explicit_matching_session_required",
            "authentication_transfer": False,
            "cdp_availability": "unknown",
        }
    return advice
