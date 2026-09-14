"""Private, single-owner Native Messaging bridge. Requests are never replayed."""
from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path
import secrets
import stat
import struct
import sys
import uuid

MAX_FRAME = 1024 * 1024
OPERATIONS = frozenset({'tabs','attach','open','snapshot','click','type','fill','check','read','select','wait','screenshot','handoff','resume','close'})
WRITES = frozenset({'open','click','type','fill','check','select','close','handoff','resume'})


class BrowserUnsupportedOperation(RuntimeError):
    """A controller rejected an operation before any page input was dispatched."""

    def __init__(self, operation: str):
        self.operation = operation
        super().__init__(f'Extension controller does not support {operation}; no page input was dispatched')

    def result(self) -> dict:
        return {'status': 'unsupported_operation', 'operation': self.operation,
                'message': str(self), 'dispatch_state': 'not_dispatched',
                'next_step': 'use_available_channel', 'repeat_input': False,
                'recovery_hint': 'Do not change CSS/ref syntax or repeat this operation. '
                    'Use current snapshot capabilities and an available action, or reload Astra Browser Control.'}

BROWSER_ENDPOINT_RECOVERY = (
    'No browser input was dispatched. This runtime cannot use the extension endpoint now. '
    'For an ordinary UI task, if the user has not required the browser channel and native '
    'computer tools are available, call computer_apps and bind the same visible target. '
    'Otherwise use the owning runtime or let the user exit it normally. '
    'Do not repeat browser reads, run shell diagnostics, delete the lock, or kill the owner '
    'just to complete a form; diagnose processes only when that is the user task.'
)


class BrowserEndpointOwnedError(RuntimeError):
    code = 'browser_endpoint_owned'

    def __init__(self, owner_pid: int | None):
        owner_pid = owner_pid if type(owner_pid) is int and 0 < owner_pid <= 2**31 - 1 else None
        self.owner_pid = owner_pid
        owner = f' (owner metadata reports Astra PID {owner_pid})' if owner_pid is not None else ''
        super().__init__(
            f'Browser control endpoint is already owned{owner}. '
            'Another Astra runtime holds the local endpoint lock before browser attachment. '
            'Use that runtime, or exit it normally and retry here. '
            'The extension Disconnect button does not release this process lock; '
            'do not delete owner.lock or attribute this error to a browser debugger.'
        )


def default_endpoint_dir() -> Path:
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/Astra/browser-control'
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'Astra/browser-control'
    return Path(os.environ.get('XDG_RUNTIME_DIR', Path.home() / '.local/state')) / 'astra/browser-control'


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise PermissionError('Browser control directory must not be a symlink')
    if os.name != 'nt' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise PermissionError('Browser control directory must be user-owned and mode 0700')


def encode_frame(value: dict) -> bytes:
    body = json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    if not 0 < len(body) <= MAX_FRAME:
        raise ValueError('Browser control frame exceeds 1MiB limit')
    return struct.pack('<I', len(body)) + body


async def read_frame(reader: asyncio.StreamReader) -> dict:
    size = struct.unpack('<I', await reader.readexactly(4))[0]
    if not 0 < size <= MAX_FRAME:
        raise ValueError('Invalid browser control frame length')
    result = json.loads(await reader.readexactly(size))
    if not isinstance(result, dict):
        raise ValueError('Browser control frame must be an object')
    return result


class BrowserControlTransport:
    def __init__(self, endpoint_dir: Path | str | None = None, *, timeout: float = 15):
        self.directory = Path(endpoint_dir) if endpoint_dir is not None else default_endpoint_dir()
        self.descriptor_path = self.directory / 'endpoint.json'
        self.timeout = timeout
        self._server = None
        self._client = None
        self._pending = {}
        self._tasks = set()
        self._lock_fd = None
        self._token = secrets.token_urlsafe(32)
        self._start_lock = asyncio.Lock()
        self.generation = 0
        self._connection_changed = asyncio.Event()
        self._closed = False
        self._ready_client = None
        self.controller_capabilities: dict = {}
        self._unsupported_operations: set[str] = set()

    def operation_support(self, operation: str) -> bool | None:
        if operation in self._unsupported_operations:
            return False
        operations = self.controller_capabilities.get('operations')
        if operations is not None:
            return operation in operations
        # Protocol-1's original controller supports click, but check was added
        # without a wire-version bump. A page capability cannot establish it.
        return None if operation in {'check', 'screenshot'} else operation in OPERATIONS

    @staticmethod
    def _validate_capabilities(value: object) -> dict:
        if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
            raise ValueError('Invalid controller capabilities')
        operations = value.get('operations')
        if (not isinstance(operations, list) or len(operations) > 64
                or any(not isinstance(op, str) or not op or len(op) > 64 for op in operations)):
            raise ValueError('Invalid controller operation list')
        version = value.get('extensionVersion', '')
        revision = value.get('controllerVersion', 0)
        if not isinstance(version, str) or len(version) > 80 or type(revision) is not int or revision < 0:
            raise ValueError('Invalid controller version')
        return {'version': 1, 'extensionVersion': version, 'controllerVersion': revision,
                'operations': list(dict.fromkeys(operations))}

    @property
    def connected(self) -> bool:
        return self._client is not None and not self._client.is_closing()

    @property
    def ready(self) -> bool:
        return self.connected and self._ready_client is self._client

    def endpoint_availability(self) -> dict:
        """Probe an existing lock without connecting or trusting stale PID metadata."""
        if self._closed:
            return {'state': 'closed'}
        if self._lock_fd is not None:
            return {'state': 'ready' if self.ready else 'owned_here'}
        try:
            directory = self.directory.lstat()
        except FileNotFoundError:
            return {'state': 'available'}
        except OSError:
            return {'state': 'unavailable'}
        if not stat.S_ISDIR(directory.st_mode) or self.directory.is_symlink():
            return {'state': 'unavailable'}
        if os.name != 'nt' and (directory.st_uid != os.getuid() or directory.st_mode & 0o077):
            return {'state': 'unavailable'}
        try:
            fd = os.open(self.directory / 'owner.lock', os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        except FileNotFoundError:
            return {'state': 'available'}
        except OSError:
            return {'state': 'unavailable'}
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (os.name != 'nt' and (info.st_uid != os.getuid() or info.st_mode & 0o077)):
                return {'state': 'unavailable'}
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    return {'state': 'owned_elsewhere', 'owner_pid': self._reported_owner_pid()}
                return {'state': 'unavailable'}
            return {'state': 'available'}
        finally:
            os.close(fd)

    async def start(self) -> None:
        async with self._start_lock:
            if self._server is not None:
                return
            self._closed = False
            private_directory(self.directory)
            fd = os.open(self.directory / 'owner.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                if os.name == 'nt':
                    import msvcrt
                    os.write(fd, b'0'); os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(fd)
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                raise BrowserEndpointOwnedError(self._reported_owner_pid()) from None
            self._lock_fd = fd
            try:
                self._server = await asyncio.start_server(self._accept, '127.0.0.1', 0)
                descriptor = {'port':self._server.sockets[0].getsockname()[1], 'token':self._token, 'pid':os.getpid()}
                temp = self.directory / f'.endpoint-{uuid.uuid4().hex}'
                out = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(out, 'w') as stream:
                    json.dump(descriptor, stream); stream.flush(); os.fsync(stream.fileno())
                os.replace(temp, self.descriptor_path)
            except BaseException:
                await self._close_unlocked()
                raise

    def _reported_owner_pid(self) -> int | None:
        """Read advisory ownership metadata without exposing the endpoint token."""
        try:
            fd = os.open(self.descriptor_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                    return None
                if os.name != 'nt' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
                    return None
                value = json.loads(stream.read(4097))
            pid = value.get('pid') if isinstance(value, dict) else None
            return pid if type(pid) is int and 0 < pid <= 2**31 - 1 else None
        except (OSError, ValueError):
            return None

    async def wait_connected(self, timeout: float = 45) -> None:
        """Wait only for authentication; never enqueue or repeat browser actions."""
        await self.start()
        try:
            async with asyncio.timeout(timeout):
                while True:
                    self._connection_changed.clear()
                    if self._closed:
                        raise ConnectionError('Browser control runtime closed while connecting')
                    if self.ready:
                        return
                    await self._connection_changed.wait()
        except TimeoutError:
            raise TimeoutError(
                'Browser auto-connect timed out. Enable Auto-connect in Astra Browser '
                'Control, or use Connect to Astra; check that the native host is installed.'
            ) from None

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            auth = await asyncio.wait_for(read_frame(reader), 3)
            token = auth.get('token')
            if not isinstance(token, str) or not secrets.compare_digest(token, self._token) or self.connected:
                return
            self._client = writer
            self.controller_capabilities = {}
            self._unsupported_operations.clear()
            self.generation += 1
            writer.write(encode_frame({'ok': True})); await writer.drain()
            self._connection_changed.set()
            while True:
                response = await read_frame(reader)
                request_id = response.get('id')
                if request_id == 'astra_control_capabilities':
                    if self.ready or response.get('ok') is not True:
                        raise ValueError('Unexpected controller capability announcement')
                    self.controller_capabilities = self._validate_capabilities(response.get('result'))
                    continue
                if request_id == 'astra_control_ready':
                    if response.get('ok') is not True or response.get('result') != {'version':1}:
                        raise ValueError('Invalid browser readiness version')
                    self._ready_client = writer
                    self._connection_changed.set()
                    continue
                if not isinstance(request_id, str):
                    raise ValueError('Invalid response ID')
                entry = self._pending.get(request_id)
                if entry is not None and entry[0] is writer and not entry[1].done():
                    entry[1].set_result(response)
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        finally:
            if self._client is writer:
                self._client = None
                self._ready_client = None
                self.controller_capabilities = {}
                self._unsupported_operations.clear()
                self._connection_changed.set()
            for _, (owner, future, operation) in list(self._pending.items()):
                if owner is writer and not future.done():
                    future.set_exception(ConnectionError(self._failure(operation, 'disconnected')))
            writer.close()
            try: await writer.wait_closed()
            except OSError: pass
            self._tasks.discard(task)

    @staticmethod
    def _failure(operation, detail):
        return f'{"unknown_outcome" if operation in WRITES else "error"}: browser control {detail}; request will not be replayed'

    async def request(self, operation: str, *, tab_id=None, args=None) -> dict:
        if operation not in OPERATIONS:
            raise ValueError('Unsupported browser control operation')
        if args is not None and not isinstance(args, dict):
            raise ValueError('Browser control arguments must be an object')
        await self.start()
        writer = self._client
        if writer is None or writer.is_closing():
            raise ConnectionError('Browser control extension is not connected; enable it in the extension popup')
        if not self.ready:
            raise ConnectionError('Browser control extension is restoring grants; wait for ready before sending an action')
        if self.operation_support(operation) is False:
            raise BrowserUnsupportedOperation(operation)
        request_id = uuid.uuid4().hex
        payload = {'id':request_id, 'operation':operation, 'args':args or {}}
        if tab_id is not None: payload['tabId'] = tab_id
        frame = encode_frame(payload)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (writer, future, operation)
        try:
            writer.write(frame)
            async with asyncio.timeout(self.timeout):
                await writer.drain()
                response = await future
            if response.get('ok') is not True:
                error = response.get('error')
                if isinstance(error, dict) and error.get('status') == 'unknown_outcome':
                    return error
                # Our arguments are known to be an object. This exact legacy
                # whitelist rejection therefore proves the operation was absent.
                if (response.get('code') == 'unsupported_operation'
                        and response.get('dispatch_state') == 'not_dispatched') or error == 'Unsupported operation or arguments':
                    self._unsupported_operations.add(operation)
                    raise BrowserUnsupportedOperation(operation)
                raise RuntimeError(str(response.get('error', 'Browser control operation failed')))
            result = response.get('result', {})
            if not isinstance(result, dict): raise ValueError('Invalid browser result')
            return result
        except asyncio.TimeoutError:
            raise TimeoutError(self._failure(operation, 'timed out')) from None
        except OSError as exc:
            raise ConnectionError(self._failure(operation, 'disconnected')) from exc
        finally:
            self._pending.pop(request_id, None)
            if not future.done(): future.cancel()

    async def close(self):
        self._closed = True
        self._connection_changed.set()
        async with self._start_lock:
            await self._close_unlocked()

    async def _close_unlocked(self):
        self._closed = True
        self._connection_changed.set()
        if self._server is not None:
            self._server.close(); await self._server.wait_closed(); self._server = None
        if self._client is not None:
            self._client.close()
        tasks = [task for task in self._tasks if task is not asyncio.current_task()]
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        if self._lock_fd is not None:
            try:
                if json.loads(self.descriptor_path.read_text()).get('token') == self._token:
                    self.descriptor_path.unlink()
            except (OSError, ValueError): pass
            os.close(self._lock_fd); self._lock_fd = None
