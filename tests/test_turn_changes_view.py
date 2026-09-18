"""Unit tests for the ``/changes`` view module (M3 · T3, plan §3.4/§3.5).

The module under test is pure: no store, no I/O.  Inputs are the frozen store
dataclasses (``CompletedTurn`` / ``TurnChangesManifest`` / ``FileChange`` /
``LoadedSides``), so the acceptance points of §3.4 (single 2 MiB line, change
outside the shown range, ``a\\n`` → ``a``, CRLF → LF, create/delete/uncaptured)
are all reachable without a backend.
"""

from __future__ import annotations

from agent.runtime import turn_changes_view as view
from agent.runtime.turn_change_store import (
    COMPARE_COARSE,
    COMPARE_FULL,
    COMPARE_NONE,
    SIDE_ABSENT,
    SIDE_CAPTURED,
    SIDE_UNCAPTURED,
    CompletedTurn,
    FileChange,
    LoadedSides,
    TurnChangesManifest,
)

REQUEST_ID = "8c749c42-0f1e-4a2b-9c3d-5e6f7a8b9c0d"
REQUEST_SHORT = REQUEST_ID[:8]


def change(path: str, **overrides) -> FileChange:
    data = dict(
        path=path,
        display=overrides.pop("display", path),
        state="modified",
        before_state=SIDE_CAPTURED,
        after_state=SIDE_CAPTURED,
        added=1,
        removed=1,
        compare=COMPARE_FULL,
        reason="",
        checkpoint_ids=[],
    )
    data.update(overrides)
    return FileChange(**data)


def manifest(files=(), unknown=(), request_id=REQUEST_ID, turn_seq=1) -> TurnChangesManifest:
    return TurnChangesManifest(
        session_id="sess",
        request_id=request_id,
        turn_seq=turn_seq,
        created_at=0.0,
        files=list(files),
        unknown=list(unknown),
        totals={"files": len(files), "added": 0, "removed": 0},
    )


def record(
    turn_seq: int = 1,
    *,
    empty: bool = False,
    available: bool = True,
    files: int = 0,
    unknown: int = 0,
    request_id: str = REQUEST_ID,
    dir_name: str = "turn-1",
) -> CompletedTurn:
    return CompletedTurn(
        turn_seq=turn_seq,
        request_id=request_id,
        created_at=0.0,
        dir_name=dir_name,
        empty=empty,
        files=files,
        unknown=unknown,
        available=available,
    )


def sides(
    before: bytes | None = None,
    after: bytes | None = None,
    before_state: str = SIDE_CAPTURED,
    after_state: str = SIDE_CAPTURED,
) -> LoadedSides:
    return LoadedSides(before, after, before_state, after_state)


def lines_of(text: str) -> list[str]:
    return text.split("\n")


# ------------------------------------------------------------------- parsing


def test_parse_accepts_bare_at_and_plain_forms():
    assert view.parse_changes_args("") == (1, None, "")
    assert view.parse_changes_args("   ") == (1, None, "")
    assert view.parse_changes_args("@3") == (3, None, "")
    assert view.parse_changes_args("@2 note.txt") == (2, "note.txt", "")
    assert view.parse_changes_args("3") == (1, "3", "")
    assert view.parse_changes_args("  @2   src/a.py  ") == (2, "src/a.py", "")
    assert view.parse_changes_args("note file.txt") == (1, "note file.txt", "")


def test_parse_rejects_bad_windows_with_usage():
    for bad in ("@0", "@", "@x", "@-1", "@ 3"):
        k, selector, error = view.parse_changes_args(bad)
        assert (k, selector) == (1, None), bad
        assert error == view.USAGE, bad


def test_usage_constant_is_frozen():
    assert view.USAGE == "Usage: /changes [@k] [n|path]"


def test_parse_accepts_large_window():
    assert view.parse_changes_args("@10") == (10, None, "")
    assert view.parse_changes_args("@10 2") == (10, "2", "")


# ------------------------------------------------------------- selector match


def test_resolve_selector_accepts_number_exact_and_unique_substring():
    entries = [change("src/a.py"), change("src/b.py"), change("docs/note.md")]
    assert view.resolve_selector(entries, "2").entry_index == 1
    assert view.resolve_selector(entries, "src/b.py").entry_index == 1
    assert view.resolve_selector(entries, "note.md").entry_index == 2


def test_resolve_selector_reports_ambiguity_and_misses():
    entries = [
        change("p1", display="pkg/x.txt"),
        change("p2", display="pkg/x.txt"),
        change("src/a.py"),
        change("src/ab.py"),
    ]
    exact = view.resolve_selector(entries, "pkg/x.txt")
    assert exact.entry_index is None
    assert exact.candidates == ("pkg/x.txt", "pkg/x.txt")
    assert "1. pkg/x.txt" in exact.error and "2. pkg/x.txt" in exact.error
    assert view.USAGE in exact.error

    ambiguous = view.resolve_selector(entries, "src/a")
    assert ambiguous.entry_index is None
    assert len(ambiguous.candidates) == 2
    assert "3. src/a.py" in ambiguous.error and "4. src/ab.py" in ambiguous.error
    assert view.USAGE in ambiguous.error

    missing = view.resolve_selector(entries, "nope")
    assert missing.entry_index is None
    assert missing.candidates == ()
    assert view.USAGE in missing.error

    out_of_range = view.resolve_selector(entries, "9")
    assert out_of_range.entry_index is None
    assert view.USAGE in out_of_range.error


# ------------------------------------------------------------------ list view


def test_list_header_and_rows_follow_the_frozen_shape():
    files = [
        change("agent/runtime/react.py", added=42, removed=7, checkpoint_ids=["51f06974aa"]),
        change("ui-tui/src/app.tsx", added=10, removed=0, compare=COMPARE_COARSE),
        change("notes/old.md", added=None, removed=None, compare=COMPARE_NONE, reason="tracked"),
    ]
    unknown = [
        change("src/mystery.txt", added=None, removed=None, compare=COMPARE_NONE, reason="error"),
        change("src/other.txt", added=None, removed=None, compare=COMPARE_NONE, reason="quota"),
    ]
    text = view.format_turn_list(1, record(files=3, unknown=2), manifest(files, unknown))
    lines = lines_of(text)
    assert lines[0] == f"回合 @1 · request {REQUEST_SHORT} · 3 个文件（另有 2 个路径未能确认）"
    assert len(lines) == 6

    assert lines[1].startswith(" 1. agent/runtime/react.py")
    assert "+42 \u22127" in lines[1]
    assert "[cp 51f06974]" in lines[1]

    assert lines[2].startswith(" 2. ui-tui/src/app.tsx")
    assert "+10 \u22120" in lines[2]
    assert "[粗]" in lines[2]

    assert lines[3].startswith(" 3. notes/old.md")
    assert "[无内容]" in lines[3]

    assert lines[4].startswith(" 4. src/mystery.txt")
    assert "—" in lines[4]
    assert "（未能确认：error）" in lines[4]

    assert lines[5].startswith(" 5. src/other.txt")
    assert "（未能确认：quota）" in lines[5]


def test_list_uses_minus_sign_and_dash_for_unavailable_counts():
    files = [change("a.txt", added=3, removed=0), change("b.txt", added=None, removed=None)]
    text = view.format_turn_list(1, record(files=2), manifest(files))
    lines = lines_of(text)
    assert "\u2212" in lines[1]
    assert "-" not in lines[1]
    assert "—" in lines[2]
    assert lines[2].rstrip().endswith("—")


def test_list_expands_colliding_checkpoint_prefixes():
    files = [change("a.txt", checkpoint_ids=["abcdefgh1111", "abcdefgh2222"])]
    text = view.format_turn_list(1, record(files=1), manifest(files))
    assert "[cp abcdefgh1111, abcdefgh2222]" in text


def test_list_shows_multiple_checkpoints_and_keeps_column_without_them():
    files = [
        change("a.txt", checkpoint_ids=["11111111aa", "22222222bb"]),
        change("b.txt"),
    ]
    text = view.format_turn_list(1, record(files=2), manifest(files))
    lines = lines_of(text)
    assert "[cp 11111111, 22222222]" in lines[1]
    assert "[cp" not in lines[2]
    assert lines[2].rstrip().endswith("—")


def test_list_empty_evicted_and_unavailable_states_are_distinct():
    empty = view.format_turn_list(2, record(turn_seq=2, empty=True, dir_name=None), None)
    assert empty == "@2 回合没有可展示的改动。"

    evicted = view.format_turn_list(1, record(available=False), None)
    assert "淘汰" in evicted
    assert "没有可展示的改动" not in evicted

    unavailable = view.format_turn_list(1, None, None)
    assert unavailable.startswith("回合索引暂不可用")

    bare = view.format_turn_list(1, record(files=0), manifest())
    assert bare == "@1 回合没有可展示的改动。"


# ------------------------------------------------------------------ diff view


def test_diff_added_file_shows_after_content():
    entry = change("note.txt", state="added", added=2, removed=0, before_state=SIDE_ABSENT)
    text = view.format_file_diff(
        1, record(), manifest(), entry, sides(None, b"hello\nworld\n", before_state=SIDE_ABSENT)
    )
    lines = lines_of(text)
    assert lines[0] == "回合 @1 · note.txt（+2 \u22120）"
    assert any(line.startswith("+ hello") for line in lines)
    assert any(line.startswith("+ world") for line in lines)
    assert not any(line.startswith("- ") for line in lines)
    assert "新增文件" in text


def test_diff_deleted_file_shows_before_content():
    entry = change("old.txt", state="deleted", added=0, removed=1, after_state=SIDE_ABSENT)
    text = view.format_file_diff(
        3, record(), manifest(), entry, sides(b"bye\n", None, after_state=SIDE_ABSENT)
    )
    lines = lines_of(text)
    assert lines[0] == "回合 @3 · old.txt（+0 \u22121）"
    assert any(line.startswith("- bye") for line in lines)
    assert not any(line.startswith("+ ") for line in lines)
    assert "删除" in text


def test_diff_uncaptured_reports_evidence_gap_without_content():
    entry = change("x.txt", added=None, removed=None, compare=COMPARE_NONE, reason="error")
    text = view.format_file_diff(
        1, record(), manifest(), entry, sides(None, b"secret\n", before_state=SIDE_UNCAPTURED)
    )
    assert "secret" not in text
    assert "证据缺失" in text
    assert "未捕获改动前内容" in text
    assert "改动后 7 字节" in text


def test_diff_binary_reports_both_sizes():
    entry = change("bin.dat", added=None, removed=None, compare=COMPARE_NONE, reason="binary")
    text = view.format_file_diff(2, record(turn_seq=2), manifest(turn_seq=2), entry, sides(b"\x00ab", b"\x00abcd"))
    assert "二进制文件，无文本对比" in text
    assert "3 字节" in text and "5 字节" in text


def test_diff_none_compare_keeps_list_marker_and_reason():
    entry = change("q.txt", added=None, removed=None, compare=COMPARE_NONE, reason="quota")
    text = view.format_file_diff(1, record(), manifest(), entry, sides(b"a\n", b"b\n"))
    assert "无内容" in text
    assert "超出快照配额" in text


def test_diff_coarse_marks_whole_block_replacement():
    entry = change("big.txt", added=3, removed=3, compare=COMPARE_COARSE, reason="timeout")
    text = view.format_file_diff(1, record(), manifest(), entry, sides(b"old\n" * 3, b"new\n" * 3))
    assert view.COARSE_MARK in text
    assert sum(1 for line in lines_of(text) if line.startswith("- old")) == 3
    assert sum(1 for line in lines_of(text) if line.startswith("+ new")) == 3


def test_diff_full_aligns_common_prefix_and_suffix():
    entry = change("a.txt")
    text = view.format_file_diff(
        1, record(), manifest(), entry, sides(b"1\n2\n3\n4\n5\n", b"1\n2\nX\n4\n5\n")
    )
    lines = lines_of(text)
    assert lines[0] == "回合 @1 · a.txt（+1 \u22121）"
    assert "- 3" in lines
    assert "+ X" in lines
    assert "  1" in lines
    assert view.COARSE_MARK not in text
    assert view.RANGE_MARK not in text


def test_diff_trailing_newline_only_difference_is_explained():
    entry = change("a.txt", added=0, removed=0)
    text = view.format_file_diff(1, record(), manifest(), entry, sides(b"a\n", b"a"))
    assert "差异仅行尾" in text
    assert "末尾换行 有→无" in text
    assert view.RANGE_MARK not in text
    assert view.ENCODING_MARK not in text


def test_diff_crlf_only_difference_is_explained():
    entry = change("a.txt", added=0, removed=0)
    text = view.format_file_diff(1, record(), manifest(), entry, sides(b"a\r\nb\r\n", b"a\nb\n"))
    assert "差异仅行尾" in text
    assert "CRLF→LF" in text
    assert view.RANGE_MARK not in text


def test_diff_replacement_decoding_marks_a_masked_difference():
    entry = change("a.txt", added=0, removed=0)
    text = view.format_file_diff(1, record(), manifest(), entry, sides(b"a\xff\n", b"a\xfe\n"))
    assert view.ENCODING_MARK in text
    assert "差异仅行尾" not in text
    assert "无变化" not in text


def test_diff_bounded_single_huge_line():
    huge = b"x" * (2 * 1024 * 1024)
    entry = change("huge.txt")
    text = view.format_file_diff(1, record(), manifest(), entry, sides(huge, (b"y" * 100) + huge))
    lines = lines_of(text)
    assert "行截断 +" in text
    assert len(lines) <= view.MAX_OUTPUT_LINES
    assert len(text) <= view.MAX_OUTPUT_CHARS
    assert all(len(line) <= view.MAX_LINE_CHARS + 40 for line in lines)


def test_diff_change_outside_preview_reports_unshown_range():
    before = b"".join(f"line {index:04d}\n".encode() for index in range(2100))
    after = before.replace(b"line 2050\n", b"line 2999\n")
    entry = change("long.txt")
    text = view.format_file_diff(1, record(), manifest(), entry, sides(before, after))
    assert view.LONG_PREVIEW_MARK in text
    assert view.RANGE_MARK in text
    assert "line 2999" not in text
    assert len(lines_of(text)) <= view.MAX_OUTPUT_LINES


def test_diff_total_output_is_capped_without_degrading_to_coarse():
    before = b"".join(f"a{index}\n".encode() for index in range(1900))
    after = b"".join(f"b{index}\n".encode() for index in range(1900))
    entry = change("big.txt", added=1900, removed=1900)
    text = view.format_file_diff(1, record(), manifest(), entry, sides(before, after))
    assert view.OUTPUT_TRUNCATION_MARK in text
    assert view.COARSE_MARK not in text
    assert len(lines_of(text)) <= view.MAX_OUTPUT_LINES
    assert len(text) <= view.MAX_OUTPUT_CHARS


def test_diff_identical_bytes_without_visible_change_is_not_a_claim():
    entry = change("a.txt", added=0, removed=0)
    text = view.format_file_diff(1, record(), manifest(), entry, sides(b"same\n", b"same\n"))
    assert lines_of(text)[0] == "回合 @1 · a.txt（+0 \u22120）"
    assert "无变化" not in text


def test_diff_ro_guards_stay_read_only():
    entry = change("a.txt")
    assert "淘汰" in view.format_file_diff(1, record(available=False), None, entry, sides())
    assert view.format_file_diff(1, record(empty=True, dir_name=None), None, entry, sides()) == (
        "@1 回合没有可展示的改动。"
    )
