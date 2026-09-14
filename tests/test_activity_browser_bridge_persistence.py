import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from agent.runtime.activity_recorder.browser_bridge import BrowserBridge


def payload(**changes):
    value = dict(browser='edge', observedAt=100, focused=True, incognito=False,
                 title='Article', url='https://user:secret@example.org/page?secret=yes#secret')
    value.update(changes)
    return value


@pytest.fixture
def bridge(tmp_path):
    state = tmp_path / 'browser-bridge'
    result = BrowserBridge(state, ['private.example.org'], port=0, snapshot_file=state / 'snapshot.json')
    result.snapshots.clock = lambda: 105
    yield result
    result.close()


def read(bridge):
    return json.loads(bridge.snapshot_file.read_text())


@pytest.mark.parametrize('changes', [dict(incognito=True), dict(focused=False), dict(url=''),
    dict(url='file:///private'), dict(url='https://private.example.org/a'), dict(title=None)])
def test_invalidation_is_persisted_and_older_updates_cannot_revive(bridge, changes):
    bridge.snapshots.accept(payload())
    assert read(bridge)['browsers']['edge']['url'] == 'https://example.org/page'
    bridge.snapshots.accept(payload(observedAt=101, **changes))
    assert read(bridge) == {'version': 1, 'browsers': {}}
    bridge.snapshots.accept(payload())
    assert read(bridge)['browsers'] == {}


@pytest.mark.parametrize('stamp', [float('nan'), float('inf'), float('-inf'), 106, True, 10**1000])
def test_invalid_timestamps_do_not_replace_current_snapshot(bridge, stamp):
    bridge.snapshots.accept(payload())
    before = bridge.snapshot_file.read_bytes()
    bridge.snapshots.accept(payload(observedAt=stamp, incognito=True))
    assert bridge.snapshot_file.read_bytes() == before


def test_start_close_restart_and_private_permissions(bridge):
    state = bridge.snapshot_file.parent
    assert read(bridge)['browsers'] == {}
    if os.name == 'posix':
        assert state.stat().st_mode & 0o777 == 0o700
        assert (state / 'browser-token.txt').stat().st_mode & 0o777 == 0o600
    bridge.snapshots.accept(payload())
    if os.name == 'posix':
        assert bridge.snapshot_file.stat().st_mode & 0o777 == 0o600
    token = bridge.token
    bridge.close()
    assert read(bridge)['browsers'] == {}
    bridge.snapshot_file.write_text('{"stale":"data"}')
    next_bridge = BrowserBridge(state, port=0, snapshot_file=bridge.snapshot_file)
    try:
        assert next_bridge.token == token
        assert read(next_bridge)['browsers'] == {}
    finally:
        next_bridge.close()


def test_default_does_not_chmod_shared_windows_parent(tmp_path):
    if os.name == 'posix':
        tmp_path.chmod(0o755)
    original_mode = tmp_path.stat().st_mode
    result = BrowserBridge(tmp_path, port=0)
    result.close()
    assert tmp_path.stat().st_mode == original_mode
    assert not (tmp_path / 'snapshot.json').exists()


@pytest.mark.parametrize('endpoint', ['snapshot.json', 'browser-token.txt'])
@pytest.mark.skipif(os.name != 'posix', reason='POSIX symlink endpoint protection')
def test_symlink_endpoint_is_rejected_without_changing_target(tmp_path, endpoint):
    state = tmp_path / 'browser-bridge'
    state.mkdir()
    target = tmp_path / 'target'
    target.write_text('untouched')
    (state / endpoint).symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        BrowserBridge(state, port=0, snapshot_file=state / 'snapshot.json')
    assert target.read_text() == 'untouched'


def test_two_browsers_unicode_and_escaping_remain_bounded(bridge):
    for browser in ('edge', 'chrome'):
        bridge.snapshots.accept(payload(browser=browser, title='😀"\\' * 1024,
                                        url='https://example.org/' + 'a' * 4000))
    raw = bridge.snapshot_file.read_bytes()
    assert len(raw) <= 16384
    assert set(json.loads(raw)['browsers']) == {'edge', 'chrome'}
    assert b'secret' not in raw


@pytest.mark.parametrize('sig', [signal.SIGTERM, signal.SIGINT])
@pytest.mark.skipif(os.name != 'posix', reason='POSIX signal handlers perform graceful shutdown')
def test_module_process_clears_snapshot_on_signal(tmp_path, sig):
    snapshot = tmp_path / 'browser-bridge' / 'snapshot.json'
    process = subprocess.Popen([sys.executable, '-m', 'agent.runtime.activity_recorder.browser_bridge',
        '--state', str(snapshot.parent), '--snapshot-file', str(snapshot), '--port', '0',
        '--excluded-domains', 'private.example.org'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not snapshot.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert snapshot.exists()
        snapshot.write_text('{"stale":true}')
        process.send_signal(sig)
        out, err = process.communicate(timeout=5)
        assert process.returncode == 0, err
        assert json.loads(snapshot.read_text()) == {'version': 1, 'browsers': {}}
        assert not out and not err
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_malformed_unicode_url_invalidates_prior_row(bridge):
    bridge.snapshots.accept(payload())
    bridge.snapshots.accept(payload(observedAt=101, url='https://example.org/\ud800'))
    assert read(bridge)['browsers'] == {}


def test_atomic_replace_uses_private_complete_temp_and_cleans_up(bridge, monkeypatch):
    import agent.runtime.activity_recorder.browser_bridge as module
    original = module.os.replace
    seen = []
    def replace(source, destination):
        source = Path(source)
        if os.name == 'posix':
            assert source.stat().st_mode & 0o777 == 0o600
        assert json.loads(source.read_text())['browsers']['edge']['url'] == 'https://example.org/page'
        assert read(bridge)['browsers'] == {}
        seen.append(source)
        original(source, destination)
    with monkeypatch.context() as context:
        context.setattr(module.os, 'replace', replace)
        bridge.snapshots.accept(payload())
    assert seen and all(not path.exists() for path in seen)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX symlink endpoint protection')
def test_snapshot_symlink_introduced_later_is_not_followed(bridge, tmp_path):
    target = tmp_path / 'target'
    target.write_text('untouched')
    bridge.snapshot_file.unlink()
    bridge.snapshot_file.symlink_to(target)
    try:
        with pytest.raises(ValueError):
            bridge.snapshots.accept(payload())
        assert target.read_text() == 'untouched'
    finally:
        bridge.snapshot_file.unlink()


def test_shutdown_publish_failure_returns_nonzero_without_error_contents(monkeypatch, tmp_path, capsys):
    import agent.runtime.activity_recorder.browser_bridge as module
    class FailedBridge:
        def __init__(self, *args):
            pass
        def close(self):
            raise OSError('sensitive payload must not appear')
    class Stopped:
        def wait(self):
            pass
        def set(self):
            pass
    monkeypatch.setattr(module, 'BrowserBridge', FailedBridge)
    monkeypatch.setattr(module.threading, 'Event', Stopped)
    assert module.main(['--state', str(tmp_path), '--snapshot-file', str(tmp_path / 'snapshot.json')]) == 1
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('failure', ['mkstemp', 'fsync', 'replace'])
def test_publish_io_failure_removes_old_snapshot_without_disabling(bridge, monkeypatch, failure):
    import agent.runtime.activity_recorder.browser_bridge as module
    marker = bridge.snapshot_file.parent / 'enabled'
    marker.write_text('1\n')
    bridge.snapshots.accept(payload())
    assert read(bridge)['browsers']
    def fail(*args, **kwargs):
        raise OSError('simulated full disk')
    with monkeypatch.context() as context:
        context.setattr(module.tempfile if failure == 'mkstemp' else module.os, failure, fail)
        with pytest.raises(OSError):
            bridge.snapshots.accept(payload(observedAt=101, incognito=True))
    assert not bridge.snapshot_file.exists()
    assert marker.read_text() == '1\n'
    assert not list(marker.parent.glob('.snapshot-*'))
    bridge.snapshots.accept(payload(observedAt=102))
    assert read(bridge)['browsers']['edge']['observedAt'] == 102
