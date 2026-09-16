"""Small, deterministic evidence contracts for native learning trials.

Assertions check observations, not semantic entailment or arbitrary causality.
They are declared before tools execute; replay and retrieval cannot manufacture
new corroboration. This module never executes a command or calls a model.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .context_compressor import _is_synthetic_user_turn

MAX_CHECKS = 4
MAX_TRIALS_PER_REQUEST = 2
_NON_EVIDENCE = frozenset({
    "context_inspect", "context_open", "session_search", "session_read",
    "session_recall", "memory", "memory_search", "memory_fetch", "activity_search",
    "activity_read", "skills_list", "skill_view", "skill_manage", "learning",
    "learning_search", "project_verifier_init", "conclave", "delegate", "skill_review_snapshot", "skill_review_apply",
    "run_code", "process_read",  # Inner tools / process_poll provide authoritative outcomes.
})


def fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def is_evidence_tool(name: str) -> bool:
    return bool(name) and name not in _NON_EVIDENCE and not name.startswith(
        ("learning_", "session_", "activity_", "context_", "memory_")
    )


def evidence_messages(messages: list[dict]) -> list[dict]:
    """Keep original user/tool sources; never count retrieved text as new proof."""
    names: dict[str, str] = {}
    result = []
    for message in messages:
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                names[str(call.get("id", ""))] = str((call.get("function") or {}).get("name", ""))
        role = message.get("role")
        if role == "user" and not _is_synthetic_user_turn(message):
            result.append(message)
        elif role == "tool":
            name = str(message.get("name") or names.get(str(message.get("tool_call_id", "")), ""))
            # Legacy transcripts have no tool name; retain them as candidate
            # provenance only. They can never satisfy a live trial assertion.
            if not name or is_evidence_tool(name):
                result.append(message)
    return result


def source_fingerprint(message: dict) -> str:
    # Omit session/call IDs: replaying the same source with a new wrapper is
    # still the same evidence, not another independent confirmation.
    return fingerprint({"role": message.get("role"), "content": message.get("content")})


def input_fingerprint(checks: list[dict]) -> str:
    return fingerprint([{key: check[key] for key in ("tool", "args")} for check in checks])


def source_inputs(messages: list[dict]) -> list[str]:
    inputs = []
    for message in messages:
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            try:
                args = function.get("arguments") or {}
                if isinstance(args, str):
                    args = json.loads(args)
                if is_evidence_tool(str(function.get("name") or "")) and isinstance(args, dict):
                    inputs.append(input_fingerprint([{"tool": function["name"], "args": args}]))
            except (TypeError, ValueError):
                continue
    return list(dict.fromkeys(inputs))[-128:]


def normalize_checks(checks: Any) -> list[dict]:
    if not isinstance(checks, list) or not 1 <= len(checks) <= MAX_CHECKS:
        raise ValueError(f"Declare 1-{MAX_CHECKS} checks before starting a trial")
    normalized = []
    for check in checks:
        if not isinstance(check, dict) or set(check) - {"tool", "args", "contains", "json_path", "equals"}:
            raise ValueError("A check needs tool, exact args, and contains or json_path/equals")
        name, args = check.get("tool"), check.get("args")
        if not isinstance(name, str) or not is_evidence_tool(name) or not isinstance(args, dict) or not args:
            raise ValueError("Checks need a non-retrieval native tool and nonempty exact arguments")
        if any(str(key).startswith("_") for key in args):
            raise ValueError("Checks cannot set hidden tool arguments")
        has_text = "contains" in check
        has_json = "json_path" in check and "equals" in check
        if has_text == has_json:
            raise ValueError("Use exactly one assertion: contains or json_path with equals")
        if has_text and (not isinstance(check["contains"], str) or not 3 <= len(check["contains"]) <= 1000):
            raise ValueError("contains must be a specific 3-1000 character observation")
        if has_json:
            path = check["json_path"]
            if not isinstance(path, list) or not path or len(path) > 8 or any(
                not isinstance(part, (str, int)) or isinstance(part, bool) for part in path
            ):
                raise ValueError("json_path must contain 1-8 object keys or array indexes")
        if len(json.dumps(check, ensure_ascii=False)) > 8000:
            raise ValueError("A verification check is too large")
        normalized.append(dict(check))
    if len({fingerprint(check) for check in normalized}) != len(normalized):
        raise ValueError("Duplicate checks do not add evidence")
    return normalized


def check_result(check: dict, result: dict) -> dict:
    """Build a content-minimal receipt from an authoritative tool completion."""
    output = result.get("output", "")
    if not isinstance(output, str):
        output = json.dumps(output, ensure_ascii=False)
    unavailable = bool(
        result.get("error") or result.get("cached") or result.get("persistent_cache")
        or result.get("verified") is False or result.get("recovery_blocked")
    )
    matched = False
    observed: Any = None
    try:
        parsed = json.loads(output)
    except (TypeError, ValueError):
        parsed = None
    # A shell process that merely started or exited nonzero is not a passed
    # task. Native process tools include these fields in their JSON output.
    if isinstance(parsed, dict):
        unavailable = unavailable or bool(parsed.get("error")) or (
            parsed.get("exit_code") is not None and parsed["exit_code"] != 0
        ) or bool(parsed.get("running")) or parsed.get("status") in {"running", "queued", "failed", "cancelled"}
        # Background process-start responses have no completed exit status.
        if parsed.get("process_id") and parsed.get("exit_code") is None:
            unavailable = True
    # The persistent Bash contract renders exit codes as text even when the
    # tool itself completed normally. Do not mistake its stdout for success.
    if re.search(r"\[(?:exit code:|Exit code:)\s*-?[1-9]\d*\]", output):
        unavailable = True
    if not unavailable:
        if "contains" in check:
            matched = check["contains"] in output
            observed = check["contains"] if matched else None
        else:
            value: Any = parsed
            try:
                for part in check["json_path"]:
                    value = value[part]
                # JSON true is not the integer 1.
                matched = fingerprint(value) == fingerprint(check["equals"])
                observed = value
            except (KeyError, IndexError, TypeError):
                pass
    return {
        "status": "inconclusive" if unavailable else "passed" if matched else "failed",
        "result_fingerprint": fingerprint(output),
        "assertion_fingerprint": fingerprint(check),
        "observed_fingerprint": fingerprint(observed),
        "duration_ms": max(0, float(result.get("duration_ms") or 0)),
        # Only a successful literal assertion is retained, never the whole
        # output, exception, prompt or unrelated private tool data.
        "supports_literal": check.get("contains") if matched and "contains" in check else None,
    }


def trial_status(receipts: list[dict], checks: list[dict]) -> str:
    if any(item["status"] == "failed" for item in receipts):
        return "failed"
    if len(receipts) == len(checks) and all(item["status"] == "passed" for item in receipts):
        return "passed"
    return "inconclusive"
