"""Shared command behavior for terminal clients; affects subsequent turns."""

from agent.runtime.turn_budget import parse_turn_budget

USAGE = "Usage: /budget [seconds|off]"


def execute_budget_command(agent, argument: str) -> tuple[str, str]:
    if argument.strip():
        try:
            seconds = parse_turn_budget(argument.strip())
        except ValueError as exc:
            return "", f"{exc} {USAGE}"
        agent.turn_timeout_seconds = seconds
    seconds = getattr(agent, "turn_timeout_seconds", 0.0)
    if seconds <= 0:
        return f"Turn time budget: off. {USAGE}", ""
    return (
        f"Turn time budget: {seconds:g}s for each subsequent turn, including approval/question waits. "
        "Expiry stops new work and preserves progress for explicit continuation. /budget off disables it.",
        "",
    )
