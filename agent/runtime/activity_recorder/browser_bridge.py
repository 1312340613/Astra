"""Authenticated loopback snapshots from the optional browser extension."""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import tempfile
import threading
import time
from urllib.parse import urlsplit

from agent.runtime.activity_store import sanitize_url

BROWSERS = {'edge': 'msedge.exe', 'chrome': 'chrome.exe'}


class BrowserSnapshots:
    def __init__(self, excluded_domains=(), clock=time.time, publish=None):
        self.excluded = tuple(x.strip().lower().strip('.') for x in excluded_domains if x.strip())
        self.clock = clock
        self.publish = publish
        self.rows = {}
        self.latest = {}
        self.lock = threading.Lock()

    def accept(self, payload):
        browser = payload.get('browser')
        if browser not in BROWSERS:
            raise ValueError('Unknown browser')
        app = BROWSERS[browser]
        with self.lock:
            stamp = payload.get('observedAt')
            if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
                return
            try:
                valid = math.isfinite(stamp) and 0 <= self.clock() - stamp <= 8
            except OverflowError:
                valid = False
            if not valid or stamp <= self.latest.get(app, float('-inf')):
                return
            self.latest[app] = stamp
            self.rows.pop(app, None)
            # Every newer observation publishes, including privacy invalidations.
            try:
                if payload.get('focused') is not True or payload.get('incognito') is not False:
                    return
                raw_url, title = payload.get('url'), payload.get('title')
                if not isinstance(raw_url, str) or not isinstance(title, str) or not title.strip():
                    return
                url = sanitize_url(raw_url)
                if not url or len(url) > 4096:
                    return
                try:
                    url.encode('utf-8')
                except UnicodeError:
                    return
                host = urlsplit(url).hostname or ''
                if any(host == domain or host.endswith('.' + domain) for domain in self.excluded):
                    return
                self.rows[app] = (stamp, title[:1024], url)
            finally:
                if self.publish is not None:
                    self.publish(self.rows)

    def lookup(self, app, window_title):
        with self.lock:
            row = self.rows.get(app)
        if row is None or not 0 <= self.clock() - row[0] <= 8:
            return None
        title = row[1]
        # Match the OS foreground window, not another browser's last-used tab.
        suffix = window_title[len(title):] if window_title.startswith(title) else None
        if suffix is None or (suffix and not re.match(
            r'^(?: 和另外 \d+ 个页面| and \d+ more pages?)? - ', suffix
        )):
            return None
        return row[2]

    def clear(self):
        with self.lock:
            self.rows.clear()
            if self.publish is not None:
                self.publish(self.rows)


class BrowserBridge:
    def __init__(self, state: Path, excluded_domains=(), port=8089, snapshot_file=None):
        state = Path(state)
        self.snapshot_file = Path(snapshot_file) if snapshot_file is not None else None
        if self.snapshot_file is not None:
            # Persistence opts into a dedicated state directory. The Windows
            # default may use a shared activity root and must not chmod it.
            if self.snapshot_file.parent != state or state.is_symlink():
                raise ValueError('Invalid browser bridge state directory')
            state.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not state.is_dir() or (os.name == 'posix' and state.stat().st_uid != os.getuid()):
                raise ValueError('Invalid browser bridge state directory')
            state.chmod(0o700)
            _regular_endpoint(self.snapshot_file)
        self.token = _private_token(state / 'browser-token.txt')
        self.snapshots = BrowserSnapshots(excluded_domains, publish=self._publish if self.snapshot_file else None)
        self.snapshots.clear()
        self._closed = False
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(1)

            def log_message(self, format: str, *args):
                pass  # Never log browser titles, URLs, or credentials.

            def do_POST(self):
                origin = self.headers.get('Origin', '')
                if origin and not re.fullmatch(r'chrome-extension://[a-p]{32}', origin):
                    self.send_error(403)
                    return
                expected = 'Bearer ' + bridge.token
                if not hmac.compare_digest(self.headers.get('Authorization', ''), expected):
                    self.send_error(401)
                    return
                if self.path != '/snapshot':
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 16384:
                        raise ValueError('size')
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError('object')
                    bridge.snapshots.accept(payload)
                except (ValueError, TypeError, UnicodeError):
                    self.send_error(400)
                    return
                except OSError:
                    self.send_error(503)
                    return
                self.send_response(204)
                self.end_headers()

        self.server = HTTPServer(('127.0.0.1', port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name='astra-browser-bridge')
        self.thread.start()

    def _publish(self, rows):
        # Called while BrowserSnapshots.lock is held, keeping write ordering
        # identical to observation ordering and preventing a late stale write.
        snapshot_file = self.snapshot_file
        if snapshot_file is None:
            return
        document = {'version': 1, 'browsers': {
            browser: {
                'observedAt': rows[app][0],
                'title': rows[app][1].encode('utf-8', errors='replace')[:1024].decode('utf-8', errors='ignore'),
                'url': rows[app][2],
            }
            for browser, app in BROWSERS.items() if app in rows
        }}
        encoded = json.dumps(document, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(encoded) > 16384:
            # Worst-case Unicode/JSON escaping can exceed the on-disk budget.
            # Publish a safe empty snapshot instead of retaining an older URL.
            encoded = b'{"version":1,"browsers":{}}'
        _regular_endpoint(snapshot_file)
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix='.snapshot-', dir=snapshot_file.parent)
            with os.fdopen(fd, 'wb') as output:
                os.chmod(temporary, 0o600)
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            _regular_endpoint(snapshot_file)
            os.replace(temporary, snapshot_file)
        except OSError:
            # An old row must not survive a failed privacy invalidation. Unlink
            # removes this endpoint, never a symlink's target. Keep enabled so
            # the recorder continues to suppress untrusted AXURL fallback.
            snapshot_file.unlink(missing_ok=True)
            raise
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    def close(self):
        if self._closed:
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.snapshots.clear()
        self._closed = True


def _regular_endpoint(path):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError('Browser bridge endpoint must be a regular file')


def _private_token(path):
    _regular_endpoint(path)
    flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        fd = os.open(path, flags)
        created = False
    with os.fdopen(fd, 'r+', encoding='ascii') as token_file:
        metadata = os.fstat(token_file.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024:
            raise ValueError('Invalid browser bridge token file')
        if os.name == 'posix':
            os.fchmod(token_file.fileno(), 0o600)
        else:
            path.chmod(0o600)
        if created:
            token = secrets.token_urlsafe(32)
            token_file.write(token)
            token_file.flush()
            os.fsync(token_file.fileno())
        else:
            token = token_file.read(1025).strip()
        if not token or not re.fullmatch(r'[A-Za-z0-9_-]{1,1024}', token):
            raise ValueError('Invalid browser bridge token file')
        return token


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the private loopback browser bridge')
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--snapshot-file', required=True, type=Path)
    parser.add_argument('--port', default=8089, type=int)
    parser.add_argument('--excluded-domains', default='')
    args = parser.parse_args(argv)
    stopped = threading.Event()
    def stop(signum, frame):
        stopped.set()
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    bridge = None
    result = 0
    try:
        bridge = BrowserBridge(args.state, args.excluded_domains.split(','), args.port, args.snapshot_file)
        stopped.wait()
    except (OSError, ValueError, UnicodeError):
        result = 1  # Never include paths, payloads or credentials in process logs.
    finally:
        try:
            if bridge is not None:
                bridge.close()
        except (OSError, ValueError):
            result = 1
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return result


if __name__ == '__main__':
    raise SystemExit(main())
