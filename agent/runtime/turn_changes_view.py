"""Pure rendering for the ``/changes`` command (M3 · T3, plan §3.4/§3.5).

No I/O, no store access: the caller reads the bounded ledger (``CompletedTurn``
index records, ``TurnChangesManifest`` entries, ``LoadedSides`` bytes) and this
module turns those values into the frozen text shapes.

Three honesty rules from review R4 drive the implementation:

* the reason a difference is not line-alignable is *stated* (``coarse`` compare,
  scan limits, truncation) instead of being papered over;
* a line-end-only difference is claimed only after normalising both sides by
  line-end form — a masked difference (decoding replacement) or a change outside
  the shown range gets its own wording instead;
* every cap (2000 chars per line, 400 lines / 200,000 chars total, 2000 lines or
  256 KiB scanned per side, 200-line preview for very long files) is applied and
  marked, and the title line plus marks count against the total.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .turn_change_store import (
    COMPARE_COARSE,
    COMPARE_NONE,
    SIDE_ABSENT,
    SIDE_UNCAPTURED,
    CompletedTurn,
    FileChange,
    LoadedSides,
    TurnChangesManifest,
)
from .turn_diff import REASON_BINARY

USAGE = "Usage: /changes [@k] [n|path]"
MAX_TURN_WINDOW = 10

# 渲染上限（§3.4；口径=字符，标题与截断标注一并计入）
MAX_OUTPUT_LINES = 400
MAX_OUTPUT_CHARS = 200_000
MAX_LINE_CHARS = 2000
MAX_SCAN_LINES = 2000
MAX_SCAN_BYTES = 256 * 1024
MAX_PREVIEW_LINES = 200
MAX_CONTEXT_LINES = 3
MAX_PATH_COLUMN = 48

MINUS = "\u2212"
DASH = "\u2014"

COARSE_MARK = "（粗略展示：仅显示替换块，未逐行对齐；计数以清单为准）"
RANGE_MARK = "（变化可能未展示范围）"
OUTPUT_TRUNCATION_MARK = "输出截断"
ENCODING_MARK = "（含编码替换字符，差异可能被掩盖）"
LONG_PREVIEW_MARK = "（超长预览：仅前 200 行，其余未展示）"
INDEX_UNAVAILABLE = "回合索引暂不可用；请稍后重试。"
NO_TURNS = "还没有完成的回合可供查看。"
EVICTED_MESSAGE = "回合的快照已被淘汰，无法展示改动清单。"

_REASON_TEXT = {
    "quota": "超出快照配额",
    "tracked": "仅记录非文本变更",
    "error": "读取失败或无法读取",
    "timeout": "计算未在预算内完成",
    "cancelled": "计算已被取消",
}


@dataclass(frozen=True)
class SelectorMatch:
    """Result of matching one ``[@k] <n|path>`` selector against the entries."""

    entry_index: int | None = None
    candidates: tuple[str, ...] = ()
    error: str = ""


def parse_changes_args(rest: str) -> tuple[int, str | None, str]:
    """Parse ``/changes`` arguments into ``(k, selector, error)``.

    ``""`` means the newest completed turn; ``@k`` selects the k-th newest
    (``1 <= k <= MAX_TURN_WINDOW``); the remainder is an entry selector that is
    resolved later against the manifest.  A bare number is a selector, never a
    turn index — only ``@`` addresses turns.
    """
    text = (rest or "").strip()
    if not text:
        return 1, None, ""
    if text.startswith("@"):
        head, _, tail = text.partition(" ")
        digits = head[1:]
        if not digits.isdigit():
            return 1, None, USAGE
        k = int(digits)
        if k < 1 or k > MAX_TURN_WINDOW:
            return 1, None, USAGE
        selector = tail.strip() or None
        return k, selector, ""
    return 1, text, ""


def resolve_selector(entries: Sequence[FileChange], selector: str) -> SelectorMatch:
    """Resolve ``selector`` against merged entries (编号直达 / 精确 → 唯一子串）."""
    text = (selector or "").strip()
    if not text:
        return SelectorMatch()
    if text.isdigit():
        number = int(text)
        if 1 <= number <= len(entries):
            return SelectorMatch(entry_index=number - 1)
        return SelectorMatch(error=f"{USAGE} · 该回合只有 {len(entries)} 个条目。")
    exact = [index for index, entry in enumerate(entries) if text in _keys(entry)]
    matches = exact or [
        index
        for index, entry in enumerate(entries)
        if any(text in key for key in _keys(entry))
    ]
    if len(matches) == 1:
        return SelectorMatch(entry_index=matches[0])
    if not matches:
        return SelectorMatch(error=f"{USAGE} · 未找到匹配 {text!r} 的条目。")
    candidates = tuple(_display(entries[index]) for index in matches)
    listing = ", ".join(f"{index + 1}. {_display(entries[index])}" for index in matches)
    return SelectorMatch(
        candidates=candidates,
        error=f"{USAGE} · 有多个条目匹配 {text!r}：{listing}。请改用条目编号。",
    )


# --------------------------------------------------------------- list view


def format_turn_list(
    k: int,
    record: CompletedTurn | None,
    manifest: TurnChangesManifest | None,
) -> str:
    """Render the per-turn ledger list bound to ``@k`` (newest completed = 1)."""
    if record is None:
        return INDEX_UNAVAILABLE
    if record.empty:
        return f"@{k} 回合没有可展示的改动。"
    if not record.available:
        return f"@{k} {EVICTED_MESSAGE}"
    if manifest is None:
        return INDEX_UNAVAILABLE

    entries = [*manifest.files, *manifest.unknown]
    if not entries:
        return f"@{k} 回合没有可展示的改动。"

    files_count = len(manifest.files)
    unknown_count = len(manifest.unknown)
    header = f"回合 @{k} · request {_short(record.request_id)} · {files_count} 个文件"
    if unknown_count:
        header += f"（另有 {unknown_count} 个路径未能确认）"

    labels = _checkpoint_labels(
        [checkpoint for entry in entries for checkpoint in entry.checkpoint_ids]
    )
    width = min(max(len(_display(entry)) for entry in entries), MAX_PATH_COLUMN)
    number_width = len(str(len(entries)))
    lines = [header]
    for index, entry in enumerate(entries, start=1):
        name = _display(entry)
        padding = " " * max(0, width - len(name))
        checkpoints = ", ".join(labels.get(cp, _short(cp)) for cp in entry.checkpoint_ids)
        checkpoint = f"[cp {checkpoints}]" if checkpoints else DASH
        row = f"{index:>{number_width}}. {name}{padding}  {_counts(entry)}   {checkpoint}"
        row += _compare_mark(entry)
        if index > files_count:
            row += f"（未能确认：{entry.reason or '未知原因'}）"
        lines.append(" " + row)
    return "\n".join(lines)


# --------------------------------------------------------------- diff view


def format_file_diff(
    k: int,
    record: CompletedTurn | None,
    manifest: TurnChangesManifest | None,
    entry: FileChange,
    sides: LoadedSides,
) -> str:
    """Render one entry's before/after view bound to ``(k, entry)``."""
    guard = _diff_guard(k, record, manifest)
    if guard:
        return guard
    title = f"回合 @{k} · {_display(entry)}（{_counts(entry)}）"
    lines, marks = _diff_body(entry, sides)
    return _finalize(title, lines, marks)


def _diff_guard(k: int, record: CompletedTurn | None, manifest: TurnChangesManifest | None) -> str:
    if record is None:
        return INDEX_UNAVAILABLE
    if record.empty:
        return f"@{k} 回合没有可展示的改动。"
    if not record.available:
        return f"@{k} {EVICTED_MESSAGE}"
    if manifest is None:
        return INDEX_UNAVAILABLE
    return ""


def _diff_body(entry: FileChange, sides: LoadedSides) -> tuple[list[str], list[str]]:
    before, after = sides.before, sides.after
    marks: list[str] = []

    if SIDE_UNCAPTURED in (sides.before_state, sides.after_state):
        missing = []
        if sides.before_state == SIDE_UNCAPTURED:
            missing.append("未捕获改动前内容")
        if sides.after_state == SIDE_UNCAPTURED:
            missing.append("未捕获改动后内容")
        marks.append(f"（证据缺失：{'，'.join(missing)}，无法对比）")
        sizes = _sizes_text(before, after)
        if sizes:
            marks.append(sizes)
        return [], marks

    if before is None and sides.before_state == SIDE_ABSENT:
        before = b""
        marks.append("（新增文件：改动前不存在，仅显示当前内容）")
    if after is None and sides.after_state == SIDE_ABSENT:
        after = b""
        marks.append("（删除文件：改动后不存在，仅显示改动前内容）")

    if entry.compare == COMPARE_NONE and entry.reason == REASON_BINARY:
        marks.append("（二进制文件，无文本对比）")
        sizes = _sizes_text(before, after)
        if sizes:
            marks.append(sizes)
        return [], marks

    if entry.compare == COMPARE_NONE:
        reason = _REASON_TEXT.get(entry.reason, "未取得可对比内容")
        marks.append(f"（无内容：{reason}，无法逐行对比）")
        sizes = _sizes_text(before, after)
        if sizes:
            marks.append(sizes)
        return [], marks

    if before is not None and before == after:
        marks.append("（两侧内容相同，无可展示的行级差异）")
        return [], marks

    scan_bounded = _within_scan_budget(before) and _within_scan_budget(after)
    if scan_bounded and before is not None and after is not None:
        line_end = _line_end_marks(before, after)
        if line_end:
            return [], [*marks, *line_end]
        if _visible_lines(before) == _visible_lines(after):
            marks.append(_masked_difference_mark(before, after))
            return [], marks

    if entry.compare == COMPARE_COARSE:
        lines = _block_lines(before, after)
        if not lines:
            marks.append("（无可展示的内容）")
        return lines, [*marks, COARSE_MARK]

    before_lines = _visible_lines(before)
    after_lines = _visible_lines(after)
    if len(before_lines) > MAX_SCAN_LINES or len(after_lines) > MAX_SCAN_LINES:
        preview_before = before_lines[:MAX_PREVIEW_LINES]
        preview_after = after_lines[:MAX_PREVIEW_LINES]
        if before != after:
            marks.append(RANGE_MARK)
        lines = _aligned_lines(preview_before, preview_after, [RANGE_MARK] if before != after else [])
        return lines, [*marks, LONG_PREVIEW_MARK]

    if not scan_bounded:
        # 扫描预算用尽：跳过修剪，整块替换（§3.4「整块替换=粗略展示」）
        lines = _block_lines(before, after)
        if not lines:
            marks.append("（无可展示的内容）")
        return lines, [*marks, COARSE_MARK]

    return _aligned_lines(before_lines, after_lines, marks), marks


def _aligned_lines(
    before_lines: Sequence[str], after_lines: Sequence[str], marks: list[str]
) -> list[str]:
    """Line-level view with bounded common prefix/suffix trimming."""
    size_before, size_after = len(before_lines), len(after_lines)
    prefix = 0
    while prefix < size_before and prefix < size_after and before_lines[prefix] == after_lines[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < size_before - prefix
        and suffix < size_after - prefix
        and before_lines[size_before - 1 - suffix] == after_lines[size_after - 1 - suffix]
    ):
        suffix += 1

    head_count = min(prefix, MAX_CONTEXT_LINES)
    tail_count = min(suffix, MAX_CONTEXT_LINES)
    omitted = (prefix - head_count) + (suffix - tail_count)
    lines = [_context(line) for line in before_lines[prefix - head_count : prefix]]
    lines += [_removed(line) for line in before_lines[prefix : size_before - suffix]]
    lines += [_added(line) for line in after_lines[prefix : size_after - suffix]]
    lines += [_context(line) for line in before_lines[size_before - suffix : size_before - suffix + tail_count]]
    if omitted > 0:
        marks.append(f"（省略 {omitted} 行未变化内容）")
    return lines


def _block_lines(before: bytes | None, after: bytes | None) -> list[str]:
    lines = [_removed(line) for line in _visible_lines(before)]
    lines += [_added(line) for line in _visible_lines(after)]
    return lines


def _finalize(title: str, lines: list[str], marks: list[str]) -> str:
    """Assemble the view and apply the total line/char caps, marks included."""
    whole = [title, *lines, *marks]
    if _fits(whole):
        return "\n".join(whole)
    marks = [*marks, RANGE_MARK]
    prefix_chars = [0]
    for line in lines:
        prefix_chars.append(prefix_chars[-1] + len(line) + 1)
    title_chars = len(title) + 1
    marks_chars = sum(len(mark) + 1 for mark in marks)
    for kept in range(len(lines), -1, -1):
        mark = f"（{OUTPUT_TRUNCATION_MARK}：仅显示前 {kept} 行，其余未展示）"
        total_lines = 1 + kept + len(marks) + 1
        total_chars = title_chars + prefix_chars[kept] + marks_chars + len(mark)
        if total_lines <= MAX_OUTPUT_LINES and total_chars <= MAX_OUTPUT_CHARS:
            candidate = [title, *lines[:kept], *marks, mark]
            if _fits(candidate):
                return "\n".join(candidate)
    return "\n".join([title, *marks])


def _fits(parts: Sequence[str]) -> bool:
    if len(parts) > MAX_OUTPUT_LINES:
        return False
    total = sum(len(part) for part in parts) + max(0, len(parts) - 1)
    return total <= MAX_OUTPUT_CHARS


# ------------------------------------------------------------------ helpers


def _keys(entry: FileChange) -> tuple[str, str]:
    return _display(entry), entry.path


def _display(entry: FileChange) -> str:
    return entry.display or entry.path


def _short(value: str) -> str:
    return value[:8]


def _compare_mark(entry: FileChange) -> str:
    """List marker for an unavailable comparison (parallel to the checkpoint column)."""
    if entry.compare == COMPARE_COARSE:
        return " [粗]"
    if entry.compare == COMPARE_NONE:
        return " [无内容]"
    return ""


def _counts(entry: FileChange) -> str:
    if entry.added is None or entry.removed is None:
        return DASH
    return f"+{entry.added} {MINUS}{entry.removed}"


def _checkpoint_labels(ids: Sequence[str]) -> dict[str, str]:
    """Prefix labels (first 8) that lengthen to the full id on a prefix collision."""
    groups: dict[str, list[str]] = {}
    for checkpoint in ids:
        groups.setdefault(checkpoint[:8], []).append(checkpoint)
    labels: dict[str, str] = {}
    for checkpoint in ids:
        labels[checkpoint] = (
            checkpoint if len(groups[checkpoint[:8]]) > 1 else checkpoint[:8]
        )
    return labels


def _truncate_line(line: str) -> str:
    if len(line) <= MAX_LINE_CHARS:
        return line
    removed = len(line) - MAX_LINE_CHARS
    return line[:MAX_LINE_CHARS] + f"…（行截断 +{removed} 字符）"


def _context(line: str) -> str:
    return "  " + _truncate_line(line)


def _removed(line: str) -> str:
    return "- " + _truncate_line(line)


def _added(line: str) -> str:
    return "+ " + _truncate_line(line)


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _visible_lines(data: bytes | None) -> list[str]:
    """Decoded lines; a trailing newline is not a line and ``\\r`` is folded in."""
    if not data:
        return []
    text = _decode(data)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _decoding_replaced(data: bytes | None) -> bool:
    if data is None:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _within_scan_budget(data: bytes | None) -> bool:
    return data is None or len(data) <= MAX_SCAN_BYTES


def _line_end_marks(before: bytes, after: bytes) -> list[str]:
    """Line-end-only difference, verified by normalising both sides (§3.4)."""
    if max(len(before), len(after)) > MAX_SCAN_BYTES:
        return []
    normalised_before = before.replace(b"\r\n", b"\n")
    normalised_after = after.replace(b"\r\n", b"\n")
    strip = lambda data: data[:-1] if data.endswith(b"\n") else data  # noqa: E731
    if strip(normalised_before) != strip(normalised_after):
        return []
    flags: list[str] = []
    if (b"\r\n" in before) != (b"\r\n" in after):
        flags.append("CRLF→LF" if b"\r\n" in before else "LF→CRLF")
    if before.endswith(b"\n") != after.endswith(b"\n"):
        flags.append("末尾换行 有→无" if before.endswith(b"\n") else "末尾换行 无→有")
    if not flags:
        flags.append("行尾形式")
    return [f"（差异仅行尾：{'，'.join(flags)}）"]


def _masked_difference_mark(before: bytes, after: bytes) -> str:
    """Bytes differ while the decoded lines match: say why, never claim no change."""
    if _decoding_replaced(before) or _decoding_replaced(after):
        return ENCODING_MARK
    return "（字节层存在差异，解码后的可见文本相同）"


def _sizes_text(before: bytes | None, after: bytes | None) -> str:
    parts = []
    if before is not None:
        parts.append(f"改动前 {len(before)} 字节")
    if after is not None:
        parts.append(f"改动后 {len(after)} 字节")
    return " · ".join(parts)


__all__ = [
    "USAGE",
    "MAX_OUTPUT_LINES",
    "MAX_OUTPUT_CHARS",
    "MAX_LINE_CHARS",
    "COARSE_MARK",
    "RANGE_MARK",
    "ENCODING_MARK",
    "LONG_PREVIEW_MARK",
    "INDEX_UNAVAILABLE",
    "NO_TURNS",
    "SelectorMatch",
    "parse_changes_args",
    "resolve_selector",
    "format_turn_list",
    "format_file_diff",
]
