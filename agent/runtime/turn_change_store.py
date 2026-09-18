"""Session-owned turn-change snapshot area. Independent of file checkpoints;
never enters model requests.

M1 interface contract (frozen 2026-09-18; see
docs/superpowers/plans/2026-09-18-turn-change-ledger-m1.md §3.2).
Behavior is implemented by the Subagent A workstream; this skeleton ships
first so both parallel workstreams compile against one contract.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .turn_diff import DiffStats

# 五态
STATE_MODIFIED = "modified"
STATE_ADDED = "added"
STATE_DELETED = "deleted"
STATE_UNCHANGED = "unchanged"
STATE_UNKNOWN = "unknown"

# 每侧快照状态（"原来不存在" vs "未捕获" 必须显式区分）
SIDE_CAPTURED = "captured"      # 有字节快照
SIDE_ABSENT = "absent"          # 确认当时不存在
SIDE_UNCAPTURED = "uncaptured"  # 未取得快照（失败/超限/配额）

# 对比可用性
COMPARE_FULL = "full"
COMPARE_COARSE = "coarse"
COMPARE_NONE = "none"

REASON_QUOTA = "quota"
REASON_TRACKED = "tracked"
REASON_ERROR = "error"


@dataclass(frozen=True)
class TurnChangeLimits:
    """Budgets for the snapshot area and diff computation."""

    max_file_bytes: int = 2 * 1024 * 1024        # 单文件参与字节
    max_paths_per_turn: int = 500                # 每回合路径数
    max_turn_bytes: int = 32 * 1024 * 1024       # 单回合快照总字节
    max_session_bytes: int = 256 * 1024 * 1024   # 单会话快照区总字节
    max_turns_retained: int = 10                 # FIFO 保留回合数
    diff_deadline_ms: int = 100                  # 单文件 diff 时限
    compute_budget_ms: int = 500                 # 回合计算总预算


@dataclass
class FileChange:
    path: str
    display: str
    state: str
    before_state: str
    after_state: str
    added: int | None
    removed: int | None
    compare: str
    reason: str
    checkpoint_ids: list[str]


@dataclass
class TurnChangesManifest:
    session_id: str
    request_id: str
    turn_seq: int
    created_at: float
    files: list[FileChange]      # 已确认净改动（含 binary/coarse 条目）
    unknown: list[FileChange]    # 未能确认区（绝不与已确认并列）
    totals: dict[str, int]       # {"files": n, "added": x, "removed": y}


@dataclass(frozen=True)
class LoadedSides:
    before: bytes | None
    after: bytes | None
    before_state: str
    after_state: str   # captured / absent / uncaptured（读取失败=uncaptured）


class TurnChangeStore:
    """Session-owned snapshot area; one instance per session (see plan §3.2)."""

    def __init__(
        self,
        workspace: str | Path,
        session_id: str,
        *,
        limits: TurnChangeLimits | None = None,
        root: str | Path | None = None,
        differ: Callable[..., DiffStats] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        raise NotImplementedError("Subagent A workstream implements this")

    def begin_turn(self, request_id: str) -> None:
        raise NotImplementedError

    def note_paths(self, paths: Iterable[str]) -> None:
        raise NotImplementedError

    def note_capture(self, path: str, before: bytes, *, checkpoint_id: str = "") -> None:
        raise NotImplementedError

    def note_absent(self, path: str, *, checkpoint_id: str = "") -> None:
        raise NotImplementedError

    def note_tracked(self, path: str, before_fp: str | None, after_fp: str | None) -> None:
        raise NotImplementedError

    def seal(self, *, cancelled: Callable[[], bool] | None = None) -> TurnChangesManifest | None:
        raise NotImplementedError

    def manifest(self, turn_offset: int = 0) -> TurnChangesManifest | None:
        raise NotImplementedError

    def load_sides(self, turn_offset: int, path: str) -> LoadedSides:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    @staticmethod
    def cleanup_orphans(
        root: str | Path,
        *,
        is_alive: Callable[[int], bool] | None = None,
    ) -> list[str]:
        raise NotImplementedError


def current_turn_change_store() -> TurnChangeStore | None:
    raise NotImplementedError("Subagent A workstream implements this")


@contextmanager
def turn_store_scope(store: TurnChangeStore | None) -> Iterator[None]:
    raise NotImplementedError("Subagent A workstream implements this")
    # Unreachable yield keeps this a generator so the decorator type-checks.
    yield
