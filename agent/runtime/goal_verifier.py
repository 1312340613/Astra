"""Evidence-based verification for session goal mode.

After each successfully completed turn with a live session goal, a separate
verifier call decides whether the objective has been met. The verifier only
accepts evidence visible in the transcript — including the delivered answer
for self-contained conversational or creative goals — never plans or effort.
This mirrors ZCode's Goal Mode "verification after every round" design.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any


DEFAULT_MAX_MESSAGES = 14
DEFAULT_MAX_CHARS = 9000
_PER_MESSAGE_CHARS = 1200
_FALLBACK_NEXT_STEP = (
    "Verification output was unparsable. Re-check the goal criteria and produce "
    "concrete evidence: run the relevant command or test and show its real output."
)
_COMPLETION_CLAIM_INSTRUCTION = (
    "When the goal is fully satisfied, call the goal tool with action=complete "
    "and concise evidence, then give the final answer. This is a completion claim "
    "for the independent verifier, not a bypass. Do not invent extra work merely "
    "because the goal has no command or test criteria."
)


def goal_mode_enabled() -> bool:
    return os.getenv("GOAL_MODE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("output") or ""
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return ""


def _bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n…[truncated]\n"
    if limit <= len(marker):
        return marker[:max(0, limit)]
    available = limit - len(marker)
    head = available // 2
    return text[:head] + marker + text[-(available - head):]


def _call_text(call: dict[str, Any]) -> str:
    function = call.get("function") or call
    if not isinstance(function, dict):
        return ""
    args = function.get("arguments", "")
    if not isinstance(args, str):
        args = json.dumps(args, ensure_ascii=False)
    return _bounded(f"call_id={call.get('id', '?')} {function.get('name', '?')} {args}", 700)


def build_transcript(messages: list[dict[str, Any]], *,
                     max_messages: int = DEFAULT_MAX_MESSAGES,
                     max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Keep recent command identity plus output head/tail within a hard budget.

    A truncated transcript is incomplete evidence, never an implied success.
    Tool arguments can come from an earlier message outside the content window.
    """
    calls = {str(call.get("id")): _call_text(call)
             for message in messages if message.get("role") == "assistant"
             for call in (message.get("tool_calls") or []) if isinstance(call, dict)}
    entries: list[str] = []
    remaining = max(0, int(max_chars))
    for message in reversed(messages[-max(1, int(max_messages)):]):
        role = str(message.get("role") or "?")
        if role == "system":
            continue
        text = _bounded(_message_text(message).strip(), _PER_MESSAGE_CHARS)
        identity = ""
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            identity = calls.get(call_id, f"call_id={call_id or '?'}")
            if isinstance(message.get("execution"), dict):
                identity += "\nExecution: " + _bounded(json.dumps(message["execution"]), 300)
        elif role == "assistant":
            identity = "\n".join(_call_text(call) for call in (message.get("tool_calls") or [])
                                   if isinstance(call, dict))
            identity = _bounded(identity, 1000)
        if not text and not identity:
            continue
        entry = f"[{role}] " + (identity + "\n" if identity else "") + text
        if entries and len(entry) > remaining:
            break
        entry = _bounded(entry, remaining)
        if not entry:
            break
        entries.append(entry)
        remaining -= len(entry) + 1
    return "\n".join(reversed(entries))


def parse_verdict(raw: str) -> dict[str, Any]:
    """Parse verifier JSON conservatively.

    Any parse failure yields met=False so a broken verifier response can
    never end the goal loop by declaring success.
    """
    text = str(raw or "").strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    parsed: Any = None
    if match:
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            parsed = None
    if not isinstance(parsed, dict):
        return {
            "met": False,
            "evidence": "",
            "next_step": _FALLBACK_NEXT_STEP,
            "parse_error": True,
        }
    met = parsed.get("met")
    if isinstance(met, str):
        normalized = met.strip().lower()
        if normalized in {"true", "yes", "met", "1"}:
            met = True
        elif normalized in {"false", "no", "not_met", "not met", "0", "nope"}:
            met = False
        else:
            met = None
    elif type(met) is not bool:
        met = None
    if met is None:
        return {
            "met": False,
            "evidence": "",
            "next_step": _FALLBACK_NEXT_STEP,
            "parse_error": True,
        }
    raw_evidence = parsed.get("evidence")
    evidence = " ".join(raw_evidence.split())[:500] if isinstance(raw_evidence, str) else ""
    if met and not evidence:
        return {"met": False, "evidence": "", "next_step": _FALLBACK_NEXT_STEP,
                "parse_error": True}
    next_step = " ".join(str(parsed.get("next_step") or "").split())[:500]
    if not bool(met) and not next_step:
        next_step = (
            "State exactly which criterion is still unmet, then run the command "
            "or test that produces the missing evidence."
        )
    return {"met": met, "evidence": evidence, "next_step": next_step, "parse_error": False}


def verdict_is_usable(verdict: dict[str, Any]) -> bool:
    """Return whether a verifier result may advance the goal state machine."""
    if not isinstance(verdict, dict) or verdict.get("error") or verdict.get("parse_error"):
        return False
    if type(verdict.get("met")) is not bool:
        return False
    evidence = verdict.get("evidence")
    return not verdict["met"] or (isinstance(evidence, str) and bool(evidence.strip()))


def verification_is_current(
    *,
    captured_session: str,
    captured_generation: int,
    current_session: str,
    current_generation: int,
) -> bool:
    """Reject verifier results made stale by a session switch or newer turn."""
    return (
        bool(captured_session)
        and captured_session == current_session
        and captured_generation > 0
        and captured_generation == current_generation
    )


def should_verify_goal_turn(
    *,
    turn_completed: bool,
    was_cancelled: bool,
    failed_message: str,
) -> bool:
    """Only successful completed turns may spend a goal verification round."""
    return turn_completed and not was_cancelled and not str(failed_message or "").strip()


def decide_goal_continuation(goal: dict[str, Any], verdict: dict[str, Any]) -> str | None:
    """Return the synthetic continuation message, or None when the loop stops.

    Expects the goal dict AFTER record_goal_round, so completed/exhausted
    goals already carry their terminal status.
    """
    if not verdict_is_usable(verdict):
        return None
    if not isinstance(goal, dict) or goal.get("status") != "active":
        return None
    if bool(verdict.get("met")):
        return None
    objective = str(goal.get("objective") or "").strip()
    if not objective:
        return None
    lines = [
        f"[goal round {goal.get('round', 0)}/{goal.get('max_rounds', 0)} — auto-continue]",
        f"Session goal: {objective}",
    ]
    criteria = str(goal.get("criteria") or "").strip()
    if criteria:
        lines.append(f"Criteria: {criteria}")
    lines.append("Verifier verdict: NOT MET.")
    evidence = str(verdict.get("evidence") or "").strip()
    lines.append(f"Evidence so far: {evidence or '(none recorded)'}")
    next_step = str(verdict.get("next_step") or "").strip()
    if next_step:
        lines.append(f"Verifier next step: {next_step}")
    lines.append(
        "Continue executing toward the goal now. Take concrete, verifiable actions "
        "(run the checks, show real outputs). Do not restart the plan from scratch "
        "and do not ask for confirmation unless you are truly blocked."
    )
    lines.append(_COMPLETION_CLAIM_INSTRUCTION)
    return "\n".join(lines)


def build_goal_resume_message(goal: dict[str, Any]) -> str:
    """Build the synthetic user message that explicitly resumes persisted work."""
    objective = str(goal.get("objective") or "").strip()
    if not objective:
        raise ValueError("Goal objective must not be empty")
    lines = [
        f"[goal round {goal.get('round', 0)}/{goal.get('max_rounds', 0)} — resume]",
        f"Session goal: {objective}",
    ]
    criteria = str(goal.get("criteria") or "").strip()
    if criteria:
        lines.append(f"Criteria: {criteria}")
    verdict = goal.get("last_verdict") or {}
    evidence = str(verdict.get("evidence") or "").strip()
    if evidence:
        lines.append(f"Evidence so far: {evidence}")
    next_step = str(verdict.get("next_step") or "").strip()
    if next_step:
        lines.append(f"Verifier next step: {next_step}")
    lines.append(
        "Resume executing toward the goal now. Preserve completed work and take the next "
        "concrete, verifiable action. Do not restart the plan from scratch and do not ask "
        "for confirmation unless you are truly blocked."
    )
    lines.append(_COMPLETION_CLAIM_INSTRUCTION)
    return "\n".join(lines)


def build_goal_start_message(goal: dict[str, Any]) -> str:
    """Build the first synthetic work message for a newly created goal."""
    objective = str(goal.get("objective") or "").strip()
    if not objective:
        raise ValueError("Goal objective must not be empty")
    lines = [
        f"[goal round 0/{goal.get('max_rounds', 0)} — start]",
        f"Session goal: {objective}",
    ]
    criteria = str(goal.get("criteria") or "").strip()
    if criteria:
        lines.append(f"Criteria: {criteria}")
    lines.append(
        "Start working toward the goal now. Take concrete, verifiable actions "
        "(run the checks, show real outputs). Do not ask for confirmation unless truly blocked."
    )
    lines.append(_COMPLETION_CLAIM_INSTRUCTION)
    return "\n".join(lines)


_VERIFIER_SYSTEM_PROMPT = (
    "You are a strict completion verifier for a session goal. Decide ONLY from "
    "evidence present in the conversation transcript. For externally checkable work, "
    "require command outputs, test results, confirmed file changes, or fetched data. "
    "For self-contained conversational, explanatory, transformative, or creative goals "
    "with no external acceptance criteria, the assistant's actually delivered answer "
    "or artifact is itself direct evidence and may satisfy the goal in one round. "
    "A goal-tool completion claim is a signal to evaluate, never sufficient evidence by "
    "itself. Plans, checklists, elapsed effort, intentions, or confident claims do not count. "
    "Return JSON only with shape {\"met\": boolean, \"evidence\": string, \"next_step\": string}. "
    "Set met=true ONLY when the transcript contains direct evidence that the objective "
    "AND its criteria are fully satisfied; quote the decisive evidence. "
    "Check command identity, execution receipts and output tails; tool RPC success "
    "alone is not a passing test. Truncated or missing results are incomplete evidence. "
    "If evidence is partial, ambiguous, or absent, set met=false and make next_step one "
    "concrete actionable step. Never continue solely to manufacture a command, test, or "
    "metric that the objective did not require. "
    "Keep evidence and next_step under 300 characters each."
)


class GoalVerifier:
    """One LLM call per completed turn while a session goal is active."""

    def __init__(self, llm: Any):
        self.llm = llm

    async def verify(self, goal: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
        transcript = build_transcript(messages)
        if not transcript.strip():
            return {
                "met": False,
                "evidence": "",
                "next_step": "No work is visible in the transcript yet; start executing the goal.",
                "verifier": "goal-verifier",
            }
        criteria = str(goal.get("criteria") or "").strip() or "(none stated)"
        prompt = [
            {"role": "system", "content": _VERIFIER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Objective: {goal.get('objective', '')}\n"
                    f"Criteria: {criteria}\n"
                    f"Rounds already spent: {goal.get('round', 0)}\n\n"
                    f"Conversation transcript (most recent last):\n{transcript}"
                ),
            },
        ]
        try:
            limited_chat = getattr(self.llm, "chat_limited", None)
            if limited_chat is not None:
                response = await limited_chat(
                    prompt,
                    max_tokens=512,
                    temperature=0.1,
                    disable_thinking=os.getenv("GOAL_VERIFY_DISABLE_THINKING", "1").lower()
                    in {"1", "true", "yes", "on"},
                )
            else:
                response = await self.llm.chat(prompt)
            raw = str(response.get("content", "") if isinstance(response, dict) else response)
        except Exception as exc:  # A verifier outage must never block chat.
            return {
                "met": False,
                "evidence": "",
                "next_step": f"Verifier call failed ({type(exc).__name__}); continue the goal work and produce evidence.",
                "verifier": "goal-verifier",
                "error": str(exc)[:300],
            }
        verdict = parse_verdict(raw)
        verdict["verifier"] = "goal-verifier"
        return verdict
