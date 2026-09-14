"""Descriptor-relative filesystem boundary for provider-audit artifacts."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Self

_AUDIT_ROOT_PARTS = (".astra", "core-runtime-audit")
_RUNTIME_DIRECTORIES = (
    PurePath("image-cache"),
    PurePath("image-cache/tiles"),
    PurePath(".astra"),
    PurePath(".astra/file-transactions"),
    PurePath("tool-results"),
)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
_MAX_PROBE_INPUT_BYTES = 4_096


def _require_safe_primitives() -> None:
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW")
    required_dir_fd = (os.open, os.mkdir, os.stat)
    if (
        os.name != "posix"
        or any(not hasattr(os, name) for name in required_flags)
        or any(operation not in os.supports_dir_fd for operation in required_dir_fd)
        or not hasattr(os, "fchmod")
    ):
        raise RuntimeError(
            "Core-runtime audit requires descriptor-relative no-follow filesystem APIs"
        )


def _safe_parts(relative: str | PurePath) -> tuple[str, ...]:
    path = PurePath(relative)
    if path.is_absolute() or not path.parts or any(
        part in {"", ".", ".."} or "/" in part or "\\" in part
        for part in path.parts
    ):
        raise RuntimeError("Audit path must be a safe relative path")
    return path.parts


def _open_directory_at(parent_fd: int, name: str, *, create: bool) -> int:
    created = False
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
    try:
        descriptor = os.open(
            name,
            _DIRECTORY_FLAGS | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise RuntimeError(
            f"Audit directory component is a symlink or unsafe object: {name}"
        ) from exc
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        raise RuntimeError(f"Audit directory component is not a directory: {name}")
    if created:
        os.fchmod(descriptor, 0o700)
    return descriptor


def _open_chain(start_fd: int, relative: str | PurePath, *, create: bool) -> int:
    descriptor = os.dup(start_fd)
    try:
        for part in _safe_parts(relative):
            child = _open_directory_at(descriptor, part, create=create)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _reject_existing_artifact(parent_fd: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        raise RuntimeError("Refusing a multi-link or hardlink audit artifact")
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("Refusing a special, symlink, or non-regular audit artifact")
    raise RuntimeError("Refusing to overwrite an existing audit artifact")


def _validate_tree(directory_fd: int) -> None:
    """Recursively reject links and special or multi-link files via open handles."""
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = _open_directory_at(directory_fd, entry.name, create=False)
                try:
                    _validate_tree(child)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise RuntimeError("Refusing a multi-link or hardlink audit artifact")
            else:
                raise RuntimeError(
                    "Refusing a special, symlink, or non-regular audit artifact"
                )


@dataclass
class AuditRun:
    """A private per-run directory anchored by a retained directory descriptor."""

    path: Path
    project_root: Path
    run_name: str
    descriptor: int
    device: int
    inode: int

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        del exc_info
        self.close()

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def prepare_runtime(self) -> None:
        self._require_open()
        _validate_tree(self.descriptor)
        for relative in _RUNTIME_DIRECTORIES:
            descriptor = _open_chain(self.descriptor, relative, create=True)
            os.close(descriptor)
        _validate_tree(self.descriptor)

    def write_private_text(self, relative: str | PurePath, value: str) -> Path:
        """Exclusively create a private regular file through retained dir handles."""
        self._require_open()
        parts = _safe_parts(relative)
        parent_fd = os.dup(self.descriptor)
        try:
            for part in parts[:-1]:
                child = _open_directory_at(parent_fd, part, create=False)
                os.close(parent_fd)
                parent_fd = child
            name = parts[-1]
            _reject_existing_artifact(parent_fd, name)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            try:
                descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
            except FileExistsError as exc:
                _reject_existing_artifact(parent_fd, name)
                raise RuntimeError("Audit artifact appeared during creation") from exc
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise RuntimeError(
                        "New audit artifact is not an exclusive regular file"
                    )
                os.fchmod(descriptor, 0o600)
                data = value.encode("utf-8")
                offset = 0
                while offset < len(data):
                    offset += os.write(descriptor, data[offset:])
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_fd)
        return self.path.joinpath(*parts)

    def regular_file_identity(
        self,
        relative: str | PurePath,
        *,
        expected_content: bytes | None = None,
    ) -> tuple[int, int, int, str]:
        """Return immutable metadata and digest for one retained-run file."""
        descriptor = self._open_regular_file(relative)
        try:
            info = os.fstat(descriptor)
            if info.st_size > _MAX_PROBE_INPUT_BYTES:
                raise RuntimeError("Audit probe input exceeds the read limit")
            content = self._read_exact(descriptor, int(info.st_size))
            if expected_content is not None and content != expected_content:
                raise RuntimeError("Audit probe input content changed")
            return (
                int(info.st_dev),
                int(info.st_ino),
                int(info.st_size),
                hashlib.sha256(content).hexdigest(),
            )
        finally:
            os.close(descriptor)

    def read_verified_probe_input(
        self,
        relative: str,
        expected_identity: tuple[int, int, int, str],
    ) -> str:
        """Read the one audit marker only after validating its opened identity."""
        if relative != "probe-input.txt":
            raise RuntimeError("Audit read_file permits only probe-input.txt")
        descriptor = self._open_regular_file(relative)
        try:
            info = os.fstat(descriptor)
            expected_metadata = tuple(int(value) for value in expected_identity[:3])
            actual = (int(info.st_dev), int(info.st_ino), int(info.st_size))
            if actual != expected_metadata:
                raise RuntimeError("Audit probe input identity changed")
            if info.st_size > _MAX_PROBE_INPUT_BYTES:
                raise RuntimeError("Audit probe input exceeds the read limit")
            content = self._read_exact(descriptor, int(info.st_size))
            if hashlib.sha256(content).hexdigest() != str(expected_identity[3]):
                raise RuntimeError("Audit probe input content changed")
            return content.decode("utf-8")
        finally:
            os.close(descriptor)

    def _open_regular_file(self, relative: str | PurePath) -> int:
        self._require_open()
        parts = _safe_parts(relative)
        if len(parts) != 1:
            raise RuntimeError("Audit input must be a direct child of the run")
        try:
            descriptor = os.open(
                parts[0], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.descriptor
            )
        except OSError as exc:
            raise RuntimeError("Audit input is missing or unsafe") from exc
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(descriptor)
            raise RuntimeError("Audit input is not a single-link regular file")
        return descriptor

    @staticmethod
    def _read_exact(descriptor: int, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                raise RuntimeError("Audit probe input was truncated during read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise RuntimeError("Audit probe input grew during read")
        return b"".join(chunks)

    def _require_open(self) -> None:
        if self.descriptor < 0:
            raise RuntimeError("Audit run directory is closed")


def _open_project_root(project_root: Path) -> tuple[Path, int]:
    _require_safe_primitives()
    resolved = project_root.resolve(strict=True)
    try:
        descriptor = os.open(resolved, _DIRECTORY_FLAGS | os.O_NOFOLLOW)
    except OSError as exc:
        raise RuntimeError("Project root is not a safe directory") from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise RuntimeError("Project root is not a directory")
    return resolved, descriptor


def create_audit_run(project_root: Path) -> AuditRun:
    """Create an unpredictable exclusive run directory under the ignored root."""
    resolved, descriptor = _open_project_root(project_root)
    try:
        for part in _AUDIT_ROOT_PARTS:
            child = _open_directory_at(descriptor, part, create=True)
            os.close(descriptor)
            descriptor = child
        for _ in range(10):
            run_name = f"run-{secrets.token_hex(16)}"
            try:
                os.mkdir(run_name, 0o700, dir_fd=descriptor)
                break
            except FileExistsError:
                continue
        else:  # pragma: no cover - cryptographic collision is not practical
            raise RuntimeError("Could not allocate an exclusive audit run directory")
        run_fd = _open_directory_at(descriptor, run_name, create=False)
        info = os.fstat(run_fd)
        os.fchmod(run_fd, 0o700)
        run = AuditRun(
            path=resolved.joinpath(*_AUDIT_ROOT_PARTS, run_name),
            project_root=resolved,
            run_name=run_name,
            descriptor=run_fd,
            device=info.st_dev,
            inode=info.st_ino,
        )
        try:
            run.prepare_runtime()
        except BaseException:
            run.close()
            raise
        return run
    finally:
        os.close(descriptor)


def open_audit_run(
    project_root: Path,
    run_name: str,
    *,
    device: int,
    inode: int,
) -> AuditRun:
    """Securely reopen an existing run in a spawned worker and verify identity."""
    if not run_name.startswith("run-") or not run_name[4:].isalnum():
        raise RuntimeError("Invalid audit run identifier")
    resolved, descriptor = _open_project_root(project_root)
    try:
        for part in (*_AUDIT_ROOT_PARTS, run_name):
            child = _open_directory_at(descriptor, part, create=False)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != (int(device), int(inode)):
            raise RuntimeError("Audit run directory identity changed")
        run = AuditRun(
            path=resolved.joinpath(*_AUDIT_ROOT_PARTS, run_name),
            project_root=resolved,
            run_name=run_name,
            descriptor=descriptor,
            device=info.st_dev,
            inode=info.st_ino,
        )
        descriptor = -1
        return run
    finally:
        if descriptor >= 0:
            os.close(descriptor)
