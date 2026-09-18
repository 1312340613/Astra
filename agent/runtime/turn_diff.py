"""Interruptible line-level diff for one before/after pair. Pure computation:
no I/O, no manifests, no events, no threads/processes.

M1 interface contract (frozen 2026-09-18; see
docs/superpowers/plans/2026-09-18-turn-change-ledger-m1.md §3.1).

Implementation (Subagent B workstream):

* Cooperative stop only: every loop advances in blocks and polls ``deadline``
  (an absolute ``time.monotonic()`` value) and ``cancelled()`` at checkpoints.
  ``difflib`` and any other whole-file, non-interruptible algorithm are unused.
* Bounded work: the line diff is a greedy Myers edit-distance search capped by
  an edit-distance limit and a work-unit limit.  Exceeding either cap degrades
  to ``DIFF_COARSE`` with the whole-segment upper bound counts (``added`` =
  lines in ``after``, ``removed`` = lines in ``before``); that degradation is
  reported as ``REASON_TIMEOUT``, because a coarse result only has the
  timeout/cancelled reasons.
* A degraded result never carries counts from a partial scan: counts are either
  the whole-file line counts (both existing sides already split) or ``None``
  (stopped before the split finished).
* Pre-check order is size, binary probe, checkpoints: an already-expired
  deadline or an already-cancelled call stops before any content scan.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

# 质量/原因常量
DIFF_FULL = "full"        # 正常完成
DIFF_COARSE = "coarse"    # 超时/取消：计数为"整段替换"粗值
DIFF_NONE = "none"        # 不可用（binary/oversized/错误输入）

REASON_NONE = ""
REASON_BINARY = "binary"
REASON_OVERSIZED = "oversized"
REASON_TIMEOUT = "timeout"
REASON_CANCELLED = "cancelled"     # 主动取消（长 deadline 下提前停止）

DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024   # 2 MiB（对齐 dsh maxFileBytes）

# 内部预算/检查点常量（实现细节，不属契约面）
_BINARY_PROBE_BYTES = 8 * 1024     # 二进制探测窗口：仅前 8 KiB
_LINE_CHUNK_BYTES = 64 * 1024      # 行切分块：同时是超时/取消检查点粒度
_LINE_TRIM_CHECKPOINT = 1024       # 前缀/后缀裁剪检查点（行）
_WORK_CHECKPOINT = 512             # Myers 推进检查点（工作单元）
_DIAGONAL_CHECKPOINT = 16          # Myers d 循环检查点
_MAX_EDIT_DISTANCE = 512           # 编辑距离上限；超出 → 降级 coarse
_MAX_WORK_UNITS = 200_000          # 工作单元上限；超出 → 降级 coarse


class _Aborted(Exception):
    """Internal cooperative stop signal; never escapes :func:`diff_bytes`."""

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Checkpoints:
    """Polling checkpoints: explicit cancellation first, then the deadline."""

    __slots__ = ("_cancelled", "_deadline")

    def __init__(self, cancelled: Callable[[], bool] | None, deadline: float | None) -> None:
        self._cancelled = cancelled
        self._deadline = deadline

    def poll(self) -> None:
        if self._cancelled is not None and self._cancelled():
            # 主动取消优先于超时：降级不得覆盖取消原因
            raise _Aborted(REASON_CANCELLED)
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise _Aborted(REASON_TIMEOUT)


@dataclass(frozen=True)
class DiffStats:
    """Result of one diff computation (see plan §3.1 semantics table)."""

    added: int | None      # 新增行数；None = 不可用
    removed: int | None    # 删除行数；None = 不可用
    quality: str           # DIFF_FULL / DIFF_COARSE / DIFF_NONE
    reason: str            # REASON_* 或 ""
    truncated: bool        # M1 恒 False（行截断属 M3 对比文本）


def _poll_budget(checks: _Checkpoints, work: int) -> None:
    if work >= _MAX_WORK_UNITS:
        raise _Aborted(REASON_TIMEOUT)
    checks.poll()


def _split_lines(data: bytes, checks: _Checkpoints) -> list[bytes]:
    """Split ``data`` on LF; a trailing empty segment is not a line.

    ``\\r`` stays inside the line content (M1 does not normalise CRLF).  The scan
    advances in ``_LINE_CHUNK_BYTES`` blocks so checkpoints stay reachable even
    for a single huge line.
    """
    lines: list[bytes] = []
    size = len(data)
    line_start = 0
    position = 0
    while position < size:
        checks.poll()
        chunk_end = min(position + _LINE_CHUNK_BYTES, size)
        while True:
            newline = data.find(b"\n", position, chunk_end)
            if newline < 0:
                break
            lines.append(data[line_start:newline])
            line_start = newline + 1
            position = line_start
        position = chunk_end
    if line_start < size:
        lines.append(data[line_start:])
    return lines


def _bounded_edit_distance(before: list[bytes], after: list[bytes], checks: _Checkpoints) -> int:
    """Shortest edit-script length between two line lists (greedy Myers).

    Raises :class:`_Aborted` when a checkpoint trips or when the edit-distance /
    work-unit caps are exceeded; the caller degrades to ``coarse`` then.
    """
    size_before = len(before)
    size_after = len(after)
    limit = min(size_before + size_after, _MAX_EDIT_DISTANCE)
    offset = limit + 1
    frontier = [0] * (2 * limit + 3)
    work = 0
    for distance in range(limit + 1):
        if distance % _DIAGONAL_CHECKPOINT == 0:
            checks.poll()
        for diagonal in range(-distance, distance + 1, 2):
            work += 1
            if work % _WORK_CHECKPOINT == 0:
                _poll_budget(checks, work)
            if diagonal == -distance:
                x = frontier[diagonal + 1 + offset]
            elif diagonal == distance or frontier[diagonal - 1 + offset] >= frontier[diagonal + 1 + offset]:
                x = frontier[diagonal - 1 + offset] + 1
            else:
                x = frontier[diagonal + 1 + offset]
            y = x - diagonal
            while x < size_before and y < size_after and before[x] == after[y]:
                x += 1
                y += 1
                work += 1
                if work % _WORK_CHECKPOINT == 0:
                    _poll_budget(checks, work)
            frontier[diagonal + offset] = x
            if x >= size_before and y >= size_after:
                return distance
    raise _Aborted(REASON_TIMEOUT)


def _count_changes(before: list[bytes], after: list[bytes], checks: _Checkpoints) -> tuple[int, int]:
    """Exact ``(added, removed)`` line counts for two line lists."""
    size_before = len(before)
    size_after = len(after)

    prefix = 0
    while prefix < size_before and prefix < size_after:
        if prefix % _LINE_TRIM_CHECKPOINT == 0:
            checks.poll()
        if before[prefix] != after[prefix]:
            break
        prefix += 1

    suffix = 0
    while suffix < size_before - prefix and suffix < size_after - prefix:
        if suffix % _LINE_TRIM_CHECKPOINT == 0:
            checks.poll()
        if before[size_before - 1 - suffix] != after[size_after - 1 - suffix]:
            break
        suffix += 1

    middle_before = size_before - prefix - suffix
    middle_after = size_after - prefix - suffix
    if middle_before == 0:
        return middle_after, 0
    if middle_after == 0:
        return 0, middle_before

    distance = _bounded_edit_distance(before[prefix : size_before - suffix], after[prefix : size_after - suffix], checks)
    # 编辑距离 d = 未匹配 before 行 + 未匹配 after 行 ⇒ matches = (n + m - d) / 2
    matches = (middle_before + middle_after - distance) // 2
    return middle_after - matches, middle_before - matches


def diff_bytes(
    before: bytes | None,
    after: bytes | None,
    *,
    max_bytes: int = DEFAULT_MAX_FILE_BYTES,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> DiffStats:
    """Compute line counts for one before/after pair.

    ``None`` means "this side does not exist" (created / deleted file).  Pure
    computation: no I/O, no manifests, no events, no threads, and no residual
    work once it returns.
    """
    checks = _Checkpoints(cancelled, deadline)

    # 预检 1：单侧字节上限（含"任一侧超限"）
    if (before is not None and len(before) > max_bytes) or (after is not None and len(after) > max_bytes):
        return DiffStats(None, None, DIFF_NONE, REASON_OVERSIZED, False)

    # 预检 2：二进制探测（仅前 8 KiB）
    for side in (before, after):
        if side is not None and b"\x00" in side[:_BINARY_PROBE_BYTES]:
            return DiffStats(None, None, DIFF_NONE, REASON_BINARY, False)

    coarse_added: int | None = None
    coarse_removed: int | None = None
    try:
        checks.poll()
        if before is not None and before == after:
            return DiffStats(0, 0, DIFF_FULL, REASON_NONE, False)

        before_lines = None if before is None else _split_lines(before, checks)
        after_lines = None if after is None else _split_lines(after, checks)
        # 两侧都已切分：此后即使被中断，粗计数（整段替换上界）也已可得
        coarse_removed = 0 if before_lines is None else len(before_lines)
        coarse_added = 0 if after_lines is None else len(after_lines)

        if before_lines is None:
            return DiffStats(coarse_added, 0, DIFF_FULL, REASON_NONE, False)
        if after_lines is None:
            return DiffStats(0, coarse_removed, DIFF_FULL, REASON_NONE, False)

        added, removed = _count_changes(before_lines, after_lines, checks)
        return DiffStats(added, removed, DIFF_FULL, REASON_NONE, False)
    except _Aborted as stop:
        return DiffStats(coarse_added, coarse_removed, DIFF_COARSE, stop.reason, False)
