import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

from agent.runtime.activity_recorder.browser_bridge import BrowserBridge, BrowserSnapshots
from agent.runtime.activity_recorder.windows import Recorder, Snapshot


def payload(**changes):
    return dict(browser='edge', focused=True, incognito=False, title='Article',
                url='https://user:secret@example.com/article?token=secret#private', observedAt=100, **changes)


def test_url_sanitization_and_foreground_match():
    snapshots = BrowserSnapshots(clock=lambda: 100)
    snapshots.accept(payload())
    assert snapshots.lookup('msedge.exe', 'Article - Microsoft Edge') == 'https://example.com/article'
    assert snapshots.lookup('msedge.exe', 'Article 和另外 32 个页面 - 个人 - Microsoft\u200b Edge') == 'https://example.com/article'
    assert snapshots.lookup('msedge.exe', 'Article and 32 more pages - Profile - Microsoft Edge') == 'https://example.com/article'
    assert snapshots.lookup('msedge.exe', 'Article unrelated page - Microsoft Edge') is None
    assert snapshots.lookup('chrome.exe', 'Article - Google Chrome') is None
    assert snapshots.lookup('msedge.exe', 'Other page - Microsoft Edge') is None
    rows = []
    Recorder(rows.append).observe(Snapshot('msedge.exe', 'Article', snapshots.lookup('msedge.exe', 'Article')), 100)
    assert 'secret' not in json.dumps(rows)


@pytest.mark.parametrize('changes', [dict(incognito=True), dict(focused=False), dict(incognito=None),
    dict(url='file:///private.txt'), dict(url='chrome://settings'), dict(url='https://private.example.com/a')])
def test_private_unknown_and_excluded_pages_clear_prior_url(changes):
    snapshots = BrowserSnapshots(['private.example.com'], clock=lambda: 101)
    snapshots.accept(payload())
    update = payload()
    update.update(observedAt=101, **changes)
    snapshots.accept(update)
    assert snapshots.lookup('msedge.exe', 'Article') is None


def test_stale_snapshot_and_out_of_order_updates():
    now = [100]
    snapshots = BrowserSnapshots(clock=lambda: now[0])
    snapshots.accept(payload())
    now[0] = 101
    private = payload()
    private.update(incognito=True, observedAt=101)
    snapshots.accept(private)
    snapshots.accept(payload())
    assert snapshots.lookup('msedge.exe', 'Article') is None
    now[0] = 110
    snapshots.accept(payload())
    assert snapshots.lookup('msedge.exe', 'Article') is None


def test_authenticated_bridge_rejects_websites_and_accepts_extension(tmp_path):
    import time
    bridge = BrowserBridge(tmp_path, port=0)
    try:
        address = 'http://127.0.0.1:' + str(bridge.server.server_port) + '/snapshot'
        data = payload()
        data['observedAt'] = time.time()
        def send(token, origin):
            return urlopen(Request(address, data=json.dumps(data).encode(), headers={
                'Authorization': 'Bearer ' + token, 'Origin': origin, 'Content-Type': 'application/json',
            }), timeout=2)
        with pytest.raises(HTTPError) as error:
            send('wrong', 'chrome-extension://' + 'a' * 32)
        assert error.value.code == 401
        with pytest.raises(HTTPError) as error:
            send(bridge.token, 'https://example.com')
        assert error.value.code == 403
        with send(bridge.token, 'chrome-extension://' + 'a' * 32) as response:
            assert response.status == 204
        assert bridge.snapshots.lookup('msedge.exe', 'Article') == 'https://example.com/article'
    finally:
        bridge.close()
