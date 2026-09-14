"""Task-bound coordination for top-level structured user questions."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .tools.user_questions import (
    UserQuestionCancelled,
    UserQuestionUnavailable,
    normalize_answers,
)

MAX_REASON_CHARS = 500
_STALE_REASON = "Question request is no longer pending."


def _bounded_reason(value: object, *, fallback: str) -> str:
    text = "".join(
        char if char == "\t" or ord(char) >= 32 else " "
        for char in str(value or "")
    )
    return " ".join(text.split())[:MAX_REASON_CHARS].strip() or fallback


@dataclass
class _PendingQuestion:
    request_id: str
    questions: list[dict[str, Any]]
    future: asyncio.Future[dict[str, Any]]


class UserQuestionBroker:
    """Own the single structured question attached to the active model task."""

    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        available: Callable[[], bool],
    ) -> None:
        self._emit = emit
        self._available = available
        self._pending: _PendingQuestion | None = None

    @property
    def pending_count(self) -> int:
        return int(self._pending is not None)

    async def ask(self, questions: list[dict[str, Any]]) -> dict[str, Any]:
        if not self._available():
            raise UserQuestionUnavailable(
                "Human interaction is unavailable on this channel."
            )
        if self._pending is not None:
            raise UserQuestionUnavailable("Another question request is already pending.")

        pending = _PendingQuestion(
            request_id=uuid.uuid4().hex,
            questions=questions,
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending = pending
        emitted = False
        terminal_state = "cancelled"
        try:
            self._emit({
                "type": "user_question_request",
                "request_id": pending.request_id,
                "questions": pending.questions,
            })
            emitted = True
            answer = await pending.future
            terminal_state = "answered"
            return answer
        finally:
            if self._pending is pending:
                self._pending = None
            if emitted:
                self._emit({
                    "type": "user_question_resolved",
                    "request_id": pending.request_id,
                    "state": terminal_state,
                })

    def resolve(self, request_id: str, answers: object) -> tuple[bool, str, bool]:
        pending = self._matching_pending(request_id)
        if pending is None:
            return False, _STALE_REASON, False
        try:
            normalized = normalize_answers(pending.questions, answers)
        except (TypeError, ValueError) as exc:
            return (
                False,
                _bounded_reason(exc, fallback="Invalid question response."),
                True,
            )
        pending.future.set_result(normalized)
        return True, "", False

    def cancel(self, request_id: str, reason: str) -> tuple[bool, str]:
        pending = self._matching_pending(request_id)
        if pending is None:
            return False, _STALE_REASON
        message = _bounded_reason(reason, fallback="Question request was cancelled.")
        pending.future.set_exception(UserQuestionCancelled(message))
        return True, ""

    def close(self, reason: str) -> None:
        pending = self._pending
        if pending is not None:
            self.cancel(pending.request_id, reason)

    def _matching_pending(self, request_id: str) -> _PendingQuestion | None:
        pending = self._pending
        if (
            pending is None
            or pending.request_id != request_id
            or pending.future.done()
        ):
            return None
        return pending
