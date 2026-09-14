"""Session goal management tool (Goal Mode).

A session goal is verified by an independent evidence-based checker after
every successfully completed turn; when unmet, the next round starts
automatically. Modeled after ZCode's Goal Mode.
"""

from __future__ import annotations

from typing import Any, Callable

from .registry import ToolDef, ToolRegistry


def register_goal_tools(
    registry: ToolRegistry,
    task_store: Any,
    session_id: Callable[[], str],
    on_goal_event: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    def _notify(goal: dict[str, Any] | None) -> None:
        if goal is not None and on_goal_event is not None:
            try:
                on_goal_event(goal)
            except Exception:
                pass

    def _goal(
        action: str,
        objective: str = "",
        criteria: str = "",
        max_rounds: int = 0,
        evidence: str = "",
    ) -> str:
        if task_store is None:
            return "Goal mode is unavailable: task persistence is disabled."
        sid = str(session_id() or "default")
        act = str(action or "show").strip().lower()

        if act == "set":
            try:
                goal = task_store.set_goal(sid, objective, criteria=criteria, max_rounds=max_rounds)
            except ValueError as exc:
                return f"Could not set goal: {exc}"
            _notify(goal)
            return (
                f"Goal set for this session (round 0/{goal['max_rounds']}): {goal['objective']}\n"
                + (f"Criteria: {goal['criteria']}\n" if goal.get("criteria") else "")
                + "Each completed turn is now checked by an independent verifier; unmet rounds "
                "continue automatically. Use pause/clear to stop the loop."
            )

        if act == "show":
            return task_store.format_goal_status(sid)

        if act == "history":
            pair = task_store.get_goal_history(sid)
            if pair is None:
                return "No live goal. Set one with the goal tool (action=set)."
            goal, history = pair
            if not history:
                return (
                    f"Goal [{goal['status']}] round {goal['round']}/{goal['max_rounds']} has "
                    f"no verification rounds yet: {goal['objective']}"
                )
            lines = [
                f"Goal [{goal['status']}] history — {len(history)} verification round(s): "
                f"{goal['objective']}"
            ]
            for entry in history:
                round_no = entry.get("round", "?")
                mark = "MET" if entry.get("met") else "not met"
                evidence = str(entry.get("evidence") or "(none)").strip()
                line = f"round {round_no}: {mark} — evidence: {evidence}"
                next_step = str(entry.get("next_step") or "").strip()
                if next_step:
                    line += f" | next: {next_step}"
                lines.append(line)
            return "\n".join(lines)

        if act == "complete":
            goal = task_store.active_goal_for_session(sid)
            if goal is None or goal.get("status") != "active":
                return "No active goal to mark complete."
            claim = " ".join(str(evidence or "").split())[:500]
            return (
                "Completion claimed for the independent verifier"
                + (f": {claim}" if claim else ".")
                + " The goal remains active until verification succeeds."
            )

        if act == "pause":
            goal = task_store.pause_goal(sid)
            if goal is None:
                return "No active goal to pause."
            _notify(goal)
            return f"Goal paused at round {goal['round']}/{goal['max_rounds']}. Completed rounds and files stay; resume to continue."

        if act == "resume":
            goal = task_store.resume_goal(sid)
            if goal is None:
                return "No paused goal to resume."
            _notify(goal)
            return f"Goal resumed at round {goal['round']}/{goal['max_rounds']}: {goal['objective']}"

        if act == "clear":
            goal = task_store.clear_goal(sid)
            if goal is None:
                return "No live goal to clear."
            _notify(goal)
            return f"Goal cleared: {goal['objective']}"

        return f"Unknown goal action: {act!r}. Use set, show, history, complete, pause, resume, or clear."

    registry.register(ToolDef(
        name="goal",
        description=(
            "Manage the session goal for long-horizon work (Goal Mode). While a goal is active, "
            "an independent verifier checks every completed turn against real evidence (command "
            "output, test results, confirmed file changes); unmet rounds auto-continue until the "
            "goal is met, paused, cleared, or the round budget runs out. Actions: set (objective "
            "required; optional criteria describing how to verify, optional max_rounds), show, "
            "history (list this goal's recorded verification rounds: round/met/evidence/next_step), "
            "complete (claim completion with evidence for independent verification), pause, resume, "
            "clear. For self-contained conversational or creative goals, the delivered answer is "
            "valid evidence; for external work, provide real outputs or confirmed changes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set", "show", "history", "complete", "pause", "resume", "clear"],
                    "description": "Goal operation to perform.",
                },
                "objective": {
                    "type": "string",
                    "description": "For action=set: one-sentence verifiable objective.",
                    "default": "",
                },
                "criteria": {
                    "type": "string",
                    "description": "For action=set: optional evidence criteria, e.g. 'pytest exits 0'.",
                    "default": "",
                },
                "max_rounds": {
                    "type": "integer",
                    "description": "For action=set: optional verification round budget (default from GOAL_MAX_ROUNDS, max 50).",
                    "default": 0,
                },
                "evidence": {
                    "type": "string",
                    "description": "For action=complete: concise evidence that the active goal is satisfied.",
                    "default": "",
                },
            },
            "required": ["action"],
        },
        fn=_goal,
        risk="write",
    ))
