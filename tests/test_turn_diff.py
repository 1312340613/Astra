"""Behavior tests for the interruptible turn diff (Subagent B workstream).

Contract of record: docs/superpowers/plans/2026-09-18-turn-change-ledger-m1.md
§3.1 (semantics table) and §4 TB (required test list 1-10).  Structural pins for
the same module live in tests/test_turn_change_contract.py.

Timing assertions use deliberately loose absolute bounds: they must show that an
aborted call stops early (orders of magnitude below a full scan), not benchmark
one specific machine.
"""

from __future__ import annotations

import ast
import inspect
import random
import time

from agent.runtime import turn_diff as turn_diff_module
from agent.runtime.turn_diff import (
    DEFAULT_MAX_FILE_BYTES,
    DIFF_COARSE,
    DIFF_FULL,
    DIFF_NONE,
    REASON_BINARY,
    REASON_CANCELLED,
    REASON_NONE,
    REASON_OVERSIZED,
    REASON_TIMEOUT,
    DiffStats,
    diff_bytes,
)

BIG_LIMIT = 16 * 1024 * 1024  # 测试用单文件上限（> 默认 2 MiB，用于构造大输入）
PROBE_BYTES = 8 * 1024  # 契约：二进制探测窗口 = 前 8 KiB
LARGE_LINES = 200_000  # "大输入"行数（~2.4 MiB/侧；完整计算 ~35 ms，远大于毫秒级 deadline）


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _text(*lines: str) -> bytes:
    return "".join(f"{line}\n" for line in lines).encode()


def _reference_line_count(data: bytes) -> int:
    """Independent line count: split on b"\\n", ignore one trailing empty segment."""
    if not data:
        return 0
    newlines = data.count(b"\n")
    return newlines if data.endswith(b"\n") else newlines + 1


def _numbered_pair(count: int, changed: int = 1) -> tuple[bytes, bytes]:
    """``count`` lines; the last ``changed`` lines differ (replacements)."""
    before_lines = [f"line-{index:06d}\n" for index in range(count)]
    after_lines = list(before_lines)
    for index in range(count - changed, count):
        after_lines[index] = f"changed-{index:06d}\n"
    return "".join(before_lines).encode(), "".join(after_lines).encode()


def _disjoint_pair(count: int) -> tuple[bytes, bytes]:
    """Two same-size files sharing no line at all (edit distance beyond budget)."""
    before = "".join(f"left-{index:06d}\n" for index in range(count)).encode()
    after = "".join(f"right-{index:06d}\n" for index in range(count)).encode()
    return before, after


def _reference_counts(before_lines: list[str], after_lines: list[str]) -> tuple[int, int]:
    """Test-only brute-force LCS reference: (added, removed) for small inputs."""
    size_before = len(before_lines)
    size_after = len(after_lines)
    lcs = [[0] * (size_after + 1) for _ in range(size_before + 1)]
    for i in range(size_before - 1, -1, -1):
        for j in range(size_after - 1, -1, -1):
            if before_lines[i] == after_lines[j]:
                lcs[i][j] = lcs[i + 1][j + 1] + 1
            else:
                lcs[i][j] = max(lcs[i + 1][j], lcs[i][j + 1])
    matches = lcs[0][0]
    return size_after - matches, size_before - matches


# ---------------------------------------------------------------------------
# 1. 基本计数（精确期望值；替换 = 1 增 + 1 删）
# ---------------------------------------------------------------------------


def test_identical_files_are_full_with_zero_counts():
    before = _text("alpha", "beta", "gamma")
    assert diff_bytes(before, before) == DiffStats(0, 0, DIFF_FULL, REASON_NONE, False)


def test_identical_empty_files_are_unchanged():
    assert diff_bytes(b"", b"") == DiffStats(0, 0, DIFF_FULL, REASON_NONE, False)


def test_inserted_line_counts_as_one_added():
    before = _text("a", "b", "c")
    after = _text("a", "x", "b", "c")
    assert diff_bytes(before, after) == DiffStats(1, 0, DIFF_FULL, REASON_NONE, False)


def test_inserted_block_counts_every_line_as_added():
    before = _text("a", "d")
    after = _text("a", "b", "c", "d")
    assert diff_bytes(before, after) == DiffStats(2, 0, DIFF_FULL, REASON_NONE, False)


def test_deleted_line_counts_as_one_removed():
    before = _text("a", "x", "b")
    after = _text("a", "b")
    assert diff_bytes(before, after) == DiffStats(0, 1, DIFF_FULL, REASON_NONE, False)


def test_replaced_line_counts_one_added_and_one_removed():
    before = _text("a", "b", "c")
    after = _text("a", "y", "c")
    assert diff_bytes(before, after) == DiffStats(1, 1, DIFF_FULL, REASON_NONE, False)


def test_replaced_block_counts_one_pair_per_line():
    before = _text("a", "b", "c", "d")
    after = _text("a", "X", "Y", "d")
    assert diff_bytes(before, after) == DiffStats(2, 2, DIFF_FULL, REASON_NONE, False)


def test_mixed_edits_report_precise_counts():
    before = _text("l1", "l2", "l3", "l4", "l5", "l6", "l7", "l8", "l9", "l10")
    # 插入 X、删除 l4、l6 替换为 Y、追加 Z → 3 增 2 删
    after = _text("l1", "l2", "X", "l3", "l5", "Y", "l7", "l8", "l9", "l10", "Z")
    assert diff_bytes(before, after) == DiffStats(3, 2, DIFF_FULL, REASON_NONE, False)


def test_appended_lines_count_as_added_only():
    assert diff_bytes(_text("a"), _text("a", "b", "c")) == DiffStats(2, 0, DIFF_FULL, REASON_NONE, False)


def test_prepended_lines_count_as_added_only():
    assert diff_bytes(_text("b", "c"), _text("a", "b", "c")) == DiffStats(1, 0, DIFF_FULL, REASON_NONE, False)


def test_removing_one_of_three_identical_lines():
    assert diff_bytes(_text("a", "a", "a"), _text("a", "a")) == DiffStats(0, 1, DIFF_FULL, REASON_NONE, False)


def test_swapped_adjacent_lines_are_one_replacement_pair():
    assert diff_bytes(_text("a", "b"), _text("b", "a")) == DiffStats(1, 1, DIFF_FULL, REASON_NONE, False)


def test_fully_different_small_files_count_all_lines():
    assert diff_bytes(_text("a", "b"), _text("x", "y", "z")) == DiffStats(3, 2, DIFF_FULL, REASON_NONE, False)


def test_exact_counts_reconcile_with_line_delta():
    cases = [
        (_text("a", "b", "c"), _text("a", "b", "c")),
        (_text("a", "b", "c"), _text("a", "x", "b", "c")),
        (_text("a", "b", "c"), _text("b", "c")),
        (_text("a", "b", "c"), _text("a", "y", "c")),
        (_text("a", "b"), _text("x", "y", "z")),
        (b"", _text("a", "b")),
        (_text("a", "b"), b""),
    ]
    for before, after in cases:
        result = diff_bytes(before, after)
        assert result.quality == DIFF_FULL
        assert result.reason == REASON_NONE
        assert result.added is not None and result.removed is not None
        assert result.added >= 0 and result.removed >= 0
        # 未匹配行数守恒：added - removed == 行数(after) - 行数(before)
        assert result.added - result.removed == _reference_line_count(after) - _reference_line_count(before)


def test_counts_match_bruteforce_lcs_reference_on_random_inputs():
    rng = random.Random(20260918)
    for _ in range(300):
        before = [f"l{rng.randrange(5)}" for _ in range(rng.randrange(0, 9))]
        after = [f"l{rng.randrange(5)}" for _ in range(rng.randrange(0, 9))]
        expected = _reference_counts(before, after)
        result = diff_bytes(
            "".join(f"{line}\n" for line in before).encode(),
            "".join(f"{line}\n" for line in after).encode(),
        )
        assert result.quality == DIFF_FULL
        assert (result.added, result.removed) == expected


# ---------------------------------------------------------------------------
# 2. before=None（新建）/ after=None（删除）
# ---------------------------------------------------------------------------


def test_created_file_counts_every_line_as_added():
    assert diff_bytes(None, _text("x", "y")) == DiffStats(2, 0, DIFF_FULL, REASON_NONE, False)


def test_created_file_without_trailing_newline_counts_last_line():
    assert diff_bytes(None, b"x\ny") == DiffStats(2, 0, DIFF_FULL, REASON_NONE, False)


def test_created_file_with_trailing_newline_does_not_add_a_line():
    assert diff_bytes(None, b"x\n") == DiffStats(1, 0, DIFF_FULL, REASON_NONE, False)


def test_created_single_line_without_newline():
    assert diff_bytes(None, b"x") == DiffStats(1, 0, DIFF_FULL, REASON_NONE, False)


def test_created_empty_file_is_no_change():
    assert diff_bytes(None, b"") == DiffStats(0, 0, DIFF_FULL, REASON_NONE, False)


def test_deleted_file_counts_every_line_as_removed():
    assert diff_bytes(_text("x", "y"), None) == DiffStats(0, 2, DIFF_FULL, REASON_NONE, False)


def test_deleted_file_without_trailing_newline_counts_last_line():
    assert diff_bytes(b"x\ny", None) == DiffStats(0, 2, DIFF_FULL, REASON_NONE, False)


def test_deleted_empty_file_is_no_change():
    assert diff_bytes(b"", None) == DiffStats(0, 0, DIFF_FULL, REASON_NONE, False)


def test_both_sides_absent_is_no_change():
    # 防御性边界：调用方（store）按侧状态过滤，正常不会两侧都传 None。
    assert diff_bytes(None, None) == DiffStats(0, 0, DIFF_FULL, REASON_NONE, False)


# ---------------------------------------------------------------------------
# 3. 二进制探测：前 8 KiB 含 NUL → none/binary
# ---------------------------------------------------------------------------


def test_nul_in_before_is_binary():
    assert diff_bytes(b"a\x00b\n", _text("a")) == DiffStats(None, None, DIFF_NONE, REASON_BINARY, False)


def test_nul_in_after_is_binary():
    assert diff_bytes(_text("a"), b"a\x00b\n") == DiffStats(None, None, DIFF_NONE, REASON_BINARY, False)


def test_nul_at_last_probe_byte_is_binary():
    data = b"x" * (PROBE_BYTES - 1) + b"\x00"
    assert diff_bytes(data, data) == DiffStats(None, None, DIFF_NONE, REASON_BINARY, False)


def test_nul_after_probe_window_is_not_probed():
    # 契约：只探测前 8 KiB；第 8193 字节起的 NUL 不判定为 binary。
    data = b"x" * PROBE_BYTES + b"\x00" + b"y" * 10
    assert diff_bytes(data, data) == DiffStats(0, 0, DIFF_FULL, REASON_NONE, False)


def test_binary_detected_before_size_scan_of_large_file():
    data = b"x" * 100 + b"\x00" + b"y" * 1_000_000
    assert diff_bytes(data, _text("a")) == DiffStats(None, None, DIFF_NONE, REASON_BINARY, False)


def test_empty_side_is_not_binary():
    assert diff_bytes(b"", _text("a")) == DiffStats(1, 0, DIFF_FULL, REASON_NONE, False)


def test_binary_result_carries_no_counts():
    result = diff_bytes(b"\x00binary", b"\x00other")
    assert result.quality == DIFF_NONE
    assert result.reason == REASON_BINARY
    assert result.added is None and result.removed is None
    assert result.truncated is False


# ---------------------------------------------------------------------------
# 4. 尺寸预检：任一侧超 max_bytes → none/oversized
# ---------------------------------------------------------------------------


def test_before_over_limit_is_oversized():
    limit = 1024
    assert diff_bytes(b"a" * (limit + 1), b"b", max_bytes=limit) == DiffStats(
        None, None, DIFF_NONE, REASON_OVERSIZED, False
    )


def test_after_over_limit_is_oversized():
    limit = 1024
    assert diff_bytes(b"b", b"a" * (limit + 1), max_bytes=limit) == DiffStats(
        None, None, DIFF_NONE, REASON_OVERSIZED, False
    )


def test_both_sides_over_limit_is_oversized():
    limit = 1024
    assert diff_bytes(b"a" * (limit + 1), b"b" * (limit + 1), max_bytes=limit) == DiffStats(
        None, None, DIFF_NONE, REASON_OVERSIZED, False
    )


def test_absent_side_over_limit_is_oversized():
    limit = 1024
    assert diff_bytes(None, b"a" * (limit + 1), max_bytes=limit) == DiffStats(
        None, None, DIFF_NONE, REASON_OVERSIZED, False
    )


def test_size_exactly_at_limit_is_allowed():
    limit = 4096
    assert diff_bytes(b"a" * limit, b"b" * limit, max_bytes=limit) == DiffStats(
        1, 1, DIFF_FULL, REASON_NONE, False
    )


def test_one_byte_over_limit_is_oversized():
    limit = 4096
    assert diff_bytes(b"a" * (limit + 1), b"b", max_bytes=limit) == DiffStats(
        None, None, DIFF_NONE, REASON_OVERSIZED, False
    )


def test_zero_limit_allows_only_empty_sides():
    assert diff_bytes(b"", b"", max_bytes=0) == DiffStats(0, 0, DIFF_FULL, REASON_NONE, False)
    assert diff_bytes(b"a", b"", max_bytes=0) == DiffStats(None, None, DIFF_NONE, REASON_OVERSIZED, False)


def test_oversized_check_precedes_binary_check():
    # 裁定（契约表第 1 行先于第 2 行）：尺寸预检先于二进制探测。
    limit = 1024
    data = b"\x00" * (limit + 1)
    assert diff_bytes(data, b"x\n", max_bytes=limit) == DiffStats(None, None, DIFF_NONE, REASON_OVERSIZED, False)


def test_default_limit_accepts_files_below_two_mib():
    before = b"a" * 1_500_000 + b"\nshared\n"
    after = b"b" * 1_500_000 + b"\nshared\n"
    assert diff_bytes(before, after) == DiffStats(1, 1, DIFF_FULL, REASON_NONE, False)


def test_default_limit_rejects_single_side_over_two_mib():
    oversized = b"a" * (DEFAULT_MAX_FILE_BYTES + 1) + b"\n"
    assert diff_bytes(oversized, _text("small")) == DiffStats(None, None, DIFF_NONE, REASON_OVERSIZED, False)


# ---------------------------------------------------------------------------
# 5. deadline 生效：快速返回、coarse/timeout、时间有界且稳定
# ---------------------------------------------------------------------------


def test_expired_deadline_returns_coarse_timeout_without_scanning():
    before, after = _numbered_pair(LARGE_LINES)
    started = time.monotonic()
    result = diff_bytes(before, after, max_bytes=BIG_LIMIT, deadline=time.monotonic() - 1.0)
    elapsed = time.monotonic() - started
    assert result.quality == DIFF_COARSE
    assert result.reason == REASON_TIMEOUT
    assert result.truncated is False
    assert elapsed < 0.2


def test_short_deadline_on_large_input_is_coarse_and_stable():
    before, after = _numbered_pair(LARGE_LINES)
    for _ in range(5):
        started = time.monotonic()
        result = diff_bytes(before, after, max_bytes=BIG_LIMIT, deadline=time.monotonic() + 0.001)
        elapsed = time.monotonic() - started
        assert result.quality == DIFF_COARSE
        assert result.reason == REASON_TIMEOUT
        # 只允许 None 或整段替换上界；绝不允许部分扫描得到的增删数
        assert (result.added, result.removed) in {(None, None), (LARGE_LINES, LARGE_LINES)}
        assert elapsed < 0.5


def test_deadline_is_an_absolute_monotonic_value():
    before, after = _numbered_pair(2_000)
    # 1.0 是"很久以前的 monotonic 时刻"：已过期 → 立即降级
    result = diff_bytes(before, after, deadline=1.0)
    assert result.quality == DIFF_COARSE
    assert result.reason == REASON_TIMEOUT


def test_generous_deadline_still_completes():
    before, after = _numbered_pair(2_000)
    result = diff_bytes(before, after, deadline=time.monotonic() + 30.0)
    assert result == DiffStats(1, 1, DIFF_FULL, REASON_NONE, False)


def test_deadline_none_means_no_time_limit():
    before, after = _numbered_pair(20_000)
    result = diff_bytes(before, after, max_bytes=BIG_LIMIT, deadline=None)
    assert result == DiffStats(1, 1, DIFF_FULL, REASON_NONE, False)


def test_abort_before_any_scan_reports_no_counts():
    # 裁定（precheck-first）：一旦超时/取消发生在切分之前，连行数都不提供 → None。
    before, after = _numbered_pair(LARGE_LINES)
    expired = diff_bytes(before, after, max_bytes=BIG_LIMIT, deadline=time.monotonic() - 1.0)
    cancelled = diff_bytes(
        before, after, max_bytes=BIG_LIMIT, deadline=time.monotonic() + 60.0, cancelled=lambda: True
    )
    assert (expired.added, expired.removed) == (None, None)
    assert (cancelled.added, cancelled.removed) == (None, None)


# ---------------------------------------------------------------------------
# 6. 返回时间与输入规模解耦
# ---------------------------------------------------------------------------


def test_return_time_is_decoupled_from_input_size():
    small_before, small_after = _numbered_pair(2_000)
    large_before, large_after = _numbered_pair(LARGE_LINES)

    started = time.monotonic()
    baseline = diff_bytes(large_before, large_after, max_bytes=BIG_LIMIT)
    baseline_wall = time.monotonic() - started
    assert baseline.quality == DIFF_FULL

    # 提前放弃的预算 = 本机"完整计算耗时"的三分之一：必然在完成前触发，
    # 且与具体机器的速度无关（自校准）。
    budget = max(0.001, baseline_wall / 3.0)

    started = time.monotonic()
    diff_bytes(small_before, small_after, max_bytes=BIG_LIMIT, deadline=time.monotonic() + budget)
    small_wall = time.monotonic() - started

    started = time.monotonic()
    large_result = diff_bytes(large_before, large_after, max_bytes=BIG_LIMIT, deadline=time.monotonic() + budget)
    large_wall = time.monotonic() - started

    assert large_result.quality == DIFF_COARSE
    assert large_result.reason == REASON_TIMEOUT
    assert small_wall < 0.5 and large_wall < 0.5  # 两种规模同界内
    if baseline_wall > 0.01:
        # 与输入规模解耦：提前返回显著快于同等规模的一次完整计算
        assert large_wall < baseline_wall


# ---------------------------------------------------------------------------
# 7. 确定性
# ---------------------------------------------------------------------------


def test_same_input_gives_same_result():
    before, after = _numbered_pair(5_000, changed=3)
    first = diff_bytes(before, after, max_bytes=BIG_LIMIT, deadline=None)
    second = diff_bytes(before, after, max_bytes=BIG_LIMIT, deadline=time.monotonic() + 30.0)
    assert first == second == DiffStats(3, 3, DIFF_FULL, REASON_NONE, False)


def test_result_does_not_depend_on_call_order():
    before, after = _text("a", "b", "c"), _text("a", "x", "c")
    first = diff_bytes(before, after)
    other_before, other_after = _numbered_pair(1_000)
    diff_bytes(other_before, other_after)
    second = diff_bytes(before, after)
    assert first == second == DiffStats(1, 1, DIFF_FULL, REASON_NONE, False)


def test_budget_degradation_is_deterministic_and_uses_whole_segment_counts():
    before, after = _disjoint_pair(10_000)
    first = diff_bytes(before, after, max_bytes=BIG_LIMIT)
    second = diff_bytes(before, after, max_bytes=BIG_LIMIT)
    assert first == second
    assert first.quality == DIFF_COARSE
    assert first.reason == REASON_TIMEOUT  # 复杂度预算耗尽 → 超时降级
    assert (first.added, first.removed) == (10_000, 10_000)  # 整段替换上界，非部分计数


# ---------------------------------------------------------------------------
# 8. 行切分与空文件语义
# ---------------------------------------------------------------------------


def test_split_ignores_trailing_empty_segment():
    assert diff_bytes(None, b"a\nb\n") == DiffStats(2, 0, DIFF_FULL, REASON_NONE, False)
    assert diff_bytes(None, b"a\nb") == DiffStats(2, 0, DIFF_FULL, REASON_NONE, False)


def test_blank_lines_are_counted():
    assert diff_bytes(None, b"a\n\n\nb\n") == DiffStats(4, 0, DIFF_FULL, REASON_NONE, False)
    assert diff_bytes(None, b"\n") == DiffStats(1, 0, DIFF_FULL, REASON_NONE, False)
    assert diff_bytes(None, b"\n\n") == DiffStats(2, 0, DIFF_FULL, REASON_NONE, False)


def test_carriage_return_is_line_content_not_a_separator():
    # \r 保留为行内内容：若做 CRLF 归一化，这里会是 (0, 0)
    replaced = diff_bytes(b"a\n", b"a\r\n")
    assert replaced == DiffStats(1, 1, DIFF_FULL, REASON_NONE, False)
    assert diff_bytes(None, b"a\r\nb\n") == DiffStats(2, 0, DIFF_FULL, REASON_NONE, False)


def test_line_longer_than_split_chunk_is_one_line():
    for line in (b"x" * 200_000, b"x" * 65_536, b"x" * 65_535):
        data = line + b"\ny\n"
        assert diff_bytes(None, data) == DiffStats(2, 0, DIFF_FULL, REASON_NONE, False)


def test_split_matches_reference_count_for_sample_inputs():
    samples = [
        b"",
        b"\n",
        b"\n\n",
        b"a",
        b"a\n",
        b"a\n\n",
        b"a\nb",
        b"a\nb\n",
        b"a\r\nb\r\n",
        b"x" * 65_536 + b"\ny",
        b"x" * 65_535 + b"\ny",
        b"a\n" * 1_000,
        b"\n" * 100,
        bytes(range(1, 256)),
    ]
    for sample in samples:
        expected = _reference_line_count(sample)
        assert diff_bytes(None, sample) == DiffStats(expected, 0, DIFF_FULL, REASON_NONE, False)
        assert diff_bytes(sample, None) == DiffStats(0, expected, DIFF_FULL, REASON_NONE, False)


def test_empty_side_semantics():
    assert diff_bytes(b"", b"a\n") == DiffStats(1, 0, DIFF_FULL, REASON_NONE, False)
    assert diff_bytes(b"a\n", b"") == DiffStats(0, 1, DIFF_FULL, REASON_NONE, False)
    assert diff_bytes(b"", b"") == DiffStats(0, 0, DIFF_FULL, REASON_NONE, False)


# ---------------------------------------------------------------------------
# 9. truncated 恒 False（M1）
# ---------------------------------------------------------------------------


def test_truncated_is_always_false():
    assert diff_bytes(b"a\n", b"b\n").truncated is False  # full
    assert diff_bytes(b"a\n", b"b\n", deadline=time.monotonic() - 1.0).truncated is False  # coarse
    assert diff_bytes(b"\x00", b"a\n").truncated is False  # none/binary


# ---------------------------------------------------------------------------
# 10. 主动取消（长 deadline 下提前停止）
# ---------------------------------------------------------------------------


def test_immediate_cancel_returns_coarse_cancelled_without_scanning():
    before, after = _numbered_pair(LARGE_LINES)
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return True

    started = time.monotonic()
    result = diff_bytes(before, after, max_bytes=BIG_LIMIT, deadline=time.monotonic() + 60.0, cancelled=cancelled)
    elapsed = time.monotonic() - started

    assert result.quality == DIFF_COARSE
    assert result.reason == REASON_CANCELLED
    assert result.truncated is False
    assert (result.added, result.removed) in {(None, None), (LARGE_LINES, LARGE_LINES)}
    assert calls >= 1
    assert elapsed < 0.1


def test_cancel_flag_is_polled_during_work():
    before, after = _numbered_pair(LARGE_LINES)
    polls = 0

    def cancelled() -> bool:
        nonlocal polls
        polls += 1
        return polls > 1  # 首次检查放行，之后的检查点立即取消

    result = diff_bytes(before, after, max_bytes=BIG_LIMIT, deadline=time.monotonic() + 60.0, cancelled=cancelled)
    assert result.quality == DIFF_COARSE
    assert result.reason == REASON_CANCELLED
    assert (result.added, result.removed) in {(None, None), (LARGE_LINES, LARGE_LINES)}
    assert polls >= 2


def test_cancellation_wins_over_expired_deadline():
    # 裁定：主动取消原因优先（降级不得覆盖取消原因）。
    result = diff_bytes(b"a\n", b"b\n", deadline=time.monotonic() - 1.0, cancelled=lambda: True)
    assert result.quality == DIFF_COARSE
    assert result.reason == REASON_CANCELLED


def test_false_cancel_flag_does_not_degrade():
    before, after = _numbered_pair(2_000)
    polls = 0

    def cancelled() -> bool:
        nonlocal polls
        polls += 1
        return False

    result = diff_bytes(before, after, deadline=time.monotonic() + 30.0, cancelled=cancelled)
    assert result == DiffStats(1, 1, DIFF_FULL, REASON_NONE, False)
    assert polls >= 1


def test_no_work_continues_after_return():
    before, after = _numbered_pair(1_000)
    polls: list[float] = []

    def cancelled() -> bool:
        polls.append(time.monotonic())
        return False

    assert diff_bytes(before, after, cancelled=cancelled) == DiffStats(1, 1, DIFF_FULL, REASON_NONE, False)
    assert polls  # 至少轮询一次
    settled = len(polls)
    time.sleep(0.05)
    assert len(polls) == settled  # 返回后没有任何执行体继续运行


# ---------------------------------------------------------------------------
# 结构与纯度（契约硬约束：纯计算、无可中断性缺口）
# ---------------------------------------------------------------------------


def test_module_imports_are_pure_and_interruptible_only():
    tree = ast.parse(inspect.getsource(turn_diff_module))
    module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module_names.add(node.module or "")
    # 无 difflib（不可中断全量算法）、无线程/进程/子进程/durable_io
    assert module_names == {"__future__", "time", "dataclasses", "typing"}
