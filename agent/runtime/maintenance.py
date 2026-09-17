"""Conservative preview-first cleanup for Astra-owned runtime artifacts."""

from __future__ import annotations

from agent.runtime.paths import state_dir

import hashlib
import os
import sqlite3
import stat
import time
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


_DURABLE_DIRECTORIES = frozenset({"sessions", "memory", "skills", "tasks", "checkpoints"})


def _requires_content_fingerprint() -> bool:
    # Python 3.11 reports Windows creation time as st_ctime, not change time.
    return os.name == "nt"


def _file_signature(metadata: os.stat_result) -> tuple[int, ...]:
    # Windows stat/fstat can disagree on ctime's meaning in Python 3.12.
    # Use creation time for that comparison; keep POSIX metadata change time.
    timestamp = getattr(metadata, "st_birthtime_ns", metadata.st_ctime_ns) if os.name == "nt" else metadata.st_ctime_ns
    return (
        metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size,
        metadata.st_mtime_ns, timestamp,
    )


def _content_fingerprint(path: Path, metadata: os.stat_result) -> str:
    expected = _file_signature(metadata)
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if _file_signature(opened) != expected:
            raise OSError("candidate changed before content verification")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        verified = os.fstat(stream.fileno())
        # ctime remains comparable between two handle reads, even on Windows.
        if _file_signature(verified) != expected or verified.st_ctime_ns != opened.st_ctime_ns:
            raise OSError("candidate changed during content verification")
    return digest


@dataclass(frozen=True)
class MaintenanceCandidate:
    path: Path
    category: str
    size: int
    modified_at: float
    device: int
    inode: int
    modified_ns: int
    changed_ns: int
    content_sha256: str = ""


class RuntimeMaintenance:
    def __init__(
        self,
        root: str | Path,
        *,
        artifact_dirs: Iterable[str | Path] = (),
        database_paths: Iterable[str | Path] = (),
    ):
        self.root = Path(root).expanduser().resolve()
        self.astra_root = state_dir(self.root)
        self.artifact_dirs = tuple(Path(path).expanduser().absolute() for path in artifact_dirs)
        self.database_paths = tuple(Path(path).expanduser().absolute() for path in database_paths)

    def _owned(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.astra_root)
            if ".." in relative.parts:
                return False
            # Never treat symlinked runtime roots or intermediate directories
            # as ownership, even when their target is also within .astra.
            if any(
                parent.is_symlink()
                for parent in (path, *path.parents)
                if parent == self.astra_root or parent.is_relative_to(self.astra_root)
            ):
                return False
            return path.resolve().is_relative_to(self.astra_root)
        except (OSError, ValueError, RuntimeError):
            return False

    def _cleanup_owned(self, path: Path) -> bool:
        return self._owned(path) and not any(
            part.lower() in _DURABLE_DIRECTORIES
            for part in path.relative_to(self.astra_root).parts
        )

    def _generated_directory(self, path: Path) -> bool:
        return path != self.astra_root and self._cleanup_owned(path) and path.is_dir()

    def _candidate(
        self,
        path: Path,
        *,
        category: str,
        cutoff: float,
    ) -> MaintenanceCandidate | None:
        try:
            if not self._cleanup_owned(path):
                return None
            metadata = path.lstat()
        except OSError:
            return None
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mtime >= cutoff:
            return None
        content_sha256 = ""
        if _requires_content_fingerprint():
            try:
                content_sha256 = _content_fingerprint(path, metadata)
                if (
                    not content_sha256
                    or not self._cleanup_owned(path)
                    or _file_signature(path.lstat()) != _file_signature(metadata)
                ):
                    return None
            except OSError:
                return None
        return MaintenanceCandidate(
            path, category, metadata.st_size, metadata.st_mtime,
            metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_ctime_ns,
            content_sha256,
        )

    def plan(self, *, artifact_days: int = 30, now: float | None = None) -> list[MaintenanceCandidate]:
        current = time.time() if now is None else float(now)
        days = max(1, min(int(artifact_days), 3650))
        candidates: dict[str, MaintenanceCandidate] = {}
        artifact_cutoff = current - days * 86_400
        for directory in self.artifact_dirs:
            if not self._generated_directory(directory):
                continue
            for path in directory.rglob("*"):
                candidate = self._candidate(
                    path,
                    category="expired_tool_artifact",
                    cutoff=artifact_cutoff,
                )
                if candidate is not None:
                    candidates[os.path.normcase(str(candidate.path))] = candidate

        # Root-level interrupted atomic writes retain the existing >24h policy.
        # Nested temp files are only ours in explicitly configured generated
        # artifact directories; a suffix alone never makes durable data ours.
        temp_cutoff = current - 86_400
        temp_paths = list(self.astra_root.glob("*.tmp")) if self._owned(self.astra_root) else []
        for directory in self.artifact_dirs:
            if self._generated_directory(directory):
                temp_paths.extend(directory.rglob("*.tmp"))
        for path in temp_paths:
            candidate = self._candidate(
                path,
                category="stale_temp",
                cutoff=temp_cutoff,
            )
            if candidate is not None:
                candidates[os.path.normcase(str(candidate.path))] = candidate
        return sorted(candidates.values(), key=lambda item: (item.category, item.modified_at, str(item.path)))

    def database_snapshot(self) -> list[dict]:
        snapshots = []
        seen: set[str] = set()
        for path in self.database_paths:
            key = os.path.normcase(str(path))
            if key in seen or not self._owned(path) or path.suffix.lower() != ".db":
                continue
            seen.add(key)
            try:
                database_bytes = path.stat().st_size if path.is_file() else 0
                wal = Path(f"{path}-wal")
                wal_bytes = wal.stat().st_size if wal.is_file() else 0
            except OSError:
                database_bytes = wal_bytes = 0
            snapshots.append({
                "path": str(path),
                "database_bytes": int(database_bytes),
                "wal_bytes": int(wal_bytes),
            })
        return snapshots

    def _checkpoint_databases(self) -> list[dict]:
        results = []
        for item in self.database_snapshot():
            path = Path(item["path"])
            if not path.is_file():
                continue
            try:
                with closing(sqlite3.connect(path, timeout=1)) as db:
                    db.execute("PRAGMA busy_timeout=1000")
                    row = db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                results.append({
                    "path": str(path),
                    "busy": int(row[0]) if row else 0,
                    "log_frames": int(row[1]) if row else 0,
                    "checkpointed_frames": int(row[2]) if row else 0,
                    "error": "",
                })
            except (OSError, sqlite3.Error) as exc:
                results.append({
                    "path": str(path),
                    "busy": 0,
                    "log_frames": 0,
                    "checkpointed_frames": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                })
        return results

    def apply(self, *, artifact_days: int = 30) -> dict:
        candidates = self.plan(artifact_days=artifact_days)
        removed = []
        failed = []
        released = 0
        for candidate in candidates:
            try:
                # Re-check ownership, file identity, and freshness immediately
                # before mutation. A replaced/refreshed file needs a new plan.
                if not self._cleanup_owned(candidate.path):
                    raise OSError("candidate is no longer an owned regular file")
                metadata = candidate.path.lstat()
                if not stat.S_ISREG(metadata.st_mode) or (
                    metadata.st_dev, metadata.st_ino, metadata.st_size,
                    metadata.st_mtime_ns, metadata.st_ctime_ns,
                ) != (
                    candidate.device, candidate.inode, candidate.size,
                    candidate.modified_ns, candidate.changed_ns,
                ):
                    raise OSError("candidate changed since planning")
                if _requires_content_fingerprint():
                    if (
                        not candidate.content_sha256
                        or _content_fingerprint(candidate.path, metadata) != candidate.content_sha256
                    ):
                        raise OSError("candidate content changed since planning")
                    if (
                        not self._cleanup_owned(candidate.path)
                        or _file_signature(candidate.path.lstat()) != _file_signature(metadata)
                    ):
                        raise OSError("candidate changed during content verification")
                candidate.path.unlink()
                removed.append(str(candidate.path))
                released += candidate.size
            except OSError as exc:
                failed.append({"path": str(candidate.path), "error": f"{type(exc).__name__}: {exc}"})
        return {
            "removed": removed,
            "failed": failed,
            "released_bytes": released,
            "checkpoints": self._checkpoint_databases(),
        }


def format_maintenance_report(
    maintenance: RuntimeMaintenance,
    *,
    action: str = "preview",
    artifact_days: int = 30,
) -> str:
    normalized = str(action or "preview").strip().lower()
    if normalized not in {"preview", "apply", "checkpoint"}:
        return "Usage: /maintenance [preview [days]|apply [days]|checkpoint]"
    days = max(1, min(int(artifact_days), 3650))
    if normalized == "apply":
        result = maintenance.apply(artifact_days=days)
        checkpoint_errors = sum(bool(item["error"]) for item in result["checkpoints"])
        busy = sum(bool(item["busy"]) for item in result["checkpoints"])
        return "\n".join([
            "Astra maintenance applied:",
            f"  Removed: {len(result['removed'])} Astra-owned generated files",
            f"  Released: {int(result['released_bytes']):,} bytes",
            f"  Failed removals: {len(result['failed'])}",
            (
                f"  SQLite PASSIVE checkpoints: {len(result['checkpoints'])} "
                f"({busy} busy, {checkpoint_errors} errors)"
            ),
            "  Sessions, memory, skills, tasks, checkpoints, and workspace files were not targeted.",
        ])
    if normalized == "checkpoint":
        results = maintenance._checkpoint_databases()
        errors = sum(bool(item["error"]) for item in results)
        busy = sum(bool(item["busy"]) for item in results)
        return (
            f"SQLite PASSIVE checkpoints: {len(results)} ({busy} busy, {errors} errors).\n"
            "No files were deleted and VACUUM was not run."
        )

    candidates = maintenance.plan(artifact_days=days)
    category_counts: dict[str, int] = {}
    total = 0
    for candidate in candidates:
        category_counts[candidate.category] = category_counts.get(candidate.category, 0) + 1
        total += candidate.size
    databases = maintenance.database_snapshot()
    return "\n".join([
        "Astra maintenance preview (no changes made):",
        f"  Tool artifact retention: {days} days",
        f"  Expired generated artifacts: {category_counts.get('expired_tool_artifact', 0)}",
        f"  Stale .tmp files (>24h, runtime root or generated artifact directories): {category_counts.get('stale_temp', 0)}",
        f"  Reclaimable: {total:,} bytes",
        f"  Project-owned SQLite databases: {len(databases)}; WAL bytes: {sum(item['wal_bytes'] for item in databases):,}",
        "  Excluded: sessions, memory, skills, tasks, checkpoints, workspace files, symlinks, and paths outside .astra.",
        f"Run /maintenance apply {days} to replan, remove unchanged candidates, and checkpoint WAL files.",
    ])
