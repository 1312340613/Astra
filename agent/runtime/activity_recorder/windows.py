"""Opt-in Windows foreground activity recorder; no screenshots or input capture."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from agent.runtime.activity_store import ActivityStore, SourceCursor, default_activity_db_path
from agent.runtime.activity_sync import normalize_event
from agent.runtime.process_env import hidden_process_creationflags

ROOT = Path(__file__).resolve().parents[3]
STATE = ROOT / '.astra' / 'windows-activity'
DEFAULT_EXCLUDES = frozenset({'1password.exe', 'keepass.exe', 'keepassxc.exe', 'bitwarden.exe',
                              'credentialuibroker.exe', 'logonui.exe', 'lockapp.exe'})


@dataclass(frozen=True)
class Snapshot:
    app: str
    title: str
    url: str = ''


class WindowsProbe:
    def __init__(self):
        if sys.platform != 'win32':
            raise RuntimeError('Foreground recording is supported on Windows only')
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = [
            (self.user.GetForegroundWindow, [], w.HWND),
            (self.user.GetWindowTextW, [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.user.GetWindowThreadProcessId, [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
            (self.user.OpenInputDesktop, [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            (self.user.GetUserObjectInformationW, [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
            (self.user.CloseDesktop, [w.HANDLE], w.BOOL),
            (self.kernel.OpenProcess, [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            (self.kernel.QueryFullProcessImageNameW, [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL),
            (self.kernel.CloseHandle, [w.HANDLE], w.BOOL),
            (self.kernel.GetTickCount, [], w.DWORD),
        ]
        for function, arguments, result in signatures:
            function.argtypes, function.restype = arguments, result

    def sample(self, idle_seconds: float) -> Snapshot | None:
        if sys.platform != 'win32':
            raise RuntimeError('Foreground recording is supported on Windows only')
        class LastInput(ctypes.Structure):
            _fields_ = [('cbSize', w.UINT), ('dwTime', w.DWORD)]
        info = LastInput(ctypes.sizeof(LastInput), 0)
        self.user.GetLastInputInfo.argtypes = [ctypes.POINTER(LastInput)]
        self.user.GetLastInputInfo.restype = w.BOOL
        if not self.user.GetLastInputInfo(ctypes.byref(info)):
            return None
        if ((self.kernel.GetTickCount() - info.dwTime) & 0xffffffff) / 1000 >= idle_seconds:
            return None
        desktop = self.user.OpenInputDesktop(0, False, 1)
        if not desktop:
            return None
        try:
            name, needed = ctypes.create_unicode_buffer(256), w.DWORD()
            if not self.user.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):
                return None
            if name.value.casefold() != 'default':
                return None
        finally:
            self.user.CloseDesktop(desktop)
        hwnd = self.user.GetForegroundWindow()
        if not hwnd:
            return None
        pid, title = w.DWORD(), ctypes.create_unicode_buffer(1025)
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        self.user.GetWindowTextW(hwnd, title, len(title))
        process = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not process:
            return None
        try:
            image, size = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
            if not self.kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(size)):
                return None
            if hwnd != self.user.GetForegroundWindow():
                return None
            return Snapshot(Path(image.value).name.lower(), title.value.strip())
        finally:
            self.kernel.CloseHandle(process)


class Recorder:
    """Sampling state machine. Durations are conservative sampled estimates."""
    def __init__(self, emit, *, excluded=DEFAULT_EXCLUDES, title_excludes=(), heartbeat=60, max_gap=10):
        self.emit = emit
        self.excluded = {x.casefold() for x in excluded}
        self.title_excludes = tuple(x.casefold() for x in title_excludes if x)
        self.heartbeat, self.max_gap = heartbeat, max_gap
        self.current = None
        self.last = self.flushed = 0.0
        self.duration = 0.0
        self.sequence = 0

    def _emit(self, now, reason):
        if self.current is None:
            return
        self.sequence += 1
        self.emit({
            'id': self.sequence, 'timestamp': datetime.fromtimestamp(now, timezone.utc).isoformat().replace('+00:00', 'Z'),
            'kind': 'window.changed',
            'app': {'name': self.current.app, 'bundleIdentifier': 'windows:' + self.current.app},
            'window': {'title': self.current.title, 'url': self.current.url}, 'ax': {'reason': reason},
            'platform': 'windows', 'duration_seconds': round(self.duration, 2),
        })
        self.duration = 0.0
        self.flushed = now

    def observe(self, sample: Snapshot | None, now: float):
        if sample and (sample.app.casefold() in self.excluded or
                       any(text in sample.title.casefold() for text in self.title_excludes)):
            sample = None
        gap = now - self.last
        continuous = 0 <= gap <= self.max_gap
        if self.current and (sample != self.current or not continuous):
            self._emit(self.last, 'ended')
            self.current = None
        if sample:
            if self.current is None:
                self.current = sample
                self._emit(now, 'started')
            else:
                self.duration += gap
                if now - self.flushed >= self.heartbeat:
                    self._emit(now, 'active')
        self.last = now


def write_state(data):
    temp = STATE / 'status.tmp'
    temp.write_text(json.dumps(data), encoding='utf-8')
    temp.replace(STATE / 'status.json')


def run(args):
    if sys.platform != 'win32':
        raise RuntimeError('Foreground recording is supported on Windows only')
    import msvcrt

    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'owner.lock').open('a+b') as owner:
        if owner.seek(0, 2) == 0:
            owner.write(b'0')
            owner.flush()
        owner.seek(0)
        try:
            msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise SystemExit('Windows activity recorder is already running')
        probe = WindowsProbe()
        stop, pause = STATE / 'stop', STATE / 'pause'
        stop.unlink(missing_ok=True)
        store = ActivityStore(args.store)
        segment = 'windows-' + uuid.uuid4().hex
        count = 0
        def emit(payload):
            nonlocal count
            event = normalize_event(segment, payload)
            if event is not None:
                cursor = SourceCursor('astra://recorder/' + segment, 'events', segment, payload['id'], 0, 0,
                                      payload['timestamp'], '')
                count += store.add_event_batch([event], cursor)
        excluded = DEFAULT_EXCLUDES | {x.strip().lower() for x in os.getenv('ASTRA_ACTIVITY_EXCLUDE_APPS', '').split(',') if x.strip()}
        titles = os.getenv('ASTRA_ACTIVITY_EXCLUDE_TITLES', 'InPrivate,Incognito,隐身,无痕').split(',')
        recorder = Recorder(emit, excluded=excluded, title_excludes=titles, max_gap=args.poll * 2)
        from .browser_bridge import BrowserBridge, BROWSERS
        bridge = None
        bridge_error = ''
        try:
            bridge = BrowserBridge(STATE, os.getenv('ASTRA_ACTIVITY_EXCLUDE_DOMAINS', '').split(','))
        except OSError as exc:
            bridge_error = type(exc).__name__
        child = None
        child_start = 0.0
        next_summary = time.monotonic() + args.summary_interval
        next_cleanup = 0.0
        log = (STATE / 'summarizer.log').open('ab')
        try:
            while not stop.exists():
                paused = pause.exists()
                error = ''
                try:
                    sample = None if paused else probe.sample(args.idle)
                    if paused and bridge:
                        bridge.snapshots.clear()
                    if sample and sample.app in BROWSERS.values():
                        url = bridge.snapshots.lookup(sample.app, sample.title) if bridge else None
                        # Unpaired/stale/private browser windows are not recorded.
                        sample = Snapshot(sample.app, sample.title, url) if url else None
                    recorder.observe(sample, time.time())
                except Exception as exc:
                    error = type(exc).__name__
                    recorder.observe(None, time.time())
                now = time.monotonic()
                if child and child.poll() is not None:
                    child = None
                if child and (paused or now - child_start > 240):
                    child.terminate()
                    child.wait(timeout=10)
                    child = None
                if not paused and not args.no_summaries and child is None and now >= next_summary:
                    child = subprocess.Popen(
                        [sys.executable, '-m', 'agent.runtime.activity_recorder.summarizer', '--store', str(store.path), '--lookback', '90'],
                        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                        creationflags=hidden_process_creationflags(),
                    )
                    child_start = now
                    next_summary = now + args.summary_interval
                if now >= next_cleanup:
                    store.clear((datetime.now(timezone.utc) - timedelta(days=args.retention_days)).isoformat())
                    next_cleanup = now + 3600
                write_state({'pid': os.getpid(), 'updated_at': time.time(), 'paused': paused,
                             'events_written': count, 'error': error, 'summarizer_running': child is not None,
                             'browser_bridge': 'listening' if bridge else bridge_error})
                time.sleep(args.poll)
        finally:
            if bridge:
                bridge.close()
            recorder.observe(None, time.time())
            if child and child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
            log.close()
            store.close()
            write_state({'pid': os.getpid(), 'updated_at': time.time(), 'stopped': True, 'events_written': count})


def main(argv=None):
    from agent.cli.environment import load_project_env
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, default=default_activity_db_path())
    parser.add_argument('--poll', type=float, default=5)
    parser.add_argument('--idle', type=float, default=120)
    parser.add_argument('--retention-days', type=int, default=30)
    parser.add_argument('--summary-interval', type=float, default=600)
    parser.add_argument('--no-summaries', action='store_true')
    parser.add_argument('--clear-history', action='store_true')
    args = parser.parse_args(argv)
    if sys.platform != 'win32':
        parser.error('This recorder is Windows-only; macOS uses its native recorder')
    if not (1 <= args.poll <= 30 and args.idle >= args.poll and 1 <= args.retention_days <= 365 and args.summary_interval >= 60):
        parser.error('Invalid polling, idle, retention, or summary interval')
    if args.clear_history:
        import msvcrt
        import sqlite3
        from agent.runtime.context_index.vector_index import default_vectors_db_path

        STATE.mkdir(parents=True, exist_ok=True)
        with (STATE / 'owner.lock').open('a+b') as owner:
            if owner.seek(0, 2) == 0:
                owner.write(b'0')
                owner.flush()
            owner.seek(0)
            try:
                msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                parser.error('Stop the activity recorder before clearing history')
            if args.store.is_file():
                store = ActivityStore(args.store)
                try:
                    print(json.dumps(store.clear(datetime.now(timezone.utc).isoformat())))
                finally:
                    store.close()
            vectors = default_vectors_db_path()
            if vectors.is_file():
                with sqlite3.connect(vectors) as connection:
                    connection.execute('DELETE FROM context_index_vectors')
            print('Activity history and current embedding index cleared; this cannot be undone.')
        return
    run(args)


if __name__ == '__main__':
    main()
