"""Session-owned turn-change snapshot area. Independent of file checkpoints;
never enters model requests.

M1 interface contract (frozen 2026-09-18; see
docs/superpowers/plans/2026-09-18-turn-change-ledger-m1.md §3.2).

Behavior notes that the frozen contract left to this module (all are covered
by ``tests/test_turn_change_store.py``):

- 主清单（``files``）只收"存在性/字节已确认"的条目；任一侧 ``uncaptured``
  一律进 ``unknown`` 区。超时/取消只降级计数（``compare=none``、
  ``added/removed=None``、``reason=timeout|cancelled``），条目仍留在主清单。
- ``note_paths`` 仅登记候选：从未取得快照的候选路径进 ``unknown``（reason=error），
  绝不作为已确认改动出现。
- tracked 行只有指纹、没有字节：``missing`` 侧记 ``absent``，有指纹侧记
  ``uncaptured``；确认变化进主清单且 ``compare=none``/``reason=tracked``。
  字节快照优先于指纹。
- 配额在捕获阶段生效（单文件/回合/会话），会话超量先 FIFO 淘汰最旧回合；
  任何 I/O 或 differ 失败只降级、不抛错。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import turn_diff
from .process_env import pid_alive
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

logger = logging.getLogger(__name__)

# 落盘布局（§3.2 行为要求 5）
MANIFEST_NAME = "manifest.json"
OWNER_NAME = "owner.json"
DEFAULT_ROOT_PARTS = (".astra", "turn-changes")

_SAFE_SESSION_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_TRACKED_CONTENT_PREFIXES = ("file:", "symlink:")
_TRACKED_MISSING = "missing"
_TRACKED_DEFENSIVE = "<clean>"
_TRACKED_ERROR_PREFIX = "error:"

_CURRENT: ContextVar["TurnChangeStore | None"] = ContextVar("astra_turn_change_store", default=None)


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


@dataclass
class _PathEntry:
    """One touched path inside the active turn (mutable turn-scoped state)."""

    path: str
    resolved: Path
    # 目标目录身份（读取 after 前核验，防止跟随被替换的目录；R1/R4）
    anchor: tuple[int, int] | None = None
    before_state: str = ""            # "" = 尚未登记（仅候选）；SIDE_* = 已登记
    before_reason: str = ""
    before: bytes | None = None
    tracked: tuple[str | None, str | None] | None = None
    checkpoint_ids: list[str] = field(default_factory=list)


@dataclass
class _ResolvedEntry:
    change: FileChange
    before_bytes: bytes | None = None
    after_bytes: bytes | None = None


def _safe_session_name(session_id: str) -> str:
    """Deterministic, filesystem-safe directory name for one session id."""
    safe = _SAFE_SESSION_CHARS.sub("_", session_id).strip("._-") or "session"
    if safe == session_id and len(safe) <= 80:
        return safe
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8]
    return f"{safe[:80]}-{digest}"


def _tracked_has_content(fingerprint: str | None) -> bool:
    return fingerprint is not None and fingerprint.startswith(_TRACKED_CONTENT_PREFIXES)


def _tracked_is_unusable(fingerprint: str | None) -> bool:
    return fingerprint is not None and (
        fingerprint.startswith(_TRACKED_ERROR_PREFIX) or fingerprint == _TRACKED_DEFENSIVE
    )


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
        raw_workspace = Path(workspace).expanduser()
        if not raw_workspace.is_absolute():
            raw_workspace = Path.cwd() / raw_workspace
        # Refuse a swapped final symlink; identity is re-checked before every
        # write so a later swap degrades instead of escaping the workspace (R1).
        self._workspace_raw = raw_workspace
        self._anchor_identity = _open_dir_anchor(raw_workspace)
        self.workspace = raw_workspace.resolve()
        self.session_id = str(session_id)
        self.limits = limits if limits is not None else TurnChangeLimits()
        self.root = (
            Path(root).expanduser().resolve()
            if root is not None
            else self.workspace.joinpath(*DEFAULT_ROOT_PARTS)
        )
        self.session_dir = self.root / _safe_session_name(self.session_id)
        self._differ = differ
        self._clock = clock
        self._lock = threading.RLock()
        self._active: dict[str, _PathEntry] | None = None
        self._request_id = ""
        self._turn_bytes = 0
        self._truncated = False
        # 会话目录创建后、写入任何快照内容前，立即写 owner.json（行为要求 10）
        self._ensure_session_dir()

    # -- 回合生命周期（react 主 agent 接线） --

    def begin_turn(self, request_id: str) -> None:
        with self._lock:
            if self._active is not None:
                raise RuntimeError(
                    "turn-change store: a turn is already active; seal it before begin_turn"
                )
            self._active = {}
            self._request_id = str(request_id)
            self._turn_bytes = 0
            self._truncated = False
            self._ensure_session_dir()

    def note_paths(self, paths: Iterable[str]) -> None:
        with self._lock:
            for raw_path in paths:
                self._register(raw_path)

    def note_capture(
        self,
        path: str | os.PathLike[str],
        before: bytes,
        *,
        checkpoint_id: str = "",
        display: str | None = None,
    ) -> None:
        """登记首份 before 字节；``display`` 为展示名（默认相对 store 工作区）.

        ``path`` 是可信的稳定身份：传绝对路径（如文件策略根解析出的
        ``captured.path``）时，before/after 都以它为目标（review R4）。
        """
        with self._lock:
            entry = self._register(path, display=display)
            if entry is None:
                return
            self._remember_checkpoint(entry, checkpoint_id)
            if entry.before_state:
                return  # 每文件每回合只存首份；首份语义即定
            data = bytes(before)
            state, reason = self._accept_snapshot(len(data))
            if state == SIDE_CAPTURED:
                entry.before = data
                entry.before_state = SIDE_CAPTURED
            else:
                # 配额在捕获阶段即生效：不复制该字节，记 uncaptured + quota
                entry.before_state = SIDE_UNCAPTURED
                entry.before_reason = reason

    def note_absent(
        self,
        path: str | os.PathLike[str],
        *,
        checkpoint_id: str = "",
        display: str | None = None,
    ) -> None:
        with self._lock:
            entry = self._register(path, display=display)
            if entry is None:
                return
            self._remember_checkpoint(entry, checkpoint_id)
            if entry.before_state:
                return  # 与 note_capture 互斥（新建 vs 修改）
            entry.before_state = SIDE_ABSENT

    def note_tracked(self, path: str, before_fp: str | None, after_fp: str | None) -> None:
        """登记一条 git 指纹变更；同一路径多次登记保留"首次 before + 末次 after".

        混合规则：字节快照（note_capture/note_absent）优先于指纹。同一路径既有
        快照又有 tracked 时，判定只走快照（首份 before 字节 vs 封存时读取），
        tracked 链不参与，登记顺序不影响判定。
        """
        with self._lock:
            entry = self._register(path)
            if entry is None:
                return
            if entry.tracked is None:
                entry.tracked = (before_fp, after_fp)
            else:
                # 回合首尾净变化语义：命令序列多次触碰同一路径时，比较首次
                # before 与最后一次 after（改回原样/删除后重建都取净结果）。
                entry.tracked = (entry.tracked[0], after_fp)

    def seal(self, *, cancelled: Callable[[], bool] | None = None) -> TurnChangesManifest | None:
        """净判定 + 配额 + 落盘 + FIFO；空回合返回 None。

        每文件 ``deadline = min(now + diff_deadline_ms, 回合截止时间)``；``cancelled``
        原样贯通给 differ。超预算/取消的未完成条目只降级计数，``seal`` 不抛错。
        """
        with self._lock:
            active = self._active
            if active is None:
                return None
            request_id = self._request_id
            self._active = None
            self._request_id = ""
            deadline = self._clock() + self.limits.compute_budget_ms / 1000.0
        resolved: list[_ResolvedEntry] = []
        for entry in active.values():
            outcome = self._resolve(entry, deadline, cancelled)
            if outcome is None:
                continue
            if outcome.change.state == STATE_UNCHANGED:
                # 净变化为零：主清单与未知区都不出现（行为要求 3，review R9）
                continue
            resolved.append(outcome)
        if not resolved:
            return None
        files = [
            item.change
            for item in resolved
            if item.change.state not in (STATE_UNKNOWN, STATE_UNCHANGED)
        ]
        unknown = [item.change for item in resolved if item.change.state == STATE_UNKNOWN]
        totals = {
            "files": len(files),
            "added": sum(change.added for change in files if change.added is not None),
            "removed": sum(change.removed for change in files if change.removed is not None),
        }
        manifest = TurnChangesManifest(
            session_id=self.session_id,
            request_id=request_id,
            turn_seq=self._next_seq(),
            created_at=self._clock(),
            files=files,
            unknown=unknown,
            totals=totals,
        )
        self._persist(manifest, resolved)
        return manifest

    # -- 读取（M2/M3 用；M1 供测试） --

    def manifest(self, turn_offset: int = 0) -> TurnChangesManifest | None:
        with self._lock:
            turn_dir = self._turn_dir(turn_offset)
            if turn_dir is None:
                return None
            payload = self._read_manifest_payload(turn_dir)
            if payload is None:
                return None
            totals = payload.get("totals")
            return TurnChangesManifest(
                session_id=str(payload.get("session_id") or ""),
                request_id=str(payload.get("request_id") or ""),
                turn_seq=int(payload.get("turn_seq") or 0),
                created_at=float(payload.get("created_at") or 0.0),
                files=[_change_from_payload(item) for item in _payload_list(payload.get("files"))],
                unknown=[_change_from_payload(item) for item in _payload_list(payload.get("unknown"))],
                totals=(
                    {str(key): int(value) for key, value in totals.items()}
                    if isinstance(totals, dict)
                    else {}
                ),
            )

    def load_sides(self, turn_offset: int, path: str) -> LoadedSides:
        with self._lock:
            turn_dir = self._turn_dir(turn_offset)
            if turn_dir is None:
                return LoadedSides(None, None, SIDE_UNCAPTURED, SIDE_UNCAPTURED)
            payload = self._read_manifest_payload(turn_dir)
            if payload is None:
                return LoadedSides(None, None, SIDE_UNCAPTURED, SIDE_UNCAPTURED)
            _, display, _ = self._key(path)
            record: dict[str, Any] | None = None
            for raw in [*_payload_list(payload.get("files")), *_payload_list(payload.get("unknown"))]:
                if isinstance(raw, dict) and raw.get("path") == display:
                    record = raw
                    break
            if record is None:
                return LoadedSides(None, None, SIDE_UNCAPTURED, SIDE_UNCAPTURED)
            before, before_state = self._load_side(turn_dir, record.get("before_file"), record.get("before_state"))
            after, after_state = self._load_side(turn_dir, record.get("after_file"), record.get("after_state"))
            return LoadedSides(before, after, before_state, after_state)

    # -- 生命周期 --

    def close(self) -> None:
        """会话释放：尽力删除本会话快照区（之后仍可重新 begin_turn）."""
        with self._lock:
            self._active = None
            self._request_id = ""
            self._turn_bytes = 0
            if self._anchor_ok():
                shutil.rmtree(self.session_dir, ignore_errors=True)

    @staticmethod
    def cleanup_orphans(
        root: str | Path,
        *,
        is_alive: Callable[[int], bool] | None = None,
    ) -> list[str]:
        """只清理"确认无活跃持有者"的会话目录；无 owner.json 一律跳过（行为要求 10）."""
        removed: list[str] = []
        base = Path(root).expanduser()
        try:
            children = sorted(path for path in base.iterdir() if path.is_dir())
        except OSError:
            return removed
        probe = is_alive if is_alive is not None else _pid_alive
        for child in children:
            try:
                owner = json.loads((child / OWNER_NAME).read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                # 可能是其他实例刚建目录、尚未写入 owner 的窗口期：宁可不清理
                logger.warning("turn-change store: skip %s (no readable owner.json)", child)
                continue
            pid = owner.get("pid") if isinstance(owner, dict) else None
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                logger.warning("turn-change store: skip %s (owner.json has no usable pid)", child)
                continue
            try:
                alive = bool(probe(pid))
            except Exception as exc:  # noqa: BLE001 - 探活失败即视为"无法确认"
                logger.warning("turn-change store: skip %s (liveness probe failed: %s)", child, exc)
                continue
            if alive:
                continue
            try:
                shutil.rmtree(child)
            except OSError as exc:
                logger.warning("turn-change store: cannot remove orphan %s (%s)", child, exc)
                continue
            removed.append(str(child))
        return removed

    # -- 内部：登记与收尾 --

    def _anchor_ok(self) -> bool:
        """True while the workspace path still resolves to the anchored directory."""
        try:
            st = os.stat(self._workspace_raw)
        except OSError:
            return False
        return (st.st_dev, st.st_ino) == self._anchor_identity

    def _ensure_session_dir(self) -> bool:
        """Create the session area and its owner marker before any content."""
        if not self._anchor_ok():
            logger.warning(
                "turn-change store: workspace identity changed for %s; refusing to write",
                self._workspace_raw,
            )
            return False
        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            owner = self.session_dir / OWNER_NAME
            if not owner.exists():
                _write_json_atomic(
                    owner,
                    {"session_id": self.session_id, "pid": os.getpid(), "created_at": time.time()},
                )
            return True
        except OSError as exc:
            logger.warning(
                "turn-change store: cannot prepare snapshot area %s (%s)", self.session_dir, exc
            )
            return False

    def _turn_dirs(self) -> list[tuple[int, Path]]:
        found: list[tuple[int, Path]] = []
        try:
            children = list(self.session_dir.iterdir())
        except OSError:
            return found
        for child in children:
            suffix = child.name[len("turn-"):] if child.name.startswith("turn-") else ""
            if not suffix.isdigit() or not child.is_dir():
                continue
            found.append((int(suffix), child))
        found.sort(key=lambda item: item[0])
        return found

    def _turn_dir(self, turn_offset: int) -> Path | None:
        if turn_offset < 0:
            return None
        turns = self._turn_dirs()
        if turn_offset >= len(turns):
            return None
        return turns[len(turns) - 1 - turn_offset][1]

    def _next_seq(self) -> int:
        turns = self._turn_dirs()
        return turns[-1][0] + 1 if turns else 1

    def _session_bytes(self) -> int:
        total = 0
        for _seq, turn_dir in self._turn_dirs():
            for path in turn_dir.glob("*.bin"):
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    def _remove_turn(self, turn_dir: Path) -> bool:
        """Remove one stale turn area; True when the directory is gone."""
        try:
            shutil.rmtree(turn_dir)
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            logger.warning("turn-change store: cannot remove stale turn area %s (%s)", turn_dir, exc)
            return False

    def _evict_until(self, condition: Callable[[], bool]) -> bool:
        """Bounded FIFO eviction; stops as soon as a removal makes no progress.

        Locked or permission-protected directories degrade the caller (quota
        handling) instead of retrying the same failing entry forever.
        """
        while not condition():
            turns = self._turn_dirs()
            if not turns:
                break
            if not self._remove_turn(turns[0][1]):
                break
            if len(self._turn_dirs()) >= len(turns):
                break  # no measurable progress; stop instead of looping
        return condition()

    def _enforce_retention(self) -> None:
        """FIFO: 只保留最近 max_turns_retained 个回合；会话超量先淘汰最旧回合."""
        keep = max(1, int(self.limits.max_turns_retained))
        self._evict_until(lambda: len(self._turn_dirs()) <= keep)
        self._evict_until(
            lambda: len(self._turn_dirs()) <= 1
            or self._session_bytes() <= self.limits.max_session_bytes
        )

    @staticmethod
    def _read_manifest_payload(turn_dir: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads((turn_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("turn-change store: unreadable turn manifest %s (%s)", turn_dir, exc)
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _load_side(turn_dir: Path, name: Any, state: Any) -> tuple[bytes | None, str]:
        if state == SIDE_ABSENT:
            return None, SIDE_ABSENT
        if state != SIDE_CAPTURED:
            return None, SIDE_UNCAPTURED
        if not isinstance(name, str) or not name:
            return None, SIDE_UNCAPTURED
        try:
            return (turn_dir / Path(name).name).read_bytes(), SIDE_CAPTURED
        except OSError as exc:
            logger.warning("turn-change store: unreadable snapshot %s (%s)", name, exc)
            return None, SIDE_UNCAPTURED

    def _persist(self, manifest: TurnChangesManifest, entries: list[_ResolvedEntry]) -> None:
        """尽力落盘；任何失败只降级（调用方仍拿到内存 manifest）."""
        turn_dir = self.session_dir / f"turn-{manifest.turn_seq}"
        try:
            if not self._ensure_session_dir():
                return
            turn_dir.mkdir(parents=True, exist_ok=True)
            by_identity = {id(item.change): item for item in entries}
            payload_entries: list[dict[str, Any]] = []
            for index, change in enumerate([*manifest.files, *manifest.unknown]):
                item = by_identity.get(id(change))
                sides: dict[str, str | None] = {"before": None, "after": None}
                if item is not None and item.before_bytes is not None:
                    sides["before"] = f"before.{index}.bin"
                    _write_bytes(turn_dir / sides["before"], item.before_bytes)
                if item is not None and item.after_bytes is not None:
                    sides["after"] = f"after.{index}.bin"
                    _write_bytes(turn_dir / sides["after"], item.after_bytes)
                payload_entries.append(_change_payload(change, sides))
            _write_json_atomic(
                turn_dir / MANIFEST_NAME,
                {
                    "session_id": manifest.session_id,
                    "request_id": manifest.request_id,
                    "turn_seq": manifest.turn_seq,
                    "created_at": manifest.created_at,
                    "files": payload_entries[: len(manifest.files)],
                    "unknown": payload_entries[len(manifest.files):],
                    "totals": dict(manifest.totals),
                },
            )
            self._enforce_retention()
        except Exception as exc:  # noqa: BLE001 - ledger never breaks the turn
            logger.warning(
                "turn-change store: cannot persist turn %s (%s: %s)",
                manifest.turn_seq,
                type(exc).__name__,
                exc,
            )
            self._remove_turn(turn_dir)

    def _require_active(self) -> dict[str, _PathEntry]:
        if self._active is None:
            raise RuntimeError(
                "turn-change store: no active turn; call begin_turn before note_*"
            )
        return self._active

    def _register(
        self,
        raw_path: str | os.PathLike[str],
        *,
        display: str | None = None,
    ) -> _PathEntry | None:
        active = self._require_active()
        key, derived_display, resolved = self._key(raw_path)
        existing = active.get(key)
        if existing is not None:
            return existing
        if len(active) >= max(1, int(self.limits.max_paths_per_turn)):
            if not self._truncated:
                self._truncated = True
                logger.warning(
                    "turn-change store: path budget %d reached; further touched paths are ignored",
                    self.limits.max_paths_per_turn,
                )
            return None
        entry = _PathEntry(path=display or derived_display, resolved=resolved)
        # 记录目标目录身份；读取 after 前核验，目录被替换/换成符号链接时降级
        try:
            entry.anchor = _open_dir_anchor(resolved.parent)
        except OSError:
            entry.anchor = None
        active[key] = entry
        return entry

    def _key(self, raw_path: str | os.PathLike[str]) -> tuple[str, str, Path]:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        try:
            resolved = candidate.resolve()
        except OSError:  # pragma: no cover - Path.resolve is tolerant on 3.11+
            resolved = Path(candidate.absolute())
        key = resolved.as_posix()
        try:
            display = resolved.relative_to(self.workspace).as_posix()
        except ValueError:
            display = key
        return key, display, resolved

    @staticmethod
    def _remember_checkpoint(entry: _PathEntry, checkpoint_id: str) -> None:
        if checkpoint_id and checkpoint_id not in entry.checkpoint_ids:
            entry.checkpoint_ids.append(checkpoint_id)

    def _accept_snapshot(self, size: int) -> tuple[str, str]:
        """Reserve ``size`` snapshot bytes, or report why they are not stored."""
        if size > self.limits.max_file_bytes:
            return SIDE_UNCAPTURED, REASON_QUOTA
        if self._turn_bytes + size > self.limits.max_turn_bytes:
            return SIDE_UNCAPTURED, REASON_QUOTA
        if not self._session_has_room(size):
            return SIDE_UNCAPTURED, REASON_QUOTA
        self._turn_bytes += size
        return SIDE_CAPTURED, ""

    def _session_has_room(self, extra: int) -> bool:
        """会话超量先 FIFO 淘汰最旧回合；仍超则由调用方内部降级（行为要求 6）."""
        limit = self.limits.max_session_bytes
        if self._session_bytes() + self._turn_bytes + extra <= limit:
            return True
        self._evict_until(
            lambda: not self._turn_dirs()
            or self._session_bytes() + self._turn_bytes + extra <= limit
        )
        return self._session_bytes() + self._turn_bytes + extra <= limit

    def _resolve(
        self,
        entry: _PathEntry,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> _ResolvedEntry | None:
        if entry.before_state or entry.tracked is None:
            # 该路径会读取 after；取消/过期后停止新的可放弃捕获（review R6）
            stop_reason = self._stop_reason(deadline, cancelled)
            if stop_reason:
                return self._degraded_without_read(entry, stop_reason)
        if entry.before_state:
            return self._resolve_snapshot(entry, deadline, cancelled)
        if entry.tracked is not None:
            return self._resolve_tracked(entry)
        # 仅候选：before 从未取得快照 → 未能确认区（带原因）
        after_state, after_bytes, _ = self._read_after(entry)
        return self._unknown_entry(entry, SIDE_UNCAPTURED, REASON_ERROR, after_state, after_bytes)

    def _read_after(self, entry: _PathEntry) -> tuple[str, bytes | None, str]:
        if not self._anchor_ok():
            logger.warning(
                "turn-change store: workspace identity changed for %s; skipping read",
                self._workspace_raw,
            )
            return SIDE_UNCAPTURED, None, REASON_ERROR
        if entry.anchor is not None:
            try:
                current = _open_dir_anchor(entry.resolved.parent)
            except OSError:
                current = None
            if current != entry.anchor:
                logger.warning(
                    "turn-change store: target directory identity changed for %s; skipping read",
                    entry.path,
                )
                return SIDE_UNCAPTURED, None, REASON_ERROR
        path = entry.resolved
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return SIDE_ABSENT, None, ""
        except OSError as exc:
            logger.warning("turn-change store: cannot stat %s (%s)", entry.path, exc)
            return SIDE_UNCAPTURED, None, REASON_ERROR
        if not path.is_file():
            logger.warning("turn-change store: %s is not a regular file", entry.path)
            return SIDE_UNCAPTURED, None, REASON_ERROR
        state, reason = self._accept_snapshot(size)
        if state != SIDE_CAPTURED:
            # 超限的 after 字节绝不落盘
            return SIDE_UNCAPTURED, None, reason
        try:
            data = _read_bytes_bounded(path, self.limits.max_file_bytes)
        except OSError as exc:
            self._turn_bytes -= size
            logger.warning("turn-change store: cannot read %s (%s)", entry.path, exc)
            return SIDE_UNCAPTURED, None, REASON_ERROR
        if len(data) > self.limits.max_file_bytes:
            # 读取期间增长越过单文件上限：有界读取兜底，超限字节不落盘
            self._turn_bytes -= size
            logger.warning(
                "turn-change store: %s grew past the byte limit while reading", entry.path
            )
            return SIDE_UNCAPTURED, None, REASON_QUOTA
        if len(data) != size:  # 读取期间被改写：按实际字节数重新结算
            self._turn_bytes -= size
            state, reason = self._accept_snapshot(len(data))
            if state != SIDE_CAPTURED:
                return SIDE_UNCAPTURED, None, reason
        return SIDE_CAPTURED, data, ""

    def _resolve_snapshot(
        self,
        entry: _PathEntry,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> _ResolvedEntry | None:
        before_state = entry.before_state
        after_state, after_bytes, after_reason = self._read_after(entry)
        if before_state == SIDE_CAPTURED and after_state == SIDE_CAPTURED:
            if entry.before == after_bytes:
                # 两侧字节相同（含"改了又改回"）→ unchanged；seal 契约排除该状态
                return _ResolvedEntry(
                    self._no_change(entry, before_state, after_state),
                    entry.before,
                    after_bytes,
                )
            return self._diff_entry(
                entry, entry.before, after_bytes, STATE_MODIFIED, before_state, after_state, deadline, cancelled
            )
        if before_state == SIDE_ABSENT and after_state == SIDE_ABSENT:
            return None  # 原来不存在、现在也不存在 → 无净改动
        if before_state == SIDE_ABSENT and after_state == SIDE_CAPTURED:
            return self._diff_entry(
                entry, None, after_bytes, STATE_ADDED, before_state, after_state, deadline, cancelled
            )
        if before_state == SIDE_CAPTURED and after_state == SIDE_ABSENT:
            return self._diff_entry(
                entry, entry.before, None, STATE_DELETED, before_state, after_state, deadline, cancelled
            )
        reason = after_reason or entry.before_reason or REASON_ERROR
        return self._unknown_entry(entry, before_state, reason, after_state, after_bytes)

    def _resolve_tracked(self, entry: _PathEntry) -> _ResolvedEntry | None:
        before_fp, after_fp = entry.tracked or (None, None)
        before_state = SIDE_ABSENT if before_fp == _TRACKED_MISSING else SIDE_UNCAPTURED
        after_state = SIDE_ABSENT if after_fp == _TRACKED_MISSING else SIDE_UNCAPTURED
        if _tracked_is_unusable(before_fp) or _tracked_is_unusable(after_fp):
            logger.warning(
                "turn-change store: unusable tracked fingerprint for %s (%r vs %r)",
                entry.path,
                before_fp,
                after_fp,
            )
            return self._unknown_entry(entry, SIDE_UNCAPTURED, REASON_TRACKED, SIDE_UNCAPTURED)
        state = _tracked_state(before_fp, after_fp)
        if state is None:
            return None
        if state == STATE_UNKNOWN:
            return self._unknown_entry(entry, before_state, REASON_TRACKED, after_state)
        return _ResolvedEntry(
            change=FileChange(
                path=entry.path,
                display=entry.path,
                state=state,
                before_state=before_state,
                after_state=after_state,
                added=None,
                removed=None,
                compare=COMPARE_NONE,
                reason=REASON_TRACKED,
                checkpoint_ids=list(entry.checkpoint_ids),
            )
        )

    def _diff_entry(
        self,
        entry: _PathEntry,
        before: bytes | None,
        after: bytes | None,
        state: str,
        before_state: str,
        after_state: str,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> _ResolvedEntry:
        stop_reason = self._stop_reason(deadline, cancelled)
        if stop_reason:
            return self._degraded_entry(entry, before, after, state, before_state, after_state, stop_reason)
        differ = self._differ if self._differ is not None else turn_diff.diff_bytes
        per_file_deadline = min(self._clock() + self.limits.diff_deadline_ms / 1000.0, deadline)
        try:
            stats = differ(
                before,
                after,
                max_bytes=self.limits.max_file_bytes,
                deadline=per_file_deadline,
                cancelled=cancelled,
            )
        except Exception as exc:  # noqa: BLE001 - differ 失败只降级本条
            logger.warning(
                "turn-change store: diff failed for %s (%s: %s)", entry.path, type(exc).__name__, exc
            )
            return self._degraded_entry(entry, before, after, state, before_state, after_state, REASON_ERROR)
        # 行数（added/removed）仅是展示数据：零行差不代表字节未变。净状态由
        # 存在性与字节决定（两侧字节相同已在 _resolve_snapshot 判为 unchanged）。
        if stats.quality == turn_diff.DIFF_COARSE:
            compare = COMPARE_COARSE
            added, removed = stats.added, stats.removed
        elif stats.quality == turn_diff.DIFF_FULL:
            compare = COMPARE_FULL
            added, removed = stats.added, stats.removed
        else:
            compare = COMPARE_NONE
            added, removed = None, None
        return _ResolvedEntry(
            change=FileChange(
                path=entry.path,
                display=entry.path,
                state=state,
                before_state=before_state,
                after_state=after_state,
                added=added,
                removed=removed,
                compare=compare,
                reason=stats.reason or "",
                checkpoint_ids=list(entry.checkpoint_ids),
            ),
            before_bytes=before,
            after_bytes=after,
        )

    def _stop_reason(self, deadline: float, cancelled: Callable[[], bool] | None) -> str:
        """返回空串表示可继续，否则给出降级原因（取消优先于超预算）."""
        if cancelled is not None:
            try:
                if cancelled():
                    return turn_diff.REASON_CANCELLED
            except Exception as exc:  # noqa: BLE001 - 取消信号出错按"已取消"处理
                logger.warning("turn-change store: cancel callback failed (%s); stopping diff work", exc)
                return turn_diff.REASON_CANCELLED
        if self._clock() >= deadline:
            return turn_diff.REASON_TIMEOUT
        return ""

    def _degraded_entry(
        self,
        entry: _PathEntry,
        before: bytes | None,
        after: bytes | None,
        state: str,
        before_state: str,
        after_state: str,
        reason: str,
    ) -> _ResolvedEntry:
        """超时/取消/错误：保留已确认状态，只丢掉计数（仅路径+大小）."""
        return _ResolvedEntry(
            change=FileChange(
                path=entry.path,
                display=entry.path,
                state=state,
                before_state=before_state,
                after_state=after_state,
                added=None,
                removed=None,
                compare=COMPARE_NONE,
                reason=reason or REASON_ERROR,
                checkpoint_ids=list(entry.checkpoint_ids),
            ),
            before_bytes=before,
            after_bytes=after,
        )

    def _degraded_without_read(self, entry: _PathEntry, reason: str) -> _ResolvedEntry:
        """取消/过期：停止新的可放弃捕获，不读取 after；条目按 before 侧降级保留."""
        if entry.before_state == SIDE_ABSENT:
            state = STATE_ADDED
        elif entry.before_state == SIDE_CAPTURED:
            state = STATE_MODIFIED
        else:
            state = STATE_UNKNOWN
        return _ResolvedEntry(
            change=FileChange(
                path=entry.path,
                display=entry.path,
                state=state,
                before_state=entry.before_state or SIDE_UNCAPTURED,
                after_state=SIDE_UNCAPTURED,
                added=None,
                removed=None,
                compare=COMPARE_NONE,
                reason=reason,
                checkpoint_ids=list(entry.checkpoint_ids),
            ),
            before_bytes=entry.before,
            after_bytes=None,
        )

    def _no_change(self, entry: _PathEntry, before_state: str, after_state: str) -> FileChange:
        return FileChange(
            path=entry.path,
            display=entry.path,
            state=STATE_UNCHANGED,
            before_state=before_state,
            after_state=after_state,
            added=0,
            removed=0,
            compare=COMPARE_FULL,
            reason="",
            checkpoint_ids=list(entry.checkpoint_ids),
        )

    def _unknown_entry(
        self,
        entry: _PathEntry,
        before_state: str,
        reason: str,
        after_state: str,
        after_bytes: bytes | None = None,
    ) -> _ResolvedEntry:
        return _ResolvedEntry(
            change=FileChange(
                path=entry.path,
                display=entry.path,
                state=STATE_UNKNOWN,
                before_state=before_state,
                after_state=after_state,
                added=None,
                removed=None,
                compare=COMPARE_NONE,
                reason=reason,
                checkpoint_ids=list(entry.checkpoint_ids),
            ),
            before_bytes=entry.before,
            after_bytes=after_bytes,
        )


def _tracked_state(before_fp: str | None, after_fp: str | None) -> str | None:
    """Frozen ``note_tracked`` decision table (§3.2 behavior 4)."""
    if before_fp is not None and after_fp is not None:
        if before_fp == after_fp:
            return None  # unchanged → 不出现
        if before_fp == _TRACKED_MISSING and _tracked_has_content(after_fp):
            return STATE_MODIFIED  # 重建
        if _tracked_has_content(before_fp) and after_fp == _TRACKED_MISSING:
            return STATE_DELETED
        return STATE_MODIFIED
    if before_fp is None:
        if after_fp == _TRACKED_MISSING:
            return STATE_DELETED
        if _tracked_has_content(after_fp):
            return STATE_MODIFIED
        return STATE_UNKNOWN  # None → None：无从确认
    if after_fp is None:
        return STATE_MODIFIED
    return STATE_UNKNOWN  # pragma: no cover - 穷尽分支


def _read_bytes_bounded(path: Path, limit: int) -> bytes:
    """Read at most ``limit + 1`` bytes so a growing file cannot be pulled in."""
    with path.open("rb") as handle:
        return handle.read(limit + 1)


def _write_bytes(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temp, path)


def _payload_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _change_payload(change: FileChange, sides: dict[str, str | None]) -> dict[str, Any]:
    return {
        "path": change.path,
        "display": change.display,
        "state": change.state,
        "before_state": change.before_state,
        "after_state": change.after_state,
        "added": change.added,
        "removed": change.removed,
        "compare": change.compare,
        "reason": change.reason,
        "checkpoint_ids": list(change.checkpoint_ids),
        "before_file": sides.get("before"),
        "after_file": sides.get("after"),
    }


def _change_from_payload(raw: Any) -> FileChange:
    if not isinstance(raw, dict):
        raise TypeError("manifest entry must be an object")
    checkpoint_ids = raw.get("checkpoint_ids")
    return FileChange(
        path=str(raw.get("path") or ""),
        display=str(raw.get("display") or raw.get("path") or ""),
        state=str(raw.get("state") or STATE_UNKNOWN),
        before_state=str(raw.get("before_state") or SIDE_UNCAPTURED),
        after_state=str(raw.get("after_state") or SIDE_UNCAPTURED),
        added=_optional_int(raw.get("added")),
        removed=_optional_int(raw.get("removed")),
        compare=str(raw.get("compare") or COMPARE_NONE),
        reason=str(raw.get("reason") or ""),
        checkpoint_ids=[str(item) for item in checkpoint_ids] if isinstance(checkpoint_ids, list) else [],
    )


def _open_dir_anchor(path: Path) -> tuple[int, int]:
    """Directory identity of ``path`` without following a swapped final symlink.

    Returns ``(st_dev, st_ino)`` of the physical directory. Raises ``OSError``
    when the final component is a symbolic link (the shape of the audit
    path-swap attack), so callers degrade instead of writing through the swap.
    """
    if os.name != "nt":
        dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        parent_fd = os.open(str(path.parent), dir_flags)
        try:
            last_fd = os.open(
                path.name, dir_flags | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd
            )
        finally:
            os.close(parent_fd)
        try:
            st = os.fstat(last_fd)
            return (st.st_dev, st.st_ino)
        finally:
            os.close(last_fd)
    if stat.S_ISLNK(os.lstat(path).st_mode):
        raise OSError(f"workspace path is a symbolic link: {path}")
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness probe; anything uncertain counts as alive."""
    if pid == os.getpid():
        return True
    try:
        return pid_alive(pid)
    except Exception:
        return True


def current_turn_change_store() -> TurnChangeStore | None:
    """回合上下文中的会话 store（files.py 旁路与 react 接线共用的单一来源）."""
    return _CURRENT.get()


@contextmanager
def turn_store_scope(store: TurnChangeStore | None) -> Iterator[None]:
    """Bind ``store`` for the current turn; nesting restores the previous value."""
    token = _CURRENT.set(store)
    try:
        yield
    finally:
        _CURRENT.reset(token)
