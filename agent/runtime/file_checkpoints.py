"""Local, Git-independent checkpoints for files changed by Agent tools."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class CapturedFile:
    path: Path
    relative: str
    existed: bool
    before: bytes
    before_sha256: str


@dataclass
class PendingCheckpoint:
    checkpoint_id: str
    operation: str
    task_id: str
    created_at: float
    files: list[CapturedFile]
    skipped_paths: list[str] = field(default_factory=list)


class FileCheckpointStore:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).expanduser().resolve()
        self.root = self.workspace / ".astra" / "checkpoints"
        self.max_checkpoints = self._positive_env("AGENT_CHECKPOINT_MAX_COUNT", 50)
        self.max_file_bytes = self._positive_env("AGENT_CHECKPOINT_MAX_FILE_BYTES", 20 * 1024 * 1024)
        self.max_total_bytes = self._positive_env("AGENT_CHECKPOINT_MAX_TOTAL_BYTES", 100 * 1024 * 1024)
        self._lock = threading.RLock()

    @staticmethod
    def _positive_env(name: str, default: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError:
            return default
        return value if value > 0 else default

    def _resolve(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve()
        if os.path.commonpath([str(self.workspace), str(resolved)]) != str(self.workspace):
            raise ValueError(f"checkpoint path escapes workspace: {path}")
        return resolved

    def capture(
        self,
        paths: list[str | Path],
        *,
        operation: str,
        task_id: str = "",
    ) -> PendingCheckpoint | None:
        files: list[CapturedFile] = []
        skipped_paths: list[str] = []
        total = 0
        seen: set[str] = set()
        for raw in paths:
            try:
                path = self._resolve(raw)
            except ValueError:
                # Approved writes outside the workspace cannot be captured
                # without broadening the checkpoint root. Skip only this path;
                # other paths in the same batch remain checkpointable.
                skipped_paths.append(str(raw))
                continue
            key = os.path.normcase(str(path))
            if key in seen:
                continue
            seen.add(key)
            existed = path.is_file()
            size = path.stat().st_size if existed else 0
            if size > self.max_file_bytes or total + size > self.max_total_bytes:
                continue
            before = path.read_bytes() if existed else b""
            total += size
            files.append(CapturedFile(
                path=path,
                relative=path.relative_to(self.workspace).as_posix(),
                existed=existed,
                before=before,
                before_sha256=_sha(before) if existed else "",
            ))
        if not files and not skipped_paths:
            return None
        return PendingCheckpoint(
            checkpoint_id=uuid.uuid4().hex,
            operation=operation,
            task_id=task_id,
            created_at=time.time(),
            files=files,
            skipped_paths=skipped_paths,
        )

    def finalize(self, pending: PendingCheckpoint | None) -> str:
        if pending is None:
            return ""
        entries: list[dict[str, Any]] = []
        changed: list[CapturedFile] = []
        for captured in pending.files:
            exists_after = captured.path.is_file()
            if exists_after and captured.path.stat().st_size > self.max_file_bytes:
                continue
            after = captured.path.read_bytes() if exists_after else b""
            after_sha = _sha(after) if exists_after else ""
            if captured.existed == exists_after and captured.before_sha256 == after_sha:
                continue
            changed.append(captured)
            entries.append({
                "path": captured.relative,
                "existed_before": captured.existed,
                "before_sha256": captured.before_sha256,
                "exists_after": exists_after,
                "after_sha256": after_sha,
                "backup": f"{len(changed) - 1}.before.bin" if captured.existed else "",
            })
        if not entries:
            return ""

        directory = self.root / pending.checkpoint_id
        with self._lock:
            directory.mkdir(parents=True, exist_ok=False)
            for index, captured in enumerate(changed):
                if captured.existed:
                    (directory / f"{index}.before.bin").write_bytes(captured.before)
            manifest = {
                "version": 1,
                "checkpoint_id": pending.checkpoint_id,
                "operation": pending.operation,
                "task_id": pending.task_id,
                "created_at": pending.created_at,
                "files": entries,
            }
            temporary = directory / "manifest.json.tmp"
            temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, directory / "manifest.json")
            self._prune()
        return pending.checkpoint_id

    def _manifests(self) -> list[tuple[Path, dict[str, Any]]]:
        items: list[tuple[Path, dict[str, Any]]] = []
        for path in self.root.glob("*/manifest.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                items.append((path, payload))
        return sorted(items, key=lambda item: float(item[1].get("created_at") or 0), reverse=True)

    def _prune(self) -> None:
        for path, _payload in self._manifests()[self.max_checkpoints:]:
            shutil.rmtree(path.parent, ignore_errors=True)

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {
                "checkpoint_id": payload.get("checkpoint_id"),
                "operation": payload.get("operation"),
                "task_id": payload.get("task_id"),
                "created_at": payload.get("created_at"),
                "files": [entry.get("path") for entry in payload.get("files", [])],
            }
            for _path, payload in self._manifests()[:max(1, min(limit, 100))]
        ]

    def restore(self, checkpoint_id: str) -> dict[str, Any]:
        if not checkpoint_id or not all(char in "0123456789abcdef" for char in checkpoint_id.lower()):
            raise ValueError("invalid checkpoint_id")
        directory = self.root / checkpoint_id
        manifest_path = directory / "manifest.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"checkpoint not found: {checkpoint_id}") from exc
        entries = payload.get("files")
        if not isinstance(entries, list) or not entries:
            raise ValueError("checkpoint has no files")

        resolved: list[tuple[Path, dict[str, Any], bytes | None]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("checkpoint manifest is invalid")
            target = self._resolve(str(entry.get("path") or ""))
            exists = target.is_file()
            current_sha = _sha(target.read_bytes()) if exists else ""
            if exists != bool(entry.get("exists_after")) or current_sha != str(entry.get("after_sha256") or ""):
                raise RuntimeError(
                    f"restore conflict: {entry.get('path')} changed after checkpoint; "
                    "refusing to overwrite user or later Agent edits"
                )
            backup_name = str(entry.get("backup") or "")
            backup = (directory / backup_name).read_bytes() if backup_name else None
            if backup is not None and _sha(backup) != str(entry.get("before_sha256") or ""):
                raise RuntimeError(f"checkpoint backup hash mismatch: {entry.get('path')}")
            resolved.append((target, entry, backup))

        rollback: list[tuple[Path, bool, bytes]] = []
        restored: list[str] = []
        try:
            for target, entry, backup in resolved:
                existed = target.is_file()
                current = target.read_bytes() if existed else b""
                rollback.append((target, existed, current))
                if bool(entry.get("existed_before")):
                    assert backup is not None
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                        temporary = Path(handle.name)
                        handle.write(backup)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, target)
                else:
                    target.unlink(missing_ok=True)
                restored.append(str(entry.get("path")))
        except Exception:
            for target, existed, content in reversed(rollback):
                if existed:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                else:
                    target.unlink(missing_ok=True)
            raise
        return {"checkpoint_id": checkpoint_id, "restored": restored, "status": "restored"}
