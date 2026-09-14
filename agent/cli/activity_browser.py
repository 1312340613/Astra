"""Explicit macOS browser bridge lifecycle; never prints captured content."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
import time

from .activity_launchd import _atomic_private_write, _missing_job

LABEL = 'com.astra.activity-browser-bridge'


def _safe(path: Path) -> Path:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Symlink state is not supported')
    return path


def state_path() -> Path:
    root = Path(os.environ.get('ASTRA_ACTIVITY_ROOT') or Path.home() / 'Library/Application Support/Astra/activity').expanduser()
    return _safe((root / 'browser-bridge').absolute())


def plist_path() -> Path:
    return _safe(Path.home() / 'Library/LaunchAgents' / f'{LABEL}.plist')


def _run(*args):
    return subprocess.run(['launchctl', *args], capture_output=True, text=True, check=False, timeout=10)


def _service() -> str:
    return f'gui/{_owner_uid()}/{LABEL}'


def _owner_uid() -> int:
    if os.name != 'posix':
        raise ValueError('POSIX ownership is required')
    return os.getuid()


def _mac_only():
    if sys.platform != 'darwin':
        raise ValueError('macOS command')


def _revoke(state: Path):
    # Revoke Swift access before launchctl: an unsuccessful stop cannot keep
    # enriching events with URLs. Do not delete pairing credentials/history.
    for name in ('enabled', 'snapshot.json'):
        _safe(state / name).unlink(missing_ok=True)


def install(*, project_root: Path, python_executable: Path, excluded_domains=()) -> dict:
    _mac_only()
    state = state_path()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    path = plist_path()
    _revoke(state)
    if path.exists():
        old = _run('bootout', _service())
        if old.returncode and not _missing_job(old):
            return {'status': 'error', 'error_type': 'LaunchctlBootoutError'}
    payload = {
        'Label': LABEL,
        'ProgramArguments': [str(python_executable), '-m', 'agent.runtime.activity_recorder.browser_bridge',
                             '--state', str(state), '--snapshot-file', str(state / 'snapshot.json'),
                             '--excluded-domains', ','.join(excluded_domains)],
        'WorkingDirectory': str(project_root),
        'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 30,
        'ProcessType': 'Background',
        'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null',
    }
    _atomic_private_write(path, plistlib.dumps(payload, sort_keys=True))
    result = _run('bootstrap', f'gui/{_owner_uid()}', str(path))
    if result.returncode:
        return {'status': 'error', 'error_type': 'LaunchctlBootstrapError', 'plist_path': str(path)}
    # A loaded job is not proof of a paired browser; status reports those stages.
    _atomic_private_write(state / 'enabled', b'1\n')
    return {'status': 'ok', 'enabled': True, 'scheduler_status': 'loaded', 'plist_path': str(path)}


def uninstall() -> dict:
    _mac_only()
    state = state_path()
    _revoke(state)
    result = _run('bootout', _service())
    if result.returncode and not _missing_job(result):
        return {'status': 'error', 'enabled': False, 'error_type': 'LaunchctlBootoutError'}
    plist_path().unlink(missing_ok=True)
    _revoke(state)  # The exiting bridge may have published an empty snapshot.
    return {'status': 'ok', 'enabled': False, 'scheduler_status': 'unloaded'}


def _read(path: Path, limit: int) -> bytes:
    if os.name != 'posix':
        raise ValueError('POSIX private-file checks are required')
    _safe(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > limit or
                info.st_uid != _owner_uid() or info.st_mode & 0o077 or not info.st_mode & 0o400):
            raise ValueError('Invalid private file')
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise ValueError('Oversized private file')
        return data
    finally:
        os.close(fd)


def status() -> dict:
    _mac_only()
    state = state_path()
    result = _run('print', _service())
    running = not result.returncode and bool(re.search(r'^\s*state = running\s*$', result.stdout, re.M))
    payload = {'status': 'ok', 'enabled': (state / 'enabled').is_file() and not (state / 'enabled').is_symlink(),
               'scheduler_status': 'running' if running else ('unloaded' if _missing_job(result) else 'not_running'),
               'snapshot_status': 'missing'}
    # Only numeric process diagnostics; never expose launchctl's environment.
    match = re.search(r'^\s*last exit code = (\d+)\s*$', result.stdout, re.M)
    if match:
        payload['last_exit_code'] = int(match[1])
    try:
        directory = state.stat()
        if directory.st_uid != _owner_uid() or directory.st_mode & 0o077:
            raise ValueError('Invalid private directory')
        if payload['enabled']:
            _read(state / 'enabled', 4096)
        snapshot = json.loads(_read(state / 'snapshot.json', 16384))
        if snapshot.get('version') != 1 or not isinstance(snapshot.get('browsers'), dict):
            raise ValueError('Invalid snapshot')
        stamps = [row.get('observedAt') for row in snapshot['browsers'].values() if isinstance(row, dict)]
        ages = [time.time() - stamp for stamp in stamps
                if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and math.isfinite(stamp)]
        fresh = [age for age in ages if 0 <= age <= 8]
        payload['snapshot_status'] = 'fresh' if fresh else ('stale' if ages else 'empty')
        if fresh:
            payload['snapshot_age_seconds'] = round(min(fresh), 2)
    except FileNotFoundError:
        pass
    except (ValueError, OSError, AttributeError):
        payload['snapshot_status'] = 'invalid'
    return payload


def copy_token() -> dict:
    _mac_only()
    token = _read(state_path() / 'browser-token.txt', 4096).decode('ascii').strip()
    if not token or any(char.isspace() for char in token):
        raise ValueError('Invalid pairing code')
    result = subprocess.run(['/usr/bin/pbcopy'], input=token, text=True, capture_output=True, check=False, timeout=5)
    return {'status': 'ok', 'copied': True} if not result.returncode else {'status': 'error', 'error_type': 'ClipboardError'}
