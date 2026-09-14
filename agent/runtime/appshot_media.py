"""Opaque session-owned Appshot PNGs. No broker authority is retained here."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import stat
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path


class AppshotMediaError(ValueError):
    pass


class AppshotMediaStore:
    def __init__(self, session_path):
        if not session_path:
            raise AppshotMediaError("session_media_unavailable")
        session = Path(session_path)
        self.path = session.parent / (session.stem + ".appshot-media")
        self._owned = {}
        self._windows = None

    def windows(self):
        if self._windows is None:
            from agent.runtime.appshot_windows_storage import WindowsMediaStore

            self._windows = WindowsMediaStore(self.path)
        return self._windows

    @contextmanager
    def directory(self, create=False, *, final_path=None):
        if sys.platform == "win32" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "getuid"):
            raise AppshotMediaError("session_media_unsupported")
        fd = None
        try:
            if create:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self.path.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            if self.path.parent.resolve() != self.path.parent.absolute():
                raise AppshotMediaError("session_media_authority")
            fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            st = os.fstat(fd)
            if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
                raise AppshotMediaError("session_media_authority")
            yield fd
            # Rename keeps this descriptor alive and checks its new name on exit.
            named = os.stat(final_path if final_path is not None else self.path, follow_symlinks=False)
            if (st.st_dev, st.st_ino, st.st_mode, st.st_uid) != (
                named.st_dev,
                named.st_ino,
                named.st_mode,
                named.st_uid,
            ):
                raise AppshotMediaError("session_media_authority")
        except OSError as exc:
            raise AppshotMediaError("session_media_unavailable") from exc
        finally:
            if fd is not None:
                os.close(fd)

    @staticmethod
    def validate_reference(ref):
        if type(ref) is not dict or set(ref) != {"media_id", "sha256", "width", "height"}:
            raise AppshotMediaError("session_media_reference")
        if (
            not isinstance(ref["media_id"], str)
            or not re.fullmatch("[0-9a-f]{32}", ref["media_id"])
            or not isinstance(ref["sha256"], str)
            or not re.fullmatch("[0-9a-f]{64}", ref["sha256"])
        ):
            raise AppshotMediaError("session_media_reference")
        if (
            any(type(ref[k]) is not int or not 1 <= ref[k] <= 16384 for k in ("width", "height"))
            or ref["width"] * ref["height"] > 32000000
        ):
            raise AppshotMediaError("session_media_reference")

    def put(self, raw, width, height):
        from agent.cli.appshots import validate_png

        validate_png(raw, width, height)
        if sys.platform == "win32":
            return self.windows().put(raw, width, height)
        media_id = uuid.uuid4().hex
        ref = dict(media_id=media_id, sha256=hashlib.sha256(raw).hexdigest(), width=width, height=height)
        temporary = media_id + ".tmp"
        final = media_id + ".png"
        with self.directory(create=True) as directory:
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory
            )
            st = os.fstat(fd)
            self._owned[media_id] = (st.st_dev, st.st_ino)
            try:
                os.fchmod(fd, 0o600)
                view = memoryview(raw)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise AppshotMediaError("session_media_write")
                    view = view[written:]
                os.fsync(fd)
                os.link(temporary, final, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                os.unlink(temporary, dir_fd=directory)
                os.fsync(directory)
                parent = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
                self.hydrate(ref)
            except BaseException:
                for name in (temporary, final):
                    try:
                        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) == (st.st_dev, st.st_ino):
                            os.unlink(name, dir_fd=directory)
                    except FileNotFoundError:
                        pass
                self._owned.pop(media_id, None)
                raise
            finally:
                os.close(fd)
        return ref

    def hydrate(self, ref):
        self.validate_reference(ref)
        if sys.platform == "win32":
            return self.windows().hydrate(ref)
        with self.directory() as directory:
            fd = os.open(
                ref["media_id"] + ".png", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory
            )
            try:
                st = os.fstat(fd)
                if (
                    not stat.S_ISREG(st.st_mode)
                    or st.st_uid != os.getuid()
                    or stat.S_IMODE(st.st_mode) != 0o600
                    or st.st_nlink != 1
                    or not 0 < st.st_size <= 10485760
                ):
                    raise AppshotMediaError("session_media_authority")
                raw = bytearray()
                while len(raw) <= st.st_size:
                    chunk = os.read(fd, min(65536, st.st_size + 1 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                after = os.fstat(fd)
                named = os.stat(ref["media_id"] + ".png", dir_fd=directory, follow_symlinks=False)
                identity = lambda s: (
                    s.st_dev,
                    s.st_ino,
                    s.st_mode,
                    s.st_uid,
                    s.st_nlink,
                    s.st_size,
                    s.st_mtime_ns,
                    s.st_ctime_ns,
                )
                if (
                    identity(st) != identity(after)
                    or identity(st) != identity(named)
                    or len(raw) != st.st_size
                    or hashlib.sha256(raw).hexdigest() != ref["sha256"]
                ):
                    raise AppshotMediaError("session_media_tampered")
            finally:
                os.close(fd)
        from agent.cli.appshots import AppshotValidationError, validate_png

        try:
            validate_png(bytes(raw), ref["width"], ref["height"])
        except AppshotValidationError as exc:
            raise AppshotMediaError("session_media_tampered") from exc
        return {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + base64.b64encode(raw).decode("ascii")},
        }

    def rollback(self, references):
        if not references:
            return
        if sys.platform == "win32":
            return self.windows().rollback(references)
        with self.directory() as directory:
            for ref in references:
                key = ref["media_id"]
                expected = self._owned.get(key)
                if expected is None:
                    continue
                try:
                    st = os.stat(key + ".png", dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (st.st_dev, st.st_ino) == expected:
                    os.unlink(key + ".png", dir_fd=directory)
                self._owned.pop(key, None)
            os.fsync(directory)

    def delete(self):
        if sys.platform == "win32":
            try:
                self.path.lstat()
            except FileNotFoundError:
                return
            return self.windows().delete()
        if not self.path.exists() and not self.path.is_symlink():
            return
        with self.directory() as directory:
            entries = []
            for name in os.listdir(directory):
                st = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (
                    not re.fullmatch("[0-9a-f]{32}\\.(png|tmp)", name)
                    or not stat.S_ISREG(st.st_mode)
                    or st.st_uid != os.getuid()
                    or stat.S_IMODE(st.st_mode) != 0o600
                    or st.st_nlink != 1
                ):
                    raise AppshotMediaError("session_media_authority")
                entries.append((name, st))
            for name, st in entries:
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (st.st_dev, st.st_ino) != (current.st_dev, current.st_ino):
                    raise AppshotMediaError("session_media_authority")
                os.unlink(name, dir_fd=directory)
            os.fsync(directory)
        self.path.rmdir()

    def rename_to(self, destination):
        if sys.platform == "win32":
            try:
                self.path.lstat()
            except FileNotFoundError:
                return
            return self.windows().rename_to(destination.path)
        if not self.path.exists() and not self.path.is_symlink():
            return
        if destination.path.exists() or destination.path.is_symlink():
            raise AppshotMediaError("session_media_exists")
        # Keep the original authority alive across the pathname operation. A
        # replacement may already have moved when detected: fail closed before
        # session history moves, without deleting/moving any unknown directory.
        with self.directory(final_path=destination.path) as original:
            expected = os.fstat(original)
            self.path.rename(destination.path)
            with destination.directory() as moved:
                actual = os.fstat(moved)
                if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
                    raise AppshotMediaError("session_media_authority")


def hydrate_content(content, session_path):
    if not isinstance(content, list):
        return content
    return [
        AppshotMediaStore(session_path).hydrate(part["appshot_image"])
        if isinstance(part, dict) and part.get("type") == "appshot_image"
        else part
        for part in content
    ]
