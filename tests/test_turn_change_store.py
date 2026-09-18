"""Behavior tests for the turn-change snapshot store (M1 · Subagent A).

Source of truth: ``docs/superpowers/plans/2026-09-18-turn-change-ledger-m1.md``
§3.2 (behavior requirements 1-10) and §4 TA (required coverage 1-16).

Every test injects a fake differ, a controllable clock and ``tmp_path``; no
real time, no network, no git, no subprocess.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from agent.runtime import turn_change_store as store
from agent.runtime.turn_diff import (
    DEFAULT_MAX_FILE_BYTES,
    DIFF_COARSE,
    DIFF_FULL,
    DIFF_NONE,
    REASON_BINARY,
    REASON_CANCELLED,
    REASON_TIMEOUT,
    DiffStats,
)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeClock:
    """Controllable ``time.monotonic`` replacement (seconds, monotonic)."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, milliseconds: float) -> None:
        self.now += milliseconds / 1000.0


@dataclass
class DifferCall:
    before: bytes | None
    after: bytes | None
    max_bytes: int
    deadline: float | None
    cancelled: Callable[[], bool] | None


def _line_count(data: bytes | None) -> int:
    if not data:
        return 0
    return len(data.split(b"\n")) - (1 if data.endswith(b"\n") else 0)


class FakeDiffer:
    """Deterministic stand-in for ``turn_diff.diff_bytes`` that records calls."""

    def __init__(
        self,
        *,
        clock: FakeClock | None = None,
        advance_ms: float = 0.0,
        result: DiffStats | None = None,
    ) -> None:
        self.clock = clock
        self.advance_ms = advance_ms
        self.result = result
        self.calls: list[DifferCall] = []

    def __call__(
        self,
        before: bytes | None,
        after: bytes | None,
        *,
        max_bytes: int = DEFAULT_MAX_FILE_BYTES,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> DiffStats:
        self.calls.append(DifferCall(before, after, max_bytes, deadline, cancelled))
        if self.clock is not None:
            self.clock.advance(self.advance_ms)
        if self.result is not None:
            return self.result
        return DiffStats(
            added=_line_count(after),
            removed=_line_count(before),
            quality=DIFF_FULL,
            reason="",
            truncated=False,
        )


def make_store(
    tmp_path: Path,
    session_id: str = "sess-1",
    *,
    differ: Callable[..., DiffStats] | None = None,
    clock: FakeClock | None = None,
    limits: store.TurnChangeLimits | None = None,
    root: Path | None = None,
) -> store.TurnChangeStore:
    return store.TurnChangeStore(
        tmp_path,
        session_id,
        limits=limits,
        root=root,
        differ=differ if differ is not None else FakeDiffer(),
        clock=clock if clock is not None else FakeClock(),
    )


def session_dir(tmp_path: Path, session_id: str = "sess-1") -> Path:
    return tmp_path / ".astra" / "turn-changes" / session_id


def entries_by_path(manifest: store.TurnChangesManifest) -> dict[str, store.FileChange]:
    return {change.path: change for change in [*manifest.files, *manifest.unknown]}


# ---------------------------------------------------------------------------
# 1. lifecycle
# ---------------------------------------------------------------------------

def test_empty_turn_seals_to_none(tmp_path: Path) -> None:
    subject = make_store(tmp_path)

    subject.begin_turn("req-1")

    assert subject.seal() is None


def test_seal_without_active_turn_is_a_no_op(tmp_path: Path) -> None:
    subject = make_store(tmp_path)

    assert subject.seal() is None


def test_repeated_begin_turn_raises(tmp_path: Path) -> None:
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    with pytest.raises(RuntimeError):
        subject.begin_turn("req-2")


def test_begin_turn_after_seal_is_legal(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"new\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")
    subject.note_capture("a.txt", b"old\n")
    assert subject.seal() is not None

    subject.begin_turn("req-2")
    subject.note_capture("a.txt", b"old\n")

    assert subject.seal() is not None


@pytest.mark.parametrize(
    "note",
    [
        lambda subject: subject.note_paths(["a.txt"]),
        lambda subject: subject.note_capture("a.txt", b"x\n"),
        lambda subject: subject.note_capture("a.txt", b"x\n", checkpoint_id="cp1"),
        lambda subject: subject.note_absent("a.txt"),
        lambda subject: subject.note_tracked("a.txt", "file:1", "file:2"),
    ],
)
def test_note_without_active_turn_raises(tmp_path: Path, note) -> None:
    subject = make_store(tmp_path)

    with pytest.raises(RuntimeError):
        note(subject)


# ---------------------------------------------------------------------------
# 2. capture dedup / mutual exclusion
# ---------------------------------------------------------------------------

def test_note_capture_keeps_only_the_first_snapshot(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"final\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    subject.note_capture("a.txt", b"first\n", checkpoint_id="cp1")
    subject.note_capture("a.txt", b"second\n", checkpoint_id="cp2")

    manifest = subject.seal()
    assert manifest is not None
    change = entries_by_path(manifest)["a.txt"]
    assert (change.state, change.compare) == (store.STATE_MODIFIED, store.COMPARE_FULL)
    assert change.checkpoint_ids == ["cp1", "cp2"]


def test_note_capture_wins_over_a_later_absent(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"new\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    subject.note_capture("a.txt", b"old\n")
    subject.note_absent("a.txt")

    manifest = subject.seal()
    assert manifest is not None
    change = entries_by_path(manifest)["a.txt"]
    assert change.before_state == store.SIDE_CAPTURED
    assert change.state == store.STATE_MODIFIED


def test_note_absent_wins_over_a_later_capture(tmp_path: Path) -> None:
    (tmp_path / "new.txt").write_bytes(b"fresh\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    subject.note_absent("new.txt", checkpoint_id="cp1")
    subject.note_capture("new.txt", b"ignored\n", checkpoint_id="cp2")

    manifest = subject.seal()
    assert manifest is not None
    change = entries_by_path(manifest)["new.txt"]
    assert (change.state, change.before_state) == (store.STATE_ADDED, store.SIDE_ABSENT)
    assert change.checkpoint_ids == ["cp1", "cp2"]


def test_candidate_path_with_a_snapshot_is_confirmed(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"new\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    subject.note_paths(["a.txt"])
    subject.note_capture("a.txt", b"old\n")

    manifest = subject.seal()
    assert manifest is not None
    assert [change.path for change in manifest.files] == ["a.txt"]
    assert manifest.unknown == []


# ---------------------------------------------------------------------------
# 4. five states at seal
# ---------------------------------------------------------------------------

def test_seal_reports_five_states(tmp_path: Path) -> None:
    (tmp_path / "mod.txt").write_bytes(b"new\n")
    (tmp_path / "same.txt").write_bytes(b"same\n")
    (tmp_path / "added.txt").write_bytes(b"fresh\n")
    (tmp_path / "candidate.txt").write_bytes(b"untracked\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    subject.note_capture("mod.txt", b"old\n")          # → modified
    subject.note_capture("same.txt", b"same\n")        # 改了又改回 → unchanged
    subject.note_absent("added.txt")                   # → added
    subject.note_capture("gone.txt", b"bye\n")         # 快照存在、文件已删 → deleted
    subject.note_paths(["candidate.txt"])              # 仅候选 → unknown

    manifest = subject.seal()
    assert manifest is not None
    confirmed = {change.path: change for change in manifest.files}
    assert set(confirmed) == {"mod.txt", "added.txt", "gone.txt"}
    assert confirmed["mod.txt"].state == store.STATE_MODIFIED
    assert confirmed["added.txt"].state == store.STATE_ADDED
    assert confirmed["gone.txt"].state == store.STATE_DELETED
    assert "same.txt" not in entries_by_path(manifest)

    unknown = {change.path: change for change in manifest.unknown}
    assert set(unknown) == {"candidate.txt"}
    entry = unknown["candidate.txt"]
    assert entry.state == store.STATE_UNKNOWN
    assert entry.before_state == store.SIDE_UNCAPTURED
    assert entry.after_state == store.SIDE_CAPTURED
    assert (entry.compare, entry.added, entry.removed) == (store.COMPARE_NONE, None, None)

    assert manifest.totals["files"] == 3
    assert manifest.session_id == "sess-1"
    assert manifest.request_id == "req-1"


def test_added_and_deleted_use_the_absent_side_for_counts(tmp_path: Path) -> None:
    (tmp_path / "added.txt").write_bytes(b"one\ntwo\n")
    differ = FakeDiffer()
    subject = make_store(tmp_path, differ=differ)
    subject.begin_turn("req-1")

    subject.note_absent("added.txt")
    subject.note_capture("deleted.txt", b"gone\n")

    manifest = subject.seal()
    assert manifest is not None
    added = {change.path: change for change in manifest.files}["added.txt"]
    deleted = {change.path: change for change in manifest.files}["deleted.txt"]
    assert (added.added, added.removed, added.compare) == (2, 0, store.COMPARE_FULL)
    assert (deleted.added, deleted.removed, deleted.compare) == (0, 1, store.COMPARE_FULL)
    assert [(call.before, call.after) for call in differ.calls] == [
        (None, b"one\ntwo\n"),
        (b"gone\n", None),
    ]


def test_unchanged_paths_are_never_reported(tmp_path: Path) -> None:
    (tmp_path / "same.txt").write_bytes(b"same\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")
    subject.note_capture("same.txt", b"same\n")

    assert subject.seal() is None


def test_trailing_newline_change_is_a_byte_modification(tmp_path: Path) -> None:
    """R9：净状态由存在性和字节决定；零行差只是展示数据."""
    (tmp_path / "note.txt").write_bytes(b"hello\n")
    subject = store.TurnChangeStore(tmp_path, "sess-1")  # 真实 turn_diff.diff_bytes
    subject.begin_turn("req-1")
    subject.note_capture("note.txt", b"hello")

    manifest = subject.seal()
    assert manifest is not None
    change = manifest.files[0]
    assert change.state == store.STATE_MODIFIED
    assert change.compare == store.COMPARE_FULL
    assert (change.added, change.removed) == (0, 0)
    assert manifest.totals["files"] == 1


def test_unchanged_entries_stay_out_of_the_main_list(tmp_path: Path) -> None:
    """R9：unchanged 不进主清单也不进未知区，主清单只收净变化条目."""
    (tmp_path / "same.txt").write_bytes(b"same\n")
    (tmp_path / "mod.txt").write_bytes(b"new\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")
    subject.note_capture("same.txt", b"same\n")  # 改了又改回
    subject.note_capture("mod.txt", b"old\n")

    manifest = subject.seal()
    assert manifest is not None
    assert [change.path for change in manifest.files] == ["mod.txt"]
    assert manifest.unknown == []
    assert manifest.totals["files"] == 1


def test_uncaptured_side_lands_in_the_unknown_area(tmp_path: Path) -> None:
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    # 只作为"被触碰候选"登记的路径：before 侧从未取得快照 → 不得当作已确认
    subject.note_paths(["never-captured.txt"])

    manifest = subject.seal()
    assert manifest is not None
    assert manifest.files == []
    change = manifest.unknown[0]
    assert change.path == "never-captured.txt"
    assert change.state == store.STATE_UNKNOWN
    assert change.before_state == store.SIDE_UNCAPTURED
    assert change.after_state == store.SIDE_ABSENT
    assert change.reason == store.REASON_ERROR


# ---------------------------------------------------------------------------
# 5. note_tracked decision table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("before_fp", "after_fp", "expected"),
    [
        # 任一侧 error: 前缀 → unknown
        ("error: not a git repository", "file:1", store.STATE_UNKNOWN),
        ("file:1", "error: index.lock exists", store.STATE_UNKNOWN),
        # 字面量 <clean>（防御）→ unknown
        ("<clean>", "file:1", store.STATE_UNKNOWN),
        ("file:1", "<clean>", store.STATE_UNKNOWN),
        # 两侧均非 None
        ("file:1", "file:1", None),                      # unchanged → 不出现
        ("missing", "file:2", store.STATE_MODIFIED),     # 重建
        ("missing", "symlink:2", store.STATE_MODIFIED),  # 重建（符号链接）
        ("file:1", "missing", store.STATE_DELETED),
        ("symlink:1", "missing", store.STATE_DELETED),
        ("file:1", "file:2", store.STATE_MODIFIED),
        ("file:1", "symlink:2", store.STATE_MODIFIED),
        # 一侧 None
        (None, "missing", store.STATE_DELETED),
        (None, "file:2", store.STATE_MODIFIED),
        (None, "symlink:2", store.STATE_MODIFIED),
        ("file:1", None, store.STATE_MODIFIED),
        # 冻结表：非 None → None 一律 modified（不细分子类）
        ("missing", None, store.STATE_MODIFIED),
    ],
)
def test_note_tracked_decision_table(
    tmp_path: Path,
    before_fp: str | None,
    after_fp: str | None,
    expected: str | None,
) -> None:
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")
    subject.note_tracked("agent/runtime/x.py", before_fp, after_fp)

    manifest = subject.seal()
    if expected is None:
        assert manifest is None
        return
    assert manifest is not None
    change = entries_by_path(manifest)["agent/runtime/x.py"]
    assert change.state == expected
    assert change.compare == store.COMPARE_NONE
    assert (change.added, change.removed) == (None, None)
    assert change.reason == store.REASON_TRACKED
    if expected == store.STATE_UNKNOWN:
        assert change in manifest.unknown
        assert manifest.files == []
    else:
        assert change in manifest.files
        assert manifest.unknown == []


def test_note_tracked_deleted_marks_the_after_side_absent(tmp_path: Path) -> None:
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")
    subject.note_tracked("agent/x.py", "file:1", "missing")

    manifest = subject.seal()
    assert manifest is not None
    change = manifest.files[0]
    assert change.after_state == store.SIDE_ABSENT
    assert change.before_state == store.SIDE_UNCAPTURED


def test_note_tracked_without_any_fingerprint_is_unknown(tmp_path: Path) -> None:
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")
    subject.note_tracked("agent/x.py", None, None)

    manifest = subject.seal()
    assert manifest is not None
    assert manifest.files == []
    assert manifest.unknown[0].state == store.STATE_UNKNOWN
    assert manifest.unknown[0].reason == store.REASON_TRACKED


def test_note_tracked_malformed_fingerprint_is_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    subject = make_store(tmp_path)

    with caplog.at_level(logging.WARNING, logger=store.__name__):
        subject.begin_turn("req-1")
        subject.note_tracked("agent/x.py", "file:1", "<clean>")
        manifest = subject.seal()

    assert manifest is not None
    assert manifest.unknown[0].state == store.STATE_UNKNOWN
    assert any("tracked" in record.getMessage() for record in caplog.records)


def test_snapshot_evidence_outranks_tracked_fingerprints(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"new\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    subject.note_tracked("a.txt", "file:1", "file:2")
    subject.note_capture("a.txt", b"old\n")

    manifest = subject.seal()
    assert manifest is not None
    change = manifest.files[0]
    assert change.compare == store.COMPARE_FULL
    assert change.reason == ""


def test_tracked_sequence_reverting_to_original_is_not_listed(tmp_path: Path) -> None:
    """R5：同一路径多次 tracked 取"首次 before + 末次 after"，改回原样=无净改动."""
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")
    subject.note_tracked("sample.py", "file:original", "file:changed")
    subject.note_tracked("sample.py", "file:changed", "file:original")

    assert subject.seal() is None


def test_tracked_chain_uses_first_before_and_last_after(tmp_path: Path) -> None:
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")
    subject.note_tracked("created.py", "missing", "file:1")
    subject.note_tracked("created.py", "file:1", "file:2")
    subject.note_tracked("dead.py", "file:1", "file:2")
    subject.note_tracked("dead.py", "file:2", "missing")
    subject.note_tracked("drift.py", "file:1", "file:2")
    subject.note_tracked("drift.py", "file:2", "file:3")

    manifest = subject.seal()
    assert manifest is not None
    changes = {change.path: change for change in manifest.files}
    assert changes["created.py"].state == store.STATE_MODIFIED  # missing → file:2
    assert changes["dead.py"].state == store.STATE_DELETED      # file:1 → missing
    assert changes["drift.py"].state == store.STATE_MODIFIED    # file:1 → file:3
    assert all(change.reason == store.REASON_TRACKED for change in changes.values())


def test_snapshot_evidence_outranks_a_repeated_tracked_chain(tmp_path: Path) -> None:
    """混合规则：字节快照优先于指纹；快照存在时 tracked 链不参与判定."""
    (tmp_path / "a.txt").write_bytes(b"new\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    subject.note_tracked("a.txt", "file:1", "file:2")
    subject.note_tracked("a.txt", "file:2", "missing")  # 单看链=deleted
    subject.note_capture("a.txt", b"old\n")

    manifest = subject.seal()
    assert manifest is not None
    change = manifest.files[0]
    assert change.state == store.STATE_MODIFIED
    assert change.compare == store.COMPARE_FULL
    assert change.reason == ""


# ---------------------------------------------------------------------------
# path normalization and the per-turn path budget
# ---------------------------------------------------------------------------

def test_paths_are_normalized_relative_to_the_workspace(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_bytes(b"new\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    subject.note_capture(str(tmp_path / "sub" / "a.txt"), b"old\n", checkpoint_id="cp1")

    manifest = subject.seal()
    assert manifest is not None
    change = manifest.files[0]
    assert change.path == "sub/a.txt"
    assert change.display == "sub/a.txt"


def test_relative_and_absolute_paths_deduplicate(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"new\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    subject.note_paths(["a.txt"])
    subject.note_capture(str((tmp_path / "a.txt").resolve()), b"old\n")

    manifest = subject.seal()
    assert manifest is not None
    assert [change.path for change in manifest.files] == ["a.txt"]


def test_capture_target_outside_the_workspace_uses_the_stable_identity(tmp_path: Path) -> None:
    """R4：display 只是展示名；before/after 以传入的稳定绝对路径为目标."""
    files = tmp_path / "files"
    sandbox = tmp_path / "sandbox"
    files.mkdir()
    sandbox.mkdir()
    (files / "note.txt").write_bytes(b"after\n")
    (sandbox / "note.txt").write_bytes(b"UNRELATED\n")
    subject = make_store(sandbox, "review")
    subject.begin_turn("req-1")

    subject.note_capture(str(files / "note.txt"), b"before\n", display="note.txt")

    manifest = subject.seal()
    assert manifest is not None
    change = manifest.files[0]
    assert (change.path, change.display) == ("note.txt", "note.txt")
    assert change.state == store.STATE_MODIFIED
    assert subject.load_sides(0, "note.txt").after == b"after\n"


def test_target_directory_swap_degrades_the_after_read(tmp_path: Path) -> None:
    """R4/R1：目标目录被换成符号链接时，after 不跟随替换路径，安全降级."""
    workspace = tmp_path / "files"
    workspace.mkdir()
    (workspace / "note.txt").write_bytes(b"before\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "note.txt").write_bytes(b"SWAPPED\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")
    subject.note_capture(str(workspace / "note.txt"), b"before\n", display="note.txt")

    workspace.rename(tmp_path / "files-held")
    workspace.symlink_to(elsewhere)

    manifest = subject.seal()
    assert manifest is not None
    assert manifest.files == []
    change = manifest.unknown[0]
    assert (change.path, change.state) == ("note.txt", store.STATE_UNKNOWN)
    assert change.before_state == store.SIDE_CAPTURED
    assert change.after_state == store.SIDE_UNCAPTURED
    assert change.reason == store.REASON_ERROR
    assert subject.load_sides(0, "note.txt").after is None


def test_path_budget_truncates_candidates(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    limits = store.TurnChangeLimits(max_paths_per_turn=2)
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_bytes(b"x\n")
    subject = make_store(tmp_path, limits=limits)

    with caplog.at_level(logging.WARNING, logger=store.__name__):
        subject.begin_turn("req-1")
        subject.note_paths(["a.txt", "b.txt", "c.txt"])
        manifest = subject.seal()

    assert manifest is not None
    assert {change.path for change in manifest.unknown} == {"a.txt", "b.txt"}
    assert any("path" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# 6. persistence and read-back (layout, manifest, load_sides, FIFO, close)
# ---------------------------------------------------------------------------

def seal_modified_turn(
    subject: store.TurnChangeStore,
    tmp_path: Path,
    *,
    name: str = "a.txt",
    before: bytes = b"old\n",
    after: bytes = b"new\n",
    request_id: str = "req-1",
) -> store.TurnChangesManifest:
    (tmp_path / name).write_bytes(after)
    subject.begin_turn(request_id)
    subject.note_capture(name, before, checkpoint_id=f"cp-{name}")
    manifest = subject.seal()
    assert manifest is not None
    return manifest


def test_turn_directory_layout_and_owner_marker(tmp_path: Path) -> None:
    subject = make_store(tmp_path, "sess-1")

    manifest = seal_modified_turn(subject, tmp_path)

    area = session_dir(tmp_path)
    turn = area / "turn-1"
    assert sorted(path.name for path in turn.iterdir()) == [
        "after.0.bin",
        "before.0.bin",
        "manifest.json",
    ]
    assert (turn / "before.0.bin").read_bytes() == b"old\n"
    assert (turn / "after.0.bin").read_bytes() == b"new\n"
    # owner.json 位于会话目录（且先于任何快照内容创建）
    assert sorted(path.name for path in area.iterdir()) == ["owner.json", "turn-1"]
    owner = json.loads((area / "owner.json").read_text(encoding="utf-8"))
    assert owner["session_id"] == "sess-1"
    assert owner["pid"] == os.getpid()
    payload = json.loads((turn / "manifest.json").read_text(encoding="utf-8"))
    assert payload["session_id"] == "sess-1"
    assert payload["request_id"] == "req-1"
    assert payload["turn_seq"] == 1
    assert payload["totals"] == manifest.totals


def test_only_captured_sides_are_persisted(tmp_path: Path) -> None:
    subject = make_store(tmp_path, "sess-1")
    (tmp_path / "created.txt").write_bytes(b"fresh\n")
    subject.begin_turn("req-1")
    subject.note_absent("created.txt")
    assert subject.seal() is not None

    turn = session_dir(tmp_path) / "turn-1"

    assert sorted(path.name for path in turn.iterdir()) == ["after.0.bin", "manifest.json"]


def test_manifest_and_load_sides_read_only_the_snapshot_area(tmp_path: Path) -> None:
    subject = make_store(tmp_path, "sess-1")
    seal_modified_turn(subject, tmp_path)
    (tmp_path / "a.txt").write_bytes(b"edited after seal\n")  # seal 后外部再编辑

    manifest = subject.manifest(0)
    assert manifest is not None
    assert [change.path for change in manifest.files] == ["a.txt"]
    assert manifest.request_id == "req-1"
    sides = subject.load_sides(0, "a.txt")
    assert (sides.before, sides.after) == (b"old\n", b"new\n")
    assert (sides.before_state, sides.after_state) == (store.SIDE_CAPTURED, store.SIDE_CAPTURED)

    (tmp_path / "a.txt").unlink()  # 快照区自足：不再读当前磁盘

    assert subject.load_sides(0, "a.txt").after == b"new\n"
    assert subject.manifest(1) is None


def test_manifest_round_trips_the_unknown_area(tmp_path: Path) -> None:
    subject = make_store(tmp_path, "sess-1")
    (tmp_path / "real.txt").write_bytes(b"new\n")
    subject.begin_turn("req-1")
    subject.note_capture("real.txt", b"old\n")
    subject.note_tracked("ghost.py", "file:1", "error: bad revision")

    manifest = subject.seal()
    reread = subject.manifest(0)

    assert manifest is not None
    assert reread is not None
    assert reread == manifest
    assert [change.path for change in reread.files] == ["real.txt"]
    assert [change.path for change in reread.unknown] == ["ghost.py"]
    assert reread.unknown[0].reason == store.REASON_TRACKED
    assert reread.unknown[0].state == store.STATE_UNKNOWN


def test_manifest_offsets_follow_turn_seq_order(tmp_path: Path) -> None:
    subject = make_store(tmp_path, "sess-1")
    for index in range(2):
        (tmp_path / "a.txt").write_bytes(f"after-{index}\n".encode())
        subject.begin_turn(f"req-{index}")
        subject.note_capture("a.txt", f"before-{index}\n".encode())
        assert subject.seal() is not None

    newest = subject.manifest(0)
    older = subject.manifest(1)

    assert newest is not None
    assert older is not None
    assert (newest.turn_seq, older.turn_seq) == (2, 1)
    assert (newest.request_id, older.request_id) == ("req-1", "req-0")
    assert subject.load_sides(1, "a.txt").after == b"after-0\n"
    assert subject.manifest(2) is None
    assert subject.manifest(-1) is None


def test_load_sides_three_states(tmp_path: Path) -> None:
    subject = make_store(tmp_path, "sess-1")
    (tmp_path / "created.txt").write_bytes(b"fresh\n")
    subject.begin_turn("req-1")
    subject.note_absent("created.txt")
    assert subject.seal() is not None

    created = subject.load_sides(0, "created.txt")
    assert (created.before, created.before_state) == (None, store.SIDE_ABSENT)
    assert (created.after, created.after_state) == (b"fresh\n", store.SIDE_CAPTURED)

    seal_modified_turn(subject, tmp_path, name="edited.txt", before=b"old\n", after=b"new\n", request_id="req-2")
    assert subject.load_sides(0, "edited.txt").after == b"new\n"

    (session_dir(tmp_path) / "turn-2" / "after.0.bin").unlink()  # 快照文件被删 = 读取失败

    broken = subject.load_sides(0, "edited.txt")
    assert (broken.after, broken.after_state) == (None, store.SIDE_UNCAPTURED)
    assert (broken.before, broken.before_state) == (b"old\n", store.SIDE_CAPTURED)
    assert subject.load_sides(9, "edited.txt") == store.LoadedSides(
        None, None, store.SIDE_UNCAPTURED, store.SIDE_UNCAPTURED
    )
    assert subject.load_sides(0, "no-such-path.txt").before_state == store.SIDE_UNCAPTURED


def test_fifo_retains_only_the_newest_turns(tmp_path: Path) -> None:
    limits = store.TurnChangeLimits(max_turns_retained=3)
    subject = make_store(tmp_path, "sess-1", limits=limits)
    for index in range(5):
        (tmp_path / "a.txt").write_bytes(f"after-{index}\n".encode())
        subject.begin_turn(f"req-{index}")
        subject.note_capture("a.txt", f"before-{index}\n".encode())
        assert subject.seal() is not None

    retained = sorted(path.name for path in session_dir(tmp_path).iterdir() if path.name.startswith("turn-"))

    assert retained == ["turn-3", "turn-4", "turn-5"]
    newest = subject.manifest(0)
    oldest_retained = subject.manifest(2)
    assert newest is not None
    assert oldest_retained is not None
    assert (newest.turn_seq, oldest_retained.turn_seq) == (5, 3)
    assert subject.manifest(3) is None
    assert subject.load_sides(2, "a.txt").before == b"before-2\n"


def test_close_removes_the_session_snapshot_area(tmp_path: Path) -> None:
    subject = make_store(tmp_path, "sess-1")
    seal_modified_turn(subject, tmp_path)

    subject.close()

    assert not session_dir(tmp_path).exists()
    assert subject.manifest(0) is None
    assert subject.load_sides(0, "a.txt").before is None


# ---------------------------------------------------------------------------
# 3. quota takes effect at capture time (never waits for seal)
# ---------------------------------------------------------------------------

def test_file_quota_degrades_at_capture_without_copying_bytes(tmp_path: Path) -> None:
    limits = store.TurnChangeLimits(max_file_bytes=16)
    big = b"x" * 64
    (tmp_path / "big.txt").write_bytes(b"small\n")
    subject = make_store(tmp_path, "sess-1", limits=limits)
    subject.begin_turn("req-1")

    subject.note_capture("big.txt", big, checkpoint_id="cp1")
    manifest = subject.seal()

    assert manifest is not None
    assert manifest.files == []
    change = manifest.unknown[0]
    assert (change.state, change.before_state) == (store.STATE_UNKNOWN, store.SIDE_UNCAPTURED)
    assert change.reason == store.REASON_QUOTA
    assert (change.compare, change.added, change.removed) == (store.COMPARE_NONE, None, None)
    assert change.checkpoint_ids == ["cp1"]

    turn = session_dir(tmp_path) / "turn-1"
    assert sorted(path.name for path in turn.iterdir()) == ["after.0.bin", "manifest.json"]
    assert big not in (turn / "after.0.bin").read_bytes()


def test_turn_quota_degrades_later_captures(tmp_path: Path) -> None:
    limits = store.TurnChangeLimits(max_file_bytes=1024, max_turn_bytes=11)
    (tmp_path / "a.txt").write_bytes(b"x")
    (tmp_path / "b.txt").write_bytes(b"y")
    subject = make_store(tmp_path, "sess-1", limits=limits)
    subject.begin_turn("req-1")

    subject.note_capture("a.txt", b"01234567\n")  # 9 字节 ≤ 回合预算
    subject.note_capture("b.txt", b"01234567\n")  # 再 9 字节 → 超回合预算
    manifest = subject.seal()

    assert manifest is not None
    confirmed = {change.path: change for change in manifest.files}
    degraded = {change.path: change for change in manifest.unknown}
    assert set(confirmed) == {"a.txt"}
    assert confirmed["a.txt"].compare == store.COMPARE_FULL
    assert list(degraded) == ["b.txt"]
    assert degraded["b.txt"].before_state == store.SIDE_UNCAPTURED
    assert degraded["b.txt"].reason == store.REASON_QUOTA


def test_session_quota_evicts_the_oldest_turn_at_capture_time(tmp_path: Path) -> None:
    limits = store.TurnChangeLimits(
        max_file_bytes=1024,
        max_turn_bytes=4096,
        max_session_bytes=160,
        max_turns_retained=10,
    )
    subject = make_store(tmp_path, "sess-1", limits=limits)
    for index in (1, 2):
        seal_modified_turn(
            subject,
            tmp_path,
            before=b"A" * 32,
            after=b"B" * 32,
            request_id=f"req-{index}",
        )
    area = session_dir(tmp_path)

    (tmp_path / "a.txt").write_bytes(b"C" * 32)
    subject.begin_turn("req-3")
    subject.note_capture("a.txt", b"D" * 64)  # 会话预算不足 → 先淘汰最旧回合

    assert not (area / "turn-1").exists()
    assert (area / "turn-2").exists()

    manifest = subject.seal()

    assert manifest is not None
    assert manifest.files[0].compare == store.COMPARE_FULL  # 淘汰后可容纳 → 不降级
    assert (area / "turn-3").exists()


def test_session_quota_degrades_when_no_room_can_be_made(tmp_path: Path) -> None:
    limits = store.TurnChangeLimits(
        max_file_bytes=1024,
        max_turn_bytes=4096,
        max_session_bytes=64,
        max_turns_retained=10,
    )
    subject = make_store(tmp_path, "sess-1", limits=limits)
    seal_modified_turn(subject, tmp_path, before=b"A" * 8, after=b"B" * 8)
    area = session_dir(tmp_path)

    (tmp_path / "big.txt").write_bytes(b"C" * 8)
    subject.begin_turn("req-2")
    subject.note_capture("big.txt", b"D" * 96)  # 淘汰最旧回合后仍超会话预算

    assert not (area / "turn-1").exists()

    manifest = subject.seal()

    assert manifest is not None
    assert manifest.files == []
    change = manifest.unknown[0]
    assert change.path == "big.txt"
    assert change.reason == store.REASON_QUOTA
    assert change.before_state == store.SIDE_UNCAPTURED


def test_after_side_over_the_file_quota_lands_in_the_unknown_area(tmp_path: Path) -> None:
    limits = store.TurnChangeLimits(max_file_bytes=16)
    (tmp_path / "a.txt").write_bytes(b"y" * 64)
    subject = make_store(tmp_path, "sess-1", limits=limits)
    subject.begin_turn("req-1")

    subject.note_capture("a.txt", b"x\n")
    manifest = subject.seal()

    assert manifest is not None
    assert manifest.files == []
    change = manifest.unknown[0]
    assert change.before_state == store.SIDE_CAPTURED
    assert change.after_state == store.SIDE_UNCAPTURED
    assert change.reason == store.REASON_QUOTA
    turn = session_dir(tmp_path) / "turn-1"
    # 超限的 after 字节绝不落盘；已捕获的 before 仍保留
    assert sorted(path.name for path in turn.iterdir()) == ["before.0.bin", "manifest.json"]


# ---------------------------------------------------------------------------
# 11. failures only degrade
# ---------------------------------------------------------------------------

def test_seal_survives_a_manifest_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = make_store(tmp_path, "sess-1")
    (tmp_path / "a.txt").write_bytes(b"new\n")

    def boom(path, payload):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_json_atomic", boom)

    subject.begin_turn("req-1")
    subject.note_capture("a.txt", b"old\n")
    manifest = subject.seal()

    assert manifest is not None
    assert [change.path for change in manifest.files] == ["a.txt"]
    assert manifest.files[0].state == store.STATE_MODIFIED
    assert subject.manifest(0) is None  # 未落盘 → 读回为空，仅降级


def test_store_survives_an_unusable_snapshot_root(tmp_path: Path) -> None:
    root = tmp_path / "blocked"
    root.mkdir()
    (root / "sess-1").write_text("not a directory", encoding="utf-8")
    (tmp_path / "a.txt").write_bytes(b"new\n")

    subject = make_store(tmp_path, "sess-1", root=root)
    subject.begin_turn("req-1")
    subject.note_capture("a.txt", b"old\n")
    manifest = subject.seal()

    assert manifest is not None
    assert manifest.files[0].state == store.STATE_MODIFIED
    assert subject.manifest(0) is None


def test_manifest_read_failure_returns_none(tmp_path: Path) -> None:
    subject = make_store(tmp_path, "sess-1")
    seal_modified_turn(subject, tmp_path)
    (session_dir(tmp_path) / "turn-1" / "manifest.json").write_text("{not json", encoding="utf-8")

    assert subject.manifest(0) is None
    sides = subject.load_sides(0, "a.txt")
    assert (sides.before, sides.after) == (None, None)
    assert (sides.before_state, sides.after_state) == (store.SIDE_UNCAPTURED, store.SIDE_UNCAPTURED)


# ---------------------------------------------------------------------------
# 13. turn compute budget
# ---------------------------------------------------------------------------

def test_turn_compute_budget_degrades_unfinished_entries(tmp_path: Path) -> None:
    clock = FakeClock()
    differ = FakeDiffer(clock=clock, advance_ms=90)
    limits = store.TurnChangeLimits(diff_deadline_ms=50, compute_budget_ms=100)
    subject = make_store(tmp_path, "sess-1", differ=differ, clock=clock, limits=limits)
    subject.begin_turn("req-1")
    for index in range(5):
        name = f"f{index}.txt"
        (tmp_path / name).write_bytes(f"new-{index}\n".encode())
        subject.note_capture(name, f"old-{index}\n".encode())
    started = clock.now

    wall = time.perf_counter()
    manifest = subject.seal()
    elapsed = time.perf_counter() - wall

    assert manifest is not None
    assert elapsed < 2.0  # 注入的慢 differ 不得拖住收尾
    assert [change.path for change in manifest.files] == [f"f{i}.txt" for i in range(5)]
    assert len(differ.calls) == 2  # 超预算后不再发起新的 diff
    assert differ.calls[0].deadline == pytest.approx(started + 0.050)  # 单文件截止时间
    assert differ.calls[1].deadline == pytest.approx(started + 0.100)  # 取较早的回合截止时间
    assert all(call.max_bytes == limits.max_file_bytes for call in differ.calls)
    assert clock.now - started == pytest.approx(0.180)

    confirmed = [change for change in manifest.files if change.compare == store.COMPARE_FULL]
    degraded = [change for change in manifest.files if change.compare == store.COMPARE_NONE]
    assert len(confirmed) == 2
    assert len(degraded) == 3
    assert all(change.added is None and change.removed is None for change in degraded)
    assert all(change.reason == REASON_TIMEOUT for change in degraded)
    assert all(change.state == store.STATE_MODIFIED for change in degraded)


# ---------------------------------------------------------------------------
# 14. cancellation is forwarded to the differ and respected
# ---------------------------------------------------------------------------

def test_seal_forwards_the_cancel_callback_to_the_differ(tmp_path: Path) -> None:
    clock = FakeClock()
    state = {"cancelled": False}
    received: list[Callable[[], bool] | None] = []

    def differ(before, after, *, max_bytes=DEFAULT_MAX_FILE_BYTES, deadline=None, cancelled=None):
        received.append(cancelled)
        state["cancelled"] = True  # 首个文件算完后取消
        return DiffStats(added=1, removed=1, quality=DIFF_FULL, reason="", truncated=False)

    def cancel_flag() -> bool:
        return state["cancelled"]

    limits = store.TurnChangeLimits(diff_deadline_ms=60_000, compute_budget_ms=60_000)  # 长 deadline
    subject = make_store(tmp_path, "sess-1", differ=differ, clock=clock, limits=limits)
    subject.begin_turn("req-1")
    for index in range(3):
        name = f"f{index}.txt"
        (tmp_path / name).write_bytes(f"new-{index}\n".encode())
        subject.note_capture(name, f"old-{index}\n".encode())

    manifest = subject.seal(cancelled=cancel_flag)

    assert manifest is not None
    assert received and received[0] is cancel_flag  # 取消信号贯通给 differ
    assert len(received) == 1                       # 取消后不再发起新的 diff
    assert manifest.files[0].compare == store.COMPARE_FULL
    for change in manifest.files[1:]:
        assert (change.compare, change.added, change.removed) == (store.COMPARE_NONE, None, None)
        assert change.reason == REASON_CANCELLED
        assert change.state == store.STATE_MODIFIED


def test_cancel_before_any_diff_degrades_every_entry(tmp_path: Path) -> None:
    clock = FakeClock()
    differ = FakeDiffer(clock=clock)
    limits = store.TurnChangeLimits(diff_deadline_ms=60_000, compute_budget_ms=60_000)
    subject = make_store(tmp_path, "sess-1", differ=differ, clock=clock, limits=limits)
    subject.begin_turn("req-1")
    (tmp_path / "a.txt").write_bytes(b"new\n")
    subject.note_capture("a.txt", b"old\n")

    manifest = subject.seal(cancelled=lambda: True)

    assert manifest is not None
    assert differ.calls == []
    change = manifest.files[0]
    assert (change.compare, change.added, change.removed) == (store.COMPARE_NONE, None, None)
    assert change.reason == REASON_CANCELLED
    assert change.state == store.STATE_MODIFIED  # 存在性/字节已确认，只是无计数


def test_count_only_downgrades_by_the_differ(tmp_path: Path) -> None:
    subject = make_store(
        tmp_path,
        "sess-1",
        differ=FakeDiffer(
            result=DiffStats(
                added=None,
                removed=None,
                quality=DIFF_COARSE,
                reason=REASON_CANCELLED,
                truncated=False,
            )
        ),
    )
    subject.begin_turn("req-1")
    (tmp_path / "a.txt").write_bytes(b"new\n")
    subject.note_capture("a.txt", b"old\n")

    manifest = subject.seal()

    assert manifest is not None
    change = manifest.files[0]
    assert (change.compare, change.added, change.removed) == (store.COMPARE_COARSE, None, None)
    assert change.reason == REASON_CANCELLED


def test_binary_diff_result_stays_confirmed_without_counts(tmp_path: Path) -> None:
    subject = make_store(
        tmp_path,
        "sess-1",
        differ=FakeDiffer(
            result=DiffStats(
                added=None,
                removed=None,
                quality=DIFF_NONE,
                reason=REASON_BINARY,
                truncated=False,
            )
        ),
    )
    subject.begin_turn("req-1")
    (tmp_path / "a.bin").write_bytes(b"\x00\x01")
    subject.note_capture("a.bin", b"\x00\x02")

    manifest = subject.seal()

    assert manifest is not None
    assert manifest.unknown == []
    change = manifest.files[0]
    assert change.state == store.STATE_MODIFIED
    assert (change.compare, change.added, change.removed) == (store.COMPARE_NONE, None, None)
    assert change.reason == REASON_BINARY


def test_differ_exception_only_degrades_the_entry(tmp_path: Path) -> None:
    def boom(before, after, **kwargs):
        raise RuntimeError("differ exploded")

    subject = make_store(tmp_path, "sess-1", differ=boom)
    subject.begin_turn("req-1")
    (tmp_path / "a.txt").write_bytes(b"new\n")
    subject.note_capture("a.txt", b"old\n")

    manifest = subject.seal()

    assert manifest is not None
    change = manifest.files[0]
    assert change.state == store.STATE_MODIFIED
    assert (change.compare, change.added, change.removed) == (store.COMPARE_NONE, None, None)
    assert change.reason == store.REASON_ERROR


# ---------------------------------------------------------------------------
# 8. cleanup_orphans safety rules
# ---------------------------------------------------------------------------

def make_session_dir(root: Path, name: str, owner: str) -> Path:
    session = root / name
    (session / "turn-1").mkdir(parents=True)
    (session / "owner.json").write_text(owner, encoding="utf-8")
    return session


def test_cleanup_orphans_skips_live_owners(tmp_path: Path) -> None:
    root = tmp_path / "turn-changes"
    live = make_session_dir(root, "live", json.dumps({"session_id": "live", "pid": 4242}))

    removed = store.TurnChangeStore.cleanup_orphans(root, is_alive=lambda pid: pid == 4242)

    assert removed == []
    assert live.exists()
    assert (live / "turn-1").exists()


def test_cleanup_orphans_removes_dead_owners(tmp_path: Path) -> None:
    root = tmp_path / "turn-changes"
    dead = make_session_dir(root, "dead", json.dumps({"session_id": "dead", "pid": 99999}))
    live = make_session_dir(root, "live", json.dumps({"session_id": "live", "pid": 4242}))

    removed = store.TurnChangeStore.cleanup_orphans(root, is_alive=lambda pid: pid == 4242)

    assert removed == [str(dead)]
    assert not dead.exists()
    assert live.exists()


def test_cleanup_orphans_never_touches_unowned_directories(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "turn-changes"
    fresh = root / "just-created"          # 另一实例刚建目录、尚未写 owner
    (fresh / "turn-1").mkdir(parents=True)
    broken = root / "broken-owner"         # owner.json 不可解析
    broken.mkdir()
    (broken / "owner.json").write_text("{not json", encoding="utf-8")
    no_pid = root / "no-pid"               # owner.json 无 pid → 无法确认
    no_pid.mkdir()
    (no_pid / "owner.json").write_text(json.dumps({"session_id": "no-pid"}), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=store.__name__):
        removed = store.TurnChangeStore.cleanup_orphans(root, is_alive=lambda pid: False)

    assert removed == []
    assert fresh.exists()
    assert (fresh / "turn-1").exists()
    assert broken.exists()
    assert no_pid.exists()
    assert "owner" in caplog.text


def test_cleanup_orphans_default_probe_keeps_a_live_session(tmp_path: Path) -> None:
    root = tmp_path / "turn-changes"
    make_session_dir(root, "self", json.dumps({"session_id": "self", "pid": os.getpid()}))

    assert store.TurnChangeStore.cleanup_orphans(root) == []
    assert (root / "self").exists()


# ---------------------------------------------------------------------------
# 9. turn_store_scope / current_turn_change_store
# ---------------------------------------------------------------------------

def test_turn_store_scope_sets_nests_and_resets(tmp_path: Path) -> None:
    assert store.current_turn_change_store() is None
    outer = make_store(tmp_path, "outer")
    inner = make_store(tmp_path, "inner")

    with store.turn_store_scope(outer):
        assert store.current_turn_change_store() is outer
        with store.turn_store_scope(inner):
            assert store.current_turn_change_store() is inner
        assert store.current_turn_change_store() is outer
        with store.turn_store_scope(None):
            assert store.current_turn_change_store() is None
        assert store.current_turn_change_store() is outer

    assert store.current_turn_change_store() is None


def test_turn_store_scope_resets_after_an_exception(tmp_path: Path) -> None:
    outer = make_store(tmp_path, "outer")

    with pytest.raises(ValueError), store.turn_store_scope(outer):
        raise ValueError("boom")

    assert store.current_turn_change_store() is None


def test_locked_turn_eviction_stops_instead_of_looping(tmp_path: Path) -> None:
    """A locked FIFO delete must degrade, not spin forever (review R2).

    Runs in a subprocess: the pre-fix behavior was an infinite loop, which
    would also hang the test runner itself.
    """
    import json as _json
    import subprocess as _subprocess
    import sys as _sys
    import textwrap

    script = textwrap.dedent(
        """
        import json
        import sys
        from pathlib import Path

        import agent.runtime.turn_change_store as tcs

        root = Path(sys.argv[1])
        calls = 0

        def failing_rmtree(*args, **kwargs):
            global calls
            calls += 1
            raise PermissionError("simulated locked snapshot directory")

        tcs.shutil.rmtree = failing_rmtree

        store = tcs.TurnChangeStore(
            root, "review", root=root / "ledger",
            limits=tcs.TurnChangeLimits(max_turns_retained=1),
        )
        second_manifest = None
        for index in range(2):
            target = root / f"file-{index}.txt"
            target.write_bytes(b"new\\n")
            store.begin_turn(f"turn-{index}")
            store.note_absent(target.name)
            second_manifest = store.seal()
        print(json.dumps({
            "done": True,
            "delete_calls": calls,
            "second_manifest_persisted": second_manifest is not None,
        }))
        """
    )
    completed = _subprocess.run(
        [_sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert completed.returncode == 0, completed.stderr
    payload = _json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["done"] is True
    # The deletion fault was exercised, yet eviction stopped after a bounded
    # number of attempts instead of looping on the same directory.
    assert 1 <= payload["delete_calls"] <= 4
    assert payload["second_manifest_persisted"] is True


def test_store_refuses_a_swapped_workspace_symlink(tmp_path: Path) -> None:
    """Constructing on a symlinked workspace refuses instead of following it.

    That is the shape of the audit path-swap attack (review R1): once the
    final component is a symbolic link, the ledger must degrade rather than
    write through it.
    """
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(OSError):
        store.TurnChangeStore(linked, "review", root=tmp_path / "ledger")


def test_store_stops_writing_after_the_workspace_is_swapped(tmp_path: Path) -> None:
    """A workspace swap after construction must not redirect writes (R1)."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    ledger = store.TurnChangeStore(workspace, "review")

    ledger.begin_turn("t1")
    (workspace / "note.txt").write_bytes(b"after\n")
    ledger.note_capture("note.txt", b"before\n")

    held = tmp_path / "held"
    workspace.rename(held)
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace.symlink_to(outside, target_is_directory=True)

    ledger.seal()
    assert not (outside / ".astra").exists()

    ledger.close()
    assert not (outside / ".astra").exists()
