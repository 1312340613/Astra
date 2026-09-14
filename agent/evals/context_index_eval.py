"""Model-free, de-identified shadow replay for the Context Index.

This module deliberately observes recommendation exposure rather than answer
quality.  A separate evaluator may supply a comparison answer, but this module
never calls a model, never opens evidence, and never infers relevance from an
open action.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import tempfile
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from agent.runtime.context_index import WorkspaceIdentity, create_context_index_broker
from agent.runtime.context_index.models import ContextIndexPack, ContextIndexTrace
from agent.runtime.context_index.session_source import content_fingerprint

_ANSWER_VARIANTS = frozenset({"off", "index_no_open", "index_with_open"})
_CASE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z")
_LABEL_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SOURCE_NAMES = frozenset({"session", "activity", "habit", "memory"})
_SOURCE_STATUSES = frozenset({"available", "absent", "error", "timeout"})
_POSIX_PATH_RE = re.compile(r"(?<![\w:/])/(?!/)(?:[^\s/,:;()\[\]{}]+/)*[^\s/,:;()\[\]{}]+")
_WINDOWS_PATH_RE = re.compile(r"(?<![\w])[A-Za-z]:[\\/](?:[^\\/\s,:;()\[\]{}]+[\\/])*[^\\/\s,:;()\[\]{}]+")
_FILE_URL_RE = re.compile(r"(?i)file://")
_UNC_PATH_RE = re.compile(r"\\\\")
_ENCODED_PATH_RE = re.compile(r"(?i)%(?:2f|5c)")
_SECRET_RE = re.compile(r"(?i)(?:token|secret|api[_-]?key|bearer)\s*[:=]")


@dataclass(frozen=True)
class ShadowCase:
    """One de-identified comparison turn.

    ``relevance_labels`` are input for an external scorer only; they are not
    exported and cannot cause the harness to record an evidence open.
    """

    case_id: str
    user_text: str = field(repr=False)
    workspace_label: str
    workspace_key: str = field(repr=False)
    local_time: datetime
    answer_variant: str = "index_no_open"
    answer: str = ""
    correction_next_turn: bool = False
    relevance_labels: dict[str, str] = field(default_factory=dict, repr=False)
    session_id: str = field(default="", repr=False)
    message_id: int | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ShadowResult:
    """Sanitized record suitable for a de-identified JSONL comparison set."""

    case_id: str
    mode: str
    workspace_label: str
    displayed: tuple[dict[str, str], ...]
    opened: tuple[str, ...]
    source_status: dict[str, str]
    latency_ms: float
    rendered_chars: int
    correction_next_turn: bool
    answer_variant: str
    answer: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "mode": self.mode,
            "workspace_label": self.workspace_label,
            "displayed": list(self.displayed),
            "opened": list(self.opened),
            "source_status": dict(self.source_status),
            "latency_ms": self.latency_ms,
            "rendered_chars": self.rendered_chars,
            "correction_next_turn": self.correction_next_turn,
            "answer_variant": self.answer_variant,
            "answer": self.answer,
        }


@dataclass
class _ShadowPreferences:
    mode: str = "shadow"
    char_budget: int = 900


class _Broker(Protocol):
    last_trace: ContextIndexTrace | None

    async def build(
        self,
        user_text: str,
        request_id: str,
        session_id: str,
        workspace: WorkspaceIdentity,
        now: datetime,
        active_fingerprints: frozenset[str],
    ) -> ContextIndexPack: ...


def _safe_label(value: object) -> str:
    return _LABEL_RE.sub("-", str(value).strip()).strip("-")[:80] or "workspace"


def _safe_answer(value: object) -> str:
    """Accept only plain de-identified evaluator answers.

    External answers are optional comparison material, never evidence. A
    suspicious answer is discarded as a whole rather than attempting partial
    redaction that could leave an encoded locator or secret behind.
    """

    text = " ".join(str(value or "").split())[:4_000]
    if (
        _FILE_URL_RE.search(text)
        or _POSIX_PATH_RE.search(text)
        or _WINDOWS_PATH_RE.search(text)
        or _UNC_PATH_RE.search(text)
        or _ENCODED_PATH_RE.search(text)
        or _SECRET_RE.search(text)
        or any(unicodedata.category(character).startswith("C") for character in text)
    ):
        return ""
    return text


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _case_from_record(raw: object) -> ShadowCase | None:
    if not isinstance(raw, dict):
        return None
    case_id = raw.get("case_id")
    user_text = raw.get("user_text")
    workspace_label = raw.get("workspace_label")
    workspace_key = raw.get("workspace_key", "")
    answer_variant = raw.get("answer_variant", "index_no_open")
    local_time = _parse_time(raw.get("local_time"))
    if (
        not isinstance(case_id, str)
        or not _CASE_ID_RE.fullmatch(case_id)
        or not isinstance(user_text, str)
        or not isinstance(workspace_label, str)
        or not isinstance(workspace_key, str)
        or not isinstance(answer_variant, str)
        or answer_variant not in _ANSWER_VARIANTS
        or local_time is None
    ):
        return None
    answer = raw.get("answer", "")
    labels = raw.get("relevance_labels", {})
    correction = raw.get("correction_next_turn", False)
    if not isinstance(answer, str) or not isinstance(labels, dict) or not isinstance(correction, bool):
        return None
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()):
        return None
    return ShadowCase(
        case_id=case_id,
        user_text=user_text,
        workspace_label=_safe_label(workspace_label),
        workspace_key=workspace_key,
        local_time=local_time,
        answer_variant=answer_variant,
        answer=answer,
        correction_next_turn=correction,
        relevance_labels=dict(labels),
        session_id=str(raw.get("session_id", ""))[:240],
        message_id=raw.get("message_id") if isinstance(raw.get("message_id"), int) and not isinstance(raw.get("message_id"), bool) else None,
    )


def load_shadow_cases(path: Path) -> tuple[ShadowCase, ...]:
    """Read valid JSONL cases only, then return a stable de-duplicated order."""

    cases: dict[str, ShadowCase] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return ()
    for line in lines:
        try:
            raw = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        case = _case_from_record(raw)
        if case is not None and case.case_id not in cases:
            cases[case.case_id] = case
    return tuple(cases[key] for key in sorted(cases))


def _safe_mode(value: object) -> str:
    text = str(value)
    return text if text in {"off", "session", "all", "shadow"} else "shadow"


def _safe_displayed(trace: ContextIndexTrace | None) -> tuple[dict[str, str], ...]:
    if trace is None:
        return ()
    rows: list[dict[str, str]] = []
    for row in trace.displayed:
        if row.source in _SOURCE_NAMES and row.slot in {"S1", "S2", "S3", "A1", "A2", "A3", "R1", "R2", "R3", "R4", "R5", "R6"}:
            rows.append({"source": row.source, "slot": row.slot})
    return tuple(rows)


def _safe_opened(trace: ContextIndexTrace | None, variant: str) -> tuple[str, ...]:
    """Return opened displayed-row source categories, never handles.

    Broker traces record ``(handle, status)`` rather than ``(handle, source)``.
    Resolve only successful statuses against the already metadata-only displayed
    rows, so exports cannot turn a private handle into a source assertion.
    """

    if trace is None or variant != "index_with_open":
        return ()
    displayed_by_handle = {
        row.handle: row.source
        for row in trace.displayed
        if row.source in _SOURCE_NAMES
    }
    return tuple(
        source
        for handle, status in trace.opened[:6]
        if status == "opened"
        and (source := displayed_by_handle.get(handle)) in _SOURCE_NAMES
    )


def _safe_status(trace: ContextIndexTrace | None) -> dict[str, str]:
    if trace is None:
        return {}
    return {
        source: status
        for source, status in sorted(trace.source_status.items())
        if source in _SOURCE_NAMES and status in _SOURCE_STATUSES
    }


def _finite_latency(value: object) -> float:
    if not isinstance(value, (str, int, float)):
        return 0.0
    try:
        latency = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(latency, 3) if math.isfinite(latency) and latency >= 0 else 0.0


def _safe_nonnegative_int(value: object) -> int:
    try:
        numeric = int(str(value))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, numeric)


def _result(case: ShadowCase, trace: ContextIndexTrace | None) -> ShadowResult:
    return ShadowResult(
        case_id=case.case_id,
        mode=_safe_mode(trace.mode if trace is not None else "shadow"),
        workspace_label=_safe_label(case.workspace_label),
        displayed=_safe_displayed(trace),
        opened=_safe_opened(trace, case.answer_variant),
        source_status=_safe_status(trace),
        latency_ms=_finite_latency(trace.latency_ms if trace is not None else 0.0),
        rendered_chars=_safe_nonnegative_int(trace.rendered_chars) if trace is not None else 0,
        correction_next_turn=case.correction_next_turn,
        answer_variant=case.answer_variant,
        answer=_safe_answer(case.answer),
    )


def _build_case(case: ShadowCase, broker: _Broker) -> ShadowResult:
    workspace = WorkspaceIdentity(
        key=case.workspace_key,
        root="",
        label=_safe_label(case.workspace_label),
    )
    asyncio.run(
        broker.build(
            case.user_text,
            f"shadow-{case.case_id}",
            case.session_id or f"shadow-session-{case.case_id}",
            workspace,
            case.local_time,
            frozenset({content_fingerprint(case.user_text)}),
            **({"query_message_id": case.message_id} if getattr(broker, "native_memory", False) else {}),
        )
    )
    return _result(case, broker.last_trace)


def _write_jsonl(records: Iterable[ShadowResult], output: Path) -> None:
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target.parent, delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
        for record in records:
            temporary.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True))
            temporary.write("\n")
    temporary_path.replace(target)


def run_shadow_cases(
    cases: Sequence[ShadowCase], *, broker: _Broker, output: Path
) -> tuple[ShadowResult, ...]:
    """Replay cases through the broker and atomically emit only safe metadata."""

    ordered = tuple(sorted(cases, key=lambda case: case.case_id))
    results = tuple(_build_case(case, broker) for case in ordered)
    _write_jsonl(results, output)
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run model-free Context Index shadow replay")
    parser.add_argument("--fixture", type=Path, required=True, help="fixture workspace root")
    parser.add_argument("--turns", type=Path, required=True, help="de-identified input JSONL")
    parser.add_argument("--output", type=Path, required=True, help="sanitized output JSONL")
    args = parser.parse_args(argv)

    cases = load_shadow_cases(args.turns)
    broker = create_context_index_broker(_ShadowPreferences(), args.fixture)
    try:
        run_shadow_cases(cases, broker=broker, output=args.output)
    except OSError:
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through module CLI.
    raise SystemExit(main())
