"""Shared /learn command handling."""

import asyncio
from collections.abc import Callable
from typing import Any

from agent.runtime.learning import (
    LearningProviderError,
    LearningReviewer,
    LearningStore,
    format_review_outcome,
)
from agent.runtime.memory import MemoryStore
from agent.runtime.skills import SkillStore
from agent.runtime.async_io import durable_io
from agent.runtime.learning_queue import candidate_readiness, format_learning_queue

LEARN_USAGE = (
    "Usage:\n"
    "  /learn\n"
    "  /learn summarize   # summarize new evidence and save unverified candidates\n"
    "  /learn review      # alias of summarize\n"
    "  /learn pending\n"
    "  /learn show <id>\n"
    "  /learn repair <id>    # ask the active model to repair and apply one proposal\n"
    "  /learn repair all     # repair all pending proposals one by one\n"
    "  /learn approve <id>\n"
    "  /learn approve all   # approve every pending proposal that can be applied\n"
    "  /learn reject <id>\n"
    "  /learn reject all    # reject every pending proposal\n"
    "  /learn rollback <id>\n"
    "  /learn mode <off|review>"
)


async def execute_learning_command(
    store: LearningStore,
    reviewer: LearningReviewer,
    memory: MemoryStore,
    skills: SkillStore,
    args: list[str],
    *,
    session_id: str,
    messages: list[dict[str, Any]],
    on_progress: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    action = args[0].lower() if args else "status"
    try:
        if action == "status":
            queue = await durable_io(format_learning_queue, reviewer.lifecycle, details=False)
            return (
                f"Learning Review: {store.mode()}\n"
                "Policy: model and background review save isolated candidates; runtime-checked, scoped validation is required for automatic activation. "
                "Core Markdown, manual/pinned skills and destructive changes retain explicit user maintenance. "
                "The model can search, revise, trial, activate or discard through native learning tools; /learn approve explicitly overrides the quality gate.\n"
                f"{queue}\n"
                f"Store: {store.path}",
                "",
            )
        if action in {"pending", "list"}:
            return await durable_io(format_learning_queue, reviewer.lifecycle), ""
        if action in {"review", "summarize", "remember"}:
            outcome = await reviewer.review_outcome(messages, session_id, manual=True)
            return format_review_outcome(outcome), ""
        if action == "show" and len(args) >= 2:
            state = await durable_io(candidate_readiness, reviewer.lifecycle, args[1])
            return (store.format_detail(args[1], skills) + "\nQueue: " + state["state"]
                    + "\nReason: " + state["reason"] + "\nNext: " + state["next_action"]), ""
        if (
            action in {"repair-all", "repair_all"}
            or (action == "repair" and len(args) >= 2 and args[1].lower() == "all")
        ):
            pending = store.list("pending", limit=10_000)
            if not pending:
                return "No pending learning proposals.", ""
            repaired: list[tuple[str, str]] = []
            failed: list[tuple[str, str]] = []
            if on_progress is not None:
                on_progress(
                    f"I found {len(pending)} pending learning proposal(s). "
                    "I’ll repair them one by one while preserving their original intent."
                )
            for index, proposal in enumerate(pending, start=1):
                if on_progress is not None:
                    on_progress(
                        f"\n\nRepairing `{proposal['id']}` ({index}/{len(pending)})…"
                    )
                try:
                    outcome = await reviewer.repair(proposal["id"])
                    if on_progress is not None and outcome.get("message"):
                        on_progress(f"\n\n{outcome['message']}")
                    repaired.append(
                        (proposal["id"], outcome["replacement"]["id"])
                    )
                except asyncio.CancelledError:
                    raise
                except LearningProviderError as exc:
                    failed.append((proposal["id"], str(exc)))
                except Exception as exc:  # noqa: BLE001 - preserve local repair diagnostics
                    failed.append((proposal["id"], str(exc)))
            lines = [
                f"Bulk repair complete: {len(repaired)} applied, {len(failed)} failed.",
            ]
            lines.extend(f"Repaired: {old_id} -> {new_id}" for old_id, new_id in repaired)
            lines.extend(f"Failed: {proposal_id} — {error}" for proposal_id, error in failed)
            lines.append(f"Remaining pending: {len(store.list('pending', limit=10_000))}")
            return "\n".join(lines), ""
        if action == "repair" and len(args) >= 2:
            if on_progress is not None:
                on_progress(
                    f"I’m repairing learning proposal `{args[1]}` with the active model. "
                    "I’ll preserve its intent and only change what is needed to satisfy the current rules."
                )
            outcome = await reviewer.repair(args[1])
            if on_progress is not None and outcome.get("message"):
                on_progress(f"\n\n{outcome['message']}")
            original = outcome["original"]
            replacement = outcome["replacement"]
            return (
                f"Repaired learning proposal {original['id']} -> "
                f"{replacement['id']} ({replacement['kind']}, applied).",
                "",
            )
        if (
            action in {"approve-all", "approve_all"}
            or (action == "approve" and len(args) >= 2 and args[1].lower() == "all")
        ):
            pending = store.list("pending", limit=10_000)
            if not pending:
                return "No pending learning proposals.", ""
            applied: list[str] = []
            failed: list[tuple[str, str]] = []
            for proposal in pending:
                try:
                    item = store.approve(proposal["id"], memory, skills)
                    applied.append(item["id"])
                except (OSError, UnicodeError, ValueError) as exc:
                    failed.append((proposal["id"], str(exc)))
            lines = [
                f"Bulk approval complete: {len(applied)} applied, {len(failed)} failed.",
            ]
            if applied:
                lines.append("Applied: " + ", ".join(applied))
            lines.extend(f"Failed: {proposal_id} — {error}" for proposal_id, error in failed)
            lines.append(f"Remaining pending: {len(store.list('pending', limit=10_000))}")
            return "\n".join(lines), ""
        if action == "approve" and len(args) >= 2:
            item = store.approve(args[1], memory, skills)
            return f"Applied learning proposal {item['id']} ({item['kind']}).", ""
        if (
            action in {"reject-all", "reject_all"}
            or (action == "reject" and len(args) >= 2 and args[1].lower() == "all")
        ):
            pending = store.list("pending", limit=10_000)
            if not pending:
                return "No pending learning proposals.", ""
            rejected: list[str] = []
            failed: list[tuple[str, str]] = []
            for proposal in pending:
                try:
                    item = store.reject(proposal["id"])
                    rejected.append(item["id"])
                except (OSError, UnicodeError, ValueError) as exc:
                    failed.append((proposal["id"], str(exc)))
            lines = [
                f"Bulk rejection complete: {len(rejected)} rejected, {len(failed)} failed.",
            ]
            if rejected:
                lines.append("Rejected: " + ", ".join(rejected))
            lines.extend(f"Failed: {proposal_id} — {error}" for proposal_id, error in failed)
            lines.append(f"Remaining pending: {len(store.list('pending', limit=10_000))}")
            return "\n".join(lines), ""
        if action == "reject" and len(args) >= 2:
            item = store.reject(args[1])
            return f"Rejected learning proposal {item['id']}.", ""
        if action == "rollback" and len(args) >= 2:
            item = store.rollback(args[1], memory, skills)
            return f"Rolled back learning proposal {item['id']}.", ""
        if action == "mode" and len(args) >= 2:
            return f"Learning Review mode: {store.set_mode(args[1])}", ""
        return "", LEARN_USAGE
    except asyncio.CancelledError:
        raise
    except LearningProviderError as exc:
        return "", str(exc)
    except Exception as exc:  # noqa: BLE001 - command boundary returns local diagnostics
        return "", str(exc)
