"""Installed Windows Appshot layout. Kept in sync with appshot-windows.ts."""
from pathlib import Path
import os
import stat

from agent.runtime.appshot_media import AppshotMediaError


def resolve_windows_appshot_helper(environ=None):
    env = os.environ if environ is None else environ
    override = env.get("ASTRA_COMPUTER_HELPER_PATH", "").strip()
    root = Path(env.get("AGENT_PROJECT_ROOT") or Path(__file__).resolve().parents[2])
    path = Path(override) if override else root / ".astra/bin/AstraWindowsComputerHelper/AstraWindowsComputerHelper.exe"
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise AppshotMediaError("session_media_native_unavailable")
        for part in (path, *path.parents[:-1]):
            if getattr(part.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise AppshotMediaError("session_media_native_unavailable")
        if not path.is_file():
            raise AppshotMediaError("session_media_native_unavailable")
    except OSError as exc:
        raise AppshotMediaError("session_media_native_unavailable") from exc
    return path


def windows_appshot_runtime(environ=None):
    env = os.environ if environ is None else environ
    local = env.get("LOCALAPPDATA", "")
    if not Path(local).is_absolute():
        raise AppshotMediaError("session_media_native_unavailable")
    return str(Path(local) / "AstraAppshot")
