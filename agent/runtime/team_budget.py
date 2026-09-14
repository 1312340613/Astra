"""Runtime-owned turn notices and episode accounting for Team members."""

from __future__ import annotations

import asyncio
from typing import Any

from .agent_team import AgentTeamRuntime, TeamDelivery
from .worker import WorkerSpec


class TeamBudgetTracker:
    def __init__(self, runtime: AgentTeamRuntime | None, spec: WorkerSpec, *, model: str):
        self.runtime = runtime
        self.spec = spec
        self.model = model
        self.episode: dict[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        return self.runtime is not None and bool(self.spec.team_agent_id)

    async def ensure(self, turns: int, *, kind: str = "coordination") -> None:
        if self.runtime is not None and self.enabled and self.episode is None:
            await self._start(turns, kind=kind)

    async def _start(self, turns: int, **metadata: Any) -> None:
        assert self.runtime is not None
        pending = asyncio.create_task(asyncio.to_thread(
            self.runtime.store.start_episode, self.spec.team_agent_id,
            start_turn=turns, max_turns=self.spec.max_turns, model=self.model, **metadata,
        ))
        try:
            self.episode = await asyncio.shield(pending)
        except asyncio.CancelledError:
            # SQLite work in a thread cannot be cancelled. Keep its row handle so
            # the caller's finally block can close it instead of orphaning it.
            self.episode = await pending
            raise

    async def assign(self, delivery: TeamDelivery, turns: int) -> None:
        assert self.runtime is not None
        if delivery.kind != "task_assignment":
            await self.ensure(turns)
            return
        await self.finish(turns, outcome="superseded")
        await self._start(
            turns,
            kind="assignment", message_id=delivery.message_id, assignment=delivery.assignment,
        )

    async def progress(self, turns: int) -> None:
        if self.episode is not None:
            assert self.runtime is not None
            await asyncio.to_thread(
                self.runtime.store.update_episode, self.episode["id"], end_turn=turns,
            )

    async def finish(self, turns: int, *, outcome: str) -> None:
        if self.episode is None:
            return
        assert self.runtime is not None
        await asyncio.to_thread(
            self.runtime.store.update_episode, self.episode["id"], end_turn=turns, outcome=outcome,
        )
        self.runtime.emit(
            "team_episode_finished", team_id=self.spec.team_id, agent_id=self.spec.team_agent_id,
            episode_id=self.episode["id"], team_task_id=self.episode.get("team_task_id"),
            turns_used=max(0, turns - self.episode["start_turn"]), outcome=outcome,
        )
        self.episode = None

    def notice(self, turns: int) -> dict[str, str]:
        """Build one transient notice; callers must not append it to retained history."""
        episode = self.episode or {}
        remaining = max(0, self.spec.max_turns - turns)
        used = max(0, turns - episode.get("start_turn", turns))
        estimate = episode.get("episode_estimate")
        text = (
            "[RUNTIME TURN BUDGET]\n"
            f"max_turns={self.spec.max_turns}; turns_used={turns}; turns_remaining={remaining}; "
            "reserved_final_report_turns=1.\n"
            f"episode_turns_used={used}; episode_estimate={estimate if estimate is not None else 'unspecified'}.\n"
            "These are cumulative model-response counts across assignments, not tool-call counts. "
            "An episode estimate is a soft target, not a new allowance. Reserve time and turns "
            "to report evidence, unverified work and remaining risks; do not assume renewal."
        )
        if remaining <= max(3, self.spec.max_turns // 10) or (estimate is not None and used >= estimate - 1):
            text += "\nReport evidence and remaining work now; avoid starting a new broad investigation."
        return {"role": "user", "content": text}
