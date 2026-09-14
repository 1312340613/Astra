"""Runtime tool authorization policy and session-scoped approvals.

Supports three levels of authorization:
1. Pattern-based rules: match tool name + argument patterns (path, domain,
   arbitrary key-value) → allow / ask / deny
2. Session-scoped allow/deny sets and resource-scoped interactive grants
3. Mode-based fallback: permissive / safe / locked risk-level gating

Matching deny rules always win. Otherwise the first matching allow/ask rule
wins, preserving configured rule order without allowing a broad allow rule to
shadow a specific safety denial.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import ntpath
import os
import posixpath
import re
from urllib.parse import urlsplit, urlunsplit
from dataclasses import dataclass, field
from typing import Any


VALID_RISKS = {"read", "write", "execute", "network", "secret"}
VALID_MODES = {"permissive", "safe", "locked"}
VALID_ACTIONS = {"allow", "ask", "deny"}

_PATH_KEYS = {"path", "file_path", "directory", "root", "cwd", "workdir"}
_URL_KEYS = {"url", "uri", "endpoint", "origin"}
_DOMAIN_KEYS = {"domain", "host", "hostname"}
_PAYLOAD_KEYS = {"content", "code", "patch", "data", "body", "input"}
_SECRET_MARKERS = ("secret", "token", "password", "api_key", "authorization")


def normalize_policy_resource(key: str, value: Any) -> str:
    """Return a stable, non-secret representation for policy comparisons."""
    key = str(key).lower()
    text = str(value).strip().replace("\r\n", "\n")
    if key in _PATH_KEYS:
        # Lexical normalization works for both host-native and foreign-looking
        # paths and, importantly, collapses traversal before glob matching.
        if re.match(r"^[a-zA-Z]:[\\/]", text) or "\\" in text:
            return ntpath.normcase(ntpath.normpath(text)).replace("\\", "/")
        return posixpath.normpath(text.replace("\\", "/"))
    if key in _URL_KEYS:
        parsed = urlsplit(text)
        if parsed.scheme and parsed.hostname:
            scheme = parsed.scheme.lower()
            host = parsed.hostname.lower().rstrip(".")
            port = parsed.port
            if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
                host = f"{host}:{port}"
            path = posixpath.normpath(parsed.path or "/")
            if parsed.path.endswith("/") and not path.endswith("/"):
                path += "/"
            return urlunsplit((scheme, host, path, parsed.query, ""))
        return text.lower()
    if key in _DOMAIN_KEYS:
        parsed = urlsplit(text if "://" in text else f"//{text}")
        return (parsed.hostname or text).lower().rstrip(".")
    if key == "command":
        return text
    return text


def policy_call_scope(name: str, args: dict[str, Any] | None) -> str:
    """Fingerprint an approved call without retaining prompt payloads/secrets."""
    resources: dict[str, str] = {}
    for raw_key, value in sorted((args or {}).items()):
        key = str(raw_key).lower()
        if key.startswith("__") or key in _PAYLOAD_KEYS or any(marker in key for marker in _SECRET_MARKERS):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            resources[key] = normalize_policy_resource(key, value)
    encoded = json.dumps(resources, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]
    return f"{name}:{digest}"


@dataclass(frozen=True)
class PolicyRule:
    """A pattern-based authorization rule.

    Matching:
    - tool_pattern: fnmatch glob against tool name (e.g. "execute_*", "git_*")
    - arg_matchers: dict of arg_name → glob pattern. ALL must match for the
      rule to apply. Supports common patterns:
        - path: "/safe/dir/*" matches paths under /safe/dir
        - url/domain: "*.example.com" matches subdomains
        - command: "git *" matches git commands
    - If arg_matchers is empty, the rule matches any call to the tool.

    Action:
    - "allow": auto-approve, skip mode-based check
    - "ask": require explicit user approval (returns allowed=False with reason)
    - "deny": block unconditionally
    """
    tool_pattern: str
    action: str
    arg_matchers: dict[str, str] = field(default_factory=dict)
    description: str = ""

    def __post_init__(self):
        if self.action not in VALID_ACTIONS:
            raise ValueError(
                f"Invalid action {self.action!r}. "
                f"Allowed actions: {', '.join(sorted(VALID_ACTIONS))}"
            )

    def matches(self, name: str, args: dict[str, Any] | None = None) -> bool:
        """Check if this rule matches the given tool call."""
        if not fnmatch.fnmatch(name, self.tool_pattern):
            return False
        if not self.arg_matchers:
            return True
        args = args or {}
        for key, pattern in self.arg_matchers.items():
            if key not in args:
                return False
            value = normalize_policy_resource(key, args.get(key, ""))
            normalized_pattern = normalize_policy_resource(key, pattern)
            if not fnmatch.fnmatch(value, normalized_pattern):
                return False
        return True


@dataclass
class ToolPolicy:
    mode: str = "permissive"
    allowed_tools: set[str] = field(default_factory=set)
    denied_tools: set[str] = field(default_factory=set)
    rules: list[PolicyRule] = field(default_factory=list)
    allowed_calls: set[str] = field(default_factory=set, repr=False)

    @classmethod
    def from_env(cls) -> "ToolPolicy":
        mode = os.getenv("AGENT_TOOL_POLICY", "permissive").strip().lower()
        if mode not in VALID_MODES:
            mode = "permissive"
        allowed = {item.strip() for item in os.getenv("AGENT_TOOL_ALLOW", "").split(",") if item.strip()}
        denied = {item.strip() for item in os.getenv("AGENT_TOOL_DENY", "").split(",") if item.strip()}
        return cls(mode=mode, allowed_tools=allowed, denied_tools=denied)

    # -- rule management ----------------------------------------------------

    def add_rule(self, rule: PolicyRule) -> None:
        """Append a rule. Deny wins; otherwise configured order matters."""
        self.rules.append(rule)

    def add_rule_shortcut(
        self,
        tool_pattern: str,
        action: str,
        *,
        path: str = "",
        url: str = "",
        command: str = "",
        description: str = "",
        **extra_matchers: str,
    ) -> None:
        """Convenience: add a rule with common argument matchers."""
        matchers: dict[str, str] = {}
        if path:
            matchers["path"] = path
        if url:
            matchers["url"] = url
        if command:
            matchers["command"] = command
        matchers.update(extra_matchers)
        self.rules.append(PolicyRule(
            tool_pattern=tool_pattern,
            action=action,
            arg_matchers=matchers,
            description=description,
        ))

    def clear_rules(self) -> None:
        self.rules.clear()

    # -- authorization ------------------------------------------------------

    def authorize(
        self,
        name: str,
        risk: str,
        approval: str = "never",
        args: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """Authorize a tool call.

        Returns (allowed, reason). Evaluation order:
        1. Pattern-based rules (deny wins, then first match)
        2. Session-scoped exact-call and per-tool deny/allow sets
        3. Approval gate (approval == "always")
        4. Mode-based risk gating
        """
        # 1. Pattern-based rules. A deny must not be shadowed by an earlier,
        # broader allow. This is the durable safety boundary for path/domain
        # rules assembled from multiple configuration layers.
        matching = [rule for rule in self.rules if rule.matches(name, args)]
        denied = next((rule for rule in matching if rule.action == "deny"), None)
        if denied is not None:
            return False, f"Denied by rule: {denied.description or denied.tool_pattern}"
        if matching:
            rule = matching[0]
            if rule.action == "allow":
                return True, f"rule: {rule.description or rule.tool_pattern}"
            # action == "ask"
            if name in self.denied_tools:
                return False, f"Tool '{name}' is denied for this process"
            if name in self.allowed_tools or policy_call_scope(name, args) in self.allowed_calls:
                return True, "session approval"
            return False, f"Approval required by rule: {rule.description or rule.tool_pattern}"

        # 2. Session-scoped sets
        if name in self.denied_tools:
            return False, f"Tool '{name}' is denied for this process"
        if name in self.allowed_tools or policy_call_scope(name, args) in self.allowed_calls:
            return True, "session approval"

        # 3. Approval gate
        if approval == "always":
            return False, f"Tool '{name}' requires approval"

        # 4. Mode-based risk gating
        if self.mode == "permissive":
            return True, "permissive policy"
        if self.mode == "locked":
            # Network tools in this runtime are read-only discovery/fetch
            # operations. Explicit deny/ask rules above still take precedence.
            allowed = risk in {"read", "network"}
        else:  # safe
            allowed = risk in {"read", "write", "network"}
        if allowed:
            return True, f"{self.mode} policy"
        return False, f"Tool '{name}' ({risk}) requires approval under {self.mode} policy"

    # -- session-scoped overrides -------------------------------------------

    def allow(self, name: str) -> None:
        self.denied_tools.discard(name)
        self.allowed_tools.add(name)

    def allow_call(self, name: str, args: dict[str, Any] | None = None) -> str:
        """Allow only this normalized resource/argument scope for the session."""
        scope = policy_call_scope(name, args)
        self.allowed_calls.add(scope)
        return scope

    def clear_allowed_calls(self, scopes: set[str] | None = None) -> None:
        if scopes is None:
            self.allowed_calls.clear()
        else:
            self.allowed_calls.difference_update(scopes)

    def deny(self, name: str) -> None:
        self.allowed_tools.discard(name)
        self.denied_tools.add(name)

    def set_mode(self, mode: str) -> None:
        normalized = mode.strip().lower()
        if normalized not in VALID_MODES:
            raise ValueError(f"Unknown policy mode '{mode}'. Use: {', '.join(sorted(VALID_MODES))}")
        self.mode = normalized
