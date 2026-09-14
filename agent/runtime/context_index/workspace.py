"""Deterministic workspace identity resolution."""

import os
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import RLock


@dataclass(frozen=True)
class WorkspaceIdentity:
    key: str
    root: str
    label: str


_WORKSPACE_CACHE_LIMIT = 32
_WorkspaceCacheEntry = tuple[tuple[tuple[int, int, int, int, int], ...], WorkspaceIdentity]
_workspace_cache: OrderedDict[str, _WorkspaceCacheEntry] = OrderedDict()
_workspace_cache_lock = RLock()


def _stat_token(path: Path) -> tuple[int, int, int, int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (0, 0, 0, 0, 0)
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _git_metadata_token(root: Path) -> tuple[tuple[int, int, int, int, int], ...]:
    """Cheap cache invalidator for a resolved Git workspace.

    Repeated prompt builds reuse the identity until the checkout's HEAD,
    index, or .git pointer
    changes.  This works for ordinary repositories and worktrees (.git file).
    """
    git = root / ".git"
    git_dir = git
    if git.is_file():
        try:
            text = git.read_text(encoding="utf-8", errors="ignore").strip()
            if text.startswith("gitdir:"):
                candidate = Path(text.partition(":")[2].strip())
                git_dir = candidate if candidate.is_absolute() else (root / candidate)
        except OSError:
            pass
    return tuple(_stat_token(path) for path in (root, git, git_dir, git_dir / "HEAD", git_dir / "index"))


def clear_workspace_cache() -> None:
    """Clear the bounded resolver cache (primarily useful for tests)."""
    with _workspace_cache_lock:
        _workspace_cache.clear()


def _git_top_level(cwd: Path) -> Path | None:
    """Find checkout metadata without spawning a process on the reply path.

    On Windows a Git launcher can leave a child holding captured pipes after
    subprocess.run's timeout, blocking cleanup indefinitely. Directory walking
    avoids that failure entirely, including when Git is absent from PATH.
    A .git file supports both linked worktrees and submodules.
    """
    for root in (cwd, *cwd.parents):
        marker = root / ".git"
        try:
            git_dir = marker
            if marker.is_file():
                with marker.open(encoding="utf-8", errors="replace") as stream:
                    pointer = stream.readline(4096).strip()
                if not pointer.startswith("gitdir:"):
                    return None
                target = pointer.partition(":")[2].strip()
                if not target:
                    return None
                git_dir = Path(target)
                if not git_dir.is_absolute():
                    git_dir = root / git_dir
            if (git_dir / "HEAD").is_file() and (
                (git_dir / "objects").is_dir() or (git_dir / "commondir").is_file()
            ):
                return root
            if marker.exists():
                return None
        except (OSError, ValueError):
            return None
    return None


def _safe_label(value: str) -> str:
    characters: list[str] = []
    replacing = False
    for character in value:
        if character.isalnum() or character in ".-_":
            characters.append(character)
            replacing = False
        elif not replacing:
            characters.append("-")
            replacing = True
    return "".join(characters)[:80]


def resolve_workspace(cwd: Path) -> WorkspaceIdentity:
    normalized_cwd = cwd.expanduser().resolve()
    cache_key = os.path.normcase(str(normalized_cwd))
    with _workspace_cache_lock:
        cached = _workspace_cache.get(cache_key)
        if cached is not None:
            metadata, identity = cached
            if _git_metadata_token(Path(identity.root)) == metadata:
                _workspace_cache.move_to_end(cache_key)
                return identity
            _workspace_cache.pop(cache_key, None)
    # Resolve outside the lock so independent callers do not serialize their
    # filesystem probes. Fallback identities are
    # intentionally not cached, because a repository can be initialized above
    # this cwd later in the same process.
    git_root = _git_top_level(normalized_cwd)
    if git_root is None:
        root = normalized_cwd
        root_text = str(root)
        return WorkspaceIdentity(
            key=os.path.normcase(root_text).casefold(),
            root=root_text,
            label=_safe_label(root.name),
        )
    root = git_root
    root_text = str(root)
    identity = WorkspaceIdentity(
        key=os.path.normcase(root_text).casefold(),
        root=root_text,
        label=_safe_label(root.name),
    )
    metadata = _git_metadata_token(root)
    with _workspace_cache_lock:
        # A peer may have completed the same resolve while this caller was in
        # the filesystem. Recheck before mutating the shared LRU and retain that identity
        # when its checkout metadata is still current.
        cached = _workspace_cache.get(cache_key)
        if cached is not None and _git_metadata_token(Path(cached[1].root)) == cached[0]:
            _workspace_cache.move_to_end(cache_key)
            return cached[1]
        _workspace_cache[cache_key] = (metadata, identity)
        _workspace_cache.move_to_end(cache_key)
        while len(_workspace_cache) > _WORKSPACE_CACHE_LIMIT:
            _workspace_cache.popitem(last=False)
        return identity
