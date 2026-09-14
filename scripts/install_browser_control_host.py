#!/usr/bin/env python3
"""Register the opt-in Astra Native Messaging host for macOS Edge or Chrome."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import sys

HOST_NAME = 'com.astra.browser_control'
BROWSERS = {'edge':'Microsoft Edge', 'chrome':'Google/Chrome'}


def _paths(browser, home):
    if browser not in BROWSERS: raise ValueError('Choose edge or chrome')
    support = Path(home) / 'Library/Application Support'
    private = support / 'Astra/browser-control-host'
    return {'manifest':support / BROWSERS[browser] / 'NativeMessagingHosts' / (HOST_NAME+'.json'), 'launcher':private / ('host-'+browser+'.sh')}


def install(*, extension_id, browser='edge', home=None, repo=None, python=None):
    if not re.fullmatch('[a-p]{32}', extension_id):
        raise ValueError('An exact Chromium extension ID ([a-p]{32}) is required')
    if sys.platform != 'darwin': raise RuntimeError('This installer supports macOS only')
    repo = Path(repo or Path(__file__).resolve().parents[1]).resolve()
    # Do not resolve the venv executable symlink: its directory selects its venv.
    python = Path(python or sys.executable).absolute()
    if not (repo / 'agent/runtime/browser_control_host.py').is_file() or not python.is_file():
        raise ValueError('A stable repository and Python interpreter are required')
    paths = _paths(browser, home or Path.home())
    if any(p.exists() or p.is_symlink() for p in paths.values()):
        raise FileExistsError('Host registration already exists; uninstall it before replacing')
    launcher = '#!/bin/sh\nset -eu\ncd '+shlex.quote(str(repo))+'\nexec '+shlex.quote(str(python))+' -m agent.runtime.browser_control_host "$@"\n'
    manifest = {'name':HOST_NAME,'description':'Astra opt-in browser control','path':str(paths['launcher']),'type':'stdio','allowed_origins':['chrome-extension://'+extension_id+'/']}
    created=[]
    try:
        paths['launcher'].parent.mkdir(parents=True,mode=0o700,exist_ok=True)
        private=paths['launcher'].parent
        if private.is_symlink() or private.stat().st_uid != os.getuid() or private.stat().st_mode & 0o077:
            raise PermissionError('Host launcher directory must be private and user-owned')
        paths['manifest'].parent.mkdir(parents=True,exist_ok=True)
        for key, content, mode in [('launcher',launcher,0o700),('manifest',json.dumps(manifest,indent=2)+'\n',0o600)]:
            fd=os.open(paths[key],os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode)
            created.append(paths[key])
            with os.fdopen(fd,'w') as output:
                output.write(content); output.flush(); os.fsync(output.fileno())
    except BaseException:
        for path in reversed(created): path.unlink(missing_ok=True)
        raise
    return paths


def uninstall(*, browser='edge', home=None):
    paths=_paths(browser,home or Path.home())
    if paths['manifest'].is_symlink() or paths['launcher'].is_symlink():
        raise PermissionError('Refusing to remove symlinked host files')
    if paths['manifest'].exists():
        manifest=json.loads(paths['manifest'].read_text())
        if manifest.get('name') != HOST_NAME or manifest.get('path') != str(paths['launcher']):
            raise ValueError('Registration does not belong to this installer')
        paths['manifest'].unlink()
    paths['launcher'].unlink(missing_ok=True)
    return paths


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser',choices=list(BROWSERS),default='edge')
    parser.add_argument('--extension-id')
    parser.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--python',type=Path,default=Path(sys.executable))
    parser.add_argument('--uninstall',action='store_true')
    args=parser.parse_args()
    if sys.platform != 'darwin': parser.error('macOS is currently supported')
    if args.uninstall:
        paths=uninstall(browser=args.browser)
    else:
        if not args.extension_id: parser.error('--extension-id is required')
        if '.worktrees' in args.repo.parts or 'worktrees' in args.repo.parts:
            parser.error('--repo must point to a stable checkout, not a temporary worktree')
        paths=install(extension_id=args.extension_id,browser=args.browser,repo=args.repo,python=args.python)
    print(('Removed' if args.uninstall else 'Installed')+' native host: '+str(paths['manifest']))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
