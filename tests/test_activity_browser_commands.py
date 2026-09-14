import json
import os
from pathlib import Path
import plistlib
from types import SimpleNamespace

import pytest

from agent.cli import activity_browser, activity_commands


def configure(tmp_path, monkeypatch):
    if os.name != 'posix':
        pytest.skip('macOS bridge lifecycle uses POSIX ownership and LaunchAgent permissions')
    monkeypatch.setattr(activity_browser.sys, 'platform', 'darwin')
    monkeypatch.setenv('ASTRA_ACTIVITY_ROOT', str(tmp_path / 'activity'))
    monkeypatch.setattr(activity_browser.Path, 'home', lambda: tmp_path / 'home')
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if 'print' in args:
            return SimpleNamespace(returncode=113, stdout='', stderr='Could not find service')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(activity_browser.subprocess, 'run', run)
    return calls


def test_browser_install_is_explicit_private_and_uses_current_python(tmp_path, monkeypatch):
    calls = configure(tmp_path, monkeypatch)
    result = activity_browser.install(project_root=tmp_path / 'project', python_executable=Path('/env/python'), excluded_domains=['private.example'])
    assert result['status'] == 'ok'
    state = tmp_path / 'activity/browser-bridge'
    assert (state / 'enabled').read_text() == '1\n'
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / 'enabled').stat().st_mode & 0o777 == 0o600
    plist = plistlib.loads(Path(result['plist_path']).read_bytes())
    assert plist['ProgramArguments'][:3] == ['/env/python', '-m', 'agent.runtime.activity_recorder.browser_bridge']
    assert plist['WorkingDirectory'] == str(tmp_path / 'project')
    assert '--snapshot-file' in plist['ProgramArguments']
    assert plist['StandardOutPath'] == plist['StandardErrorPath'] == '/dev/null'
    assert any('bootstrap' in call for call in calls)
    assert not (state / 'browser-token.txt').exists()  # Only the bridge creates credentials.


def test_failed_bootstrap_does_not_enable_reader(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    monkeypatch.setattr(activity_browser.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='', stderr='failure'))
    result = activity_browser.install(project_root=tmp_path, python_executable=Path('/env/python'))
    assert result['status'] == 'error'
    assert not (tmp_path / 'activity/browser-bridge/enabled').exists()


def test_uninstall_revokes_reader_before_stopping_service(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    state = tmp_path / 'activity/browser-bridge'
    state.mkdir(parents=True)
    (state / 'enabled').write_text('1\n')
    (state / 'snapshot.json').write_text('private url')
    def run(*args, **kwargs):
        assert not (state / 'enabled').exists()
        assert not (state / 'snapshot.json').exists()
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(activity_browser.subprocess, 'run', run)
    assert activity_browser.uninstall()['status'] == 'ok'


def test_status_reports_freshness_without_url_title_or_token(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    monkeypatch.setattr(activity_browser.time, 'time', lambda: 100)
    state = tmp_path / 'activity/browser-bridge'
    state.mkdir(parents=True)
    (state / 'enabled').write_text('1\n')
    (state / 'snapshot.json').write_text(json.dumps({'version': 1, 'browsers': {'edge': {'observedAt': 98, 'url': 'https://secret.example', 'title': 'Private title'}}}))
    state.chmod(0o700)
    (state / 'enabled').chmod(0o600)
    (state / 'snapshot.json').chmod(0o600)
    result = activity_browser.status()
    assert result['snapshot_status'] == 'fresh'
    assert result['snapshot_age_seconds'] == 2
    assert 'secret' not in json.dumps(result) and 'Private' not in json.dumps(result)


def test_symlink_state_is_refused_without_mutating_target(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    target = tmp_path / 'outside'
    target.mkdir()
    state = tmp_path / 'activity/browser-bridge'
    state.parent.mkdir()
    state.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        activity_browser.install(project_root=tmp_path, python_executable=Path('/env/python'))
    assert list(target.iterdir()) == []


def test_browser_command_does_not_open_activity_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(activity_browser, 'status', lambda: {'status': 'ok'})
    def forbidden():
        raise AssertionError('browser status should not open history DB')
    assert activity_commands.execute_activity_command(['browser-status'], store_factory=forbidden) == 0
    assert json.loads(capsys.readouterr().out)['action'] == 'browser-status'


@pytest.mark.parametrize('operation', ['install', 'uninstall', 'status', 'copy_token'])
def test_browser_lifecycle_rejects_windows_before_side_effects(tmp_path, monkeypatch, operation):
    monkeypatch.setattr(activity_browser, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setenv('ASTRA_ACTIVITY_ROOT', str(tmp_path / 'activity'))
    kwargs = {'project_root': tmp_path, 'python_executable': tmp_path / 'python'} if operation == 'install' else {}
    with pytest.raises(ValueError, match='macOS command'):
        getattr(activity_browser, operation)(**kwargs)
    assert list(tmp_path.iterdir()) == []


def test_pairing_code_is_copied_without_printing_secret(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    state = tmp_path / 'activity/browser-bridge'
    state.mkdir(parents=True)
    (state / 'browser-token.txt').write_text('test-pairing-secret')
    (state / 'browser-token.txt').chmod(0o600)
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(activity_browser.subprocess, 'run', run)
    result = activity_browser.copy_token()
    assert result == {'status': 'ok', 'copied': True}
    assert calls[0][0] == ['/usr/bin/pbcopy']
    assert calls[0][1]['input'] == 'test-pairing-secret'
    assert 'test-pairing-secret' not in json.dumps(result)


@pytest.mark.parametrize('unsafe', ['directory', 'enabled', 'snapshot.json'])
def test_status_marks_nonprivate_reader_state_invalid(tmp_path, monkeypatch, unsafe):
    configure(tmp_path, monkeypatch)
    state = tmp_path / 'activity/browser-bridge'
    state.mkdir(parents=True, mode=0o700)
    (state / 'enabled').write_text('1\n')
    (state / 'snapshot.json').write_text(json.dumps({'version': 1, 'browsers': {'edge': {'observedAt': 98}}}))
    for name in ('enabled', 'snapshot.json'):
        (state / name).chmod(0o600)
    (state if unsafe == 'directory' else state / unsafe).chmod(0o755 if unsafe == 'directory' else 0o644)
    assert activity_browser.status()['snapshot_status'] == 'invalid'
