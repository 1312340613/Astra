"""Interruptible line-level diff for one before/after pair. Pure computation:
no I/O, no manifests, no events, no threads/processes.

M1 interface contract (frozen 2026-09-18; see
docs/superpowers/plans/2026-09-18-turn-change-ledger-m1.md §3.1).
Behavior is implemented by the Subagent B workstream; this skeleton ships
first so both parallel workstreams compile against one contract.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class DiffStats:
    """Result of one diff computation (see plan §3.1 semantics table)."""

    added: int | None      # 新增行数；None = 不可用
    removed: int | None    # 删除行数；None = 不可用
    quality: str           # DIFF_FULL / DIFF_COARSE / DIFF_NONE
    reason: str            # REASON_* 或 ""
    truncated: bool        # M1 恒 False（行截断属 M3 对比文本）


def diff_bytes(
    before: bytes | None,
    after: bytes | None,
    *,
    max_bytes: int = DEFAULT_MAX_FILE_BYTES,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> DiffStats:
    """Compute line counts for one before/after pair (Subagent B implements)."""
    raise NotImplementedError("Subagent B workstream implements this")
