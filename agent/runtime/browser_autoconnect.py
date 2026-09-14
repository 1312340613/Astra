"""Discover installer-owned browser control; launch only the ordinary browser."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shlex
import sys

HOST = 'com.astra.browser_control'
BROWSERS = {'edge': ('Microsoft Edge', 'Microsoft Edge.app'),
            'chrome': ('Google/Chrome', 'Google Chrome.app')}


def _private_file(path: Path) -> bool:
    if os.name != 'posix':
        return False
    try:
        info = path.lstat()
        return path.is_file() and not path.is_symlink() and info.st_uid == os.getuid() and not info.st_mode & 0o077
    except OSError:
        return False


def _registered(browser: str, home: Path, repo: Path) -> bool:
    support = home / 'Library/Application Support'
    manifest_path = support / BROWSERS[browser][0] / 'NativeMessagingHosts' / (HOST + '.json')
    launcher = support / 'Astra/browser-control-host' / ('host-' + browser + '.sh')
    if not _private_file(manifest_path) or not _private_file(launcher):
        return False
    try:
        manifest = json.loads(manifest_path.read_text())
        origins = manifest.get('allowed_origins', [])
        lines = launcher.read_text().splitlines()
        command = shlex.split(lines[-1])
        return (
            manifest.get('name') == HOST and manifest.get('type') == 'stdio'
            and manifest.get('path') == str(launcher)
            and len(origins) == 1 and isinstance(origins[0], str)
            and re.fullmatch(r'chrome-extension://[a-p]{32}/', origins[0]) is not None
            and lines[:2] == ['#!/bin/sh', 'set -eu'] and len(lines) == 4
            and shlex.split(lines[2]) == ['cd', str(repo)]
            and len(command) == 5 and command[0] == 'exec'
            and Path(command[1]).is_file()
            and command[2:] == ['-m', 'agent.runtime.browser_control_host', '$@']
        )
    except (OSError, ValueError, TypeError, IndexError, AttributeError):
        return False


async def launch_browser(browser: str, *, home: Path | None = None) -> None:
    """Launch/reopen the installed app without CDP or a replacement profile."""
    home = home or Path.home()
    app_name = BROWSERS[browser][1]
    candidates = [Path('/Applications') / app_name, home / 'Applications' / app_name]
    app = next((path for path in candidates if path.is_dir()), None)
    if app is None:
        raise RuntimeError(f'Install {app_name} before using browser auto-connect')
    proc = await asyncio.create_subprocess_exec(
        '/usr/bin/open', '-g', '-a', str(app),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(5):
            code = await proc.wait()
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if code:
        raise RuntimeError(f'Could not launch {app_name}; open the browser and retry')


def auto_browser_options(*, home=None, repo=None, environ=None) -> dict:
    """Installed host enables listening; the extension still requires user opt-in."""
    env = os.environ if environ is None else environ
    mode = env.get('ASTRA_BROWSER_TRANSPORT', '').strip().lower()
    if mode in ('cdp', 'manual'):
        return {'auto_connect': False}
    if mode not in ('', 'auto', 'extension'):
        return {'auto_connect': True, 'setup_error': 'ASTRA_BROWSER_TRANSPORT must be auto, extension, cdp, or manual'}
    home = Path(home or Path.home())
    repo = Path(repo or Path(__file__).resolve().parents[2]).resolve()
    preferred = env.get('ASTRA_BROWSER_APP', '').strip().lower()
    if preferred and preferred not in BROWSERS:
        return {'auto_connect': True, 'setup_error': 'ASTRA_BROWSER_APP must be edge or chrome'}
    if sys.platform == 'darwin':
        for browser in ([preferred] if preferred else BROWSERS):
            if _registered(browser, home, repo):
                async def launch(selected=browser):
                    await launch_browser(selected, home=home)
                return {'auto_connect': True, 'launch_browser': launch}
    if mode in ('auto', 'extension'):
        return {'auto_connect': True, 'setup_error':
            'Browser auto-connect requires a native host installed for this checkout. '
            'Run scripts/install_browser_control_host.py on macOS and enable Auto-connect in the extension.'}
    return {'auto_connect': False}
