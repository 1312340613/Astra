from datetime import datetime, timezone, timedelta
import json
import sys

import pytest

from agent.runtime.activity_recorder.windows import Recorder, Snapshot
from agent.runtime.activity_recorder.contract import validate_event
from agent.runtime.activity_sync import normalize_event
from agent.runtime.activity_store import ActivityStore, SourceCursor
from agent.runtime.activity_recorder.summarizer import summarize_window


def test_sampling_coalesces_and_counts_only_observed_intervals():
    rows = []
    recorder = Recorder(rows.append, heartbeat=60)
    app = Snapshot('editor.exe', 'Project')
    for second in range(0, 66, 5):
        recorder.observe(app, 1800000000 + second)
    assert len(rows) == 2
    assert rows[1]['duration_seconds'] == 60
    recorder.observe(None, 1800000070)
    assert rows[-1]['duration_seconds'] == 5
    assert sum(x['duration_seconds'] for x in rows) == 65
    assert all(validate_event(x) == [] for x in rows)


def test_pause_exclusion_and_sleep_do_not_leak_or_inflate_time():
    rows = []
    recorder = Recorder(rows.append, title_excludes=['private'])
    app = Snapshot('editor.exe', 'Project')
    recorder.observe(app, 1800000000)
    recorder.observe(app, 1800000005)
    recorder.observe(Snapshot('keepass.exe', 'secret'), 1800000010)
    recorder.observe(Snapshot('browser.exe', 'PRIVATE page'), 1800000015)
    recorder.observe(None, 1800000020)
    recorder.observe(app, 1800001000)
    recorder.observe(app, 1800005000)
    assert sum(x['duration_seconds'] for x in rows) == 5
    assert all(x['window']['title'] == 'Project' for x in rows)
    assert 'secret' not in json.dumps(rows)


def test_window_switch_resets_duration():
    rows = []
    recorder = Recorder(rows.append)
    recorder.observe(Snapshot('one.exe', 'first'), 1800000000)
    recorder.observe(Snapshot('one.exe', 'first'), 1800000005)
    recorder.observe(Snapshot('two.exe', 'second'), 1800000010)
    assert [x['ax']['reason'] for x in rows] == ['started', 'ended', 'started']
    assert rows[1]['duration_seconds'] == 5


def test_windows_events_use_shared_store_and_summary_contract(tmp_path):
    rows = []
    start = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)
    recorder = Recorder(rows.append)
    for second in range(0, 186, 5):
        recorder.observe(Snapshot('code.exe', 'Astra Windows memory'), start.timestamp() + second)
    store = ActivityStore(tmp_path / 'activity.db')
    try:
        events = [normalize_event('windows-test', row) for row in rows]
        assert all(events)
        cursor = SourceCursor('astra://test', 'events', 'windows-test', len(events), 0, 0,
                              start.isoformat(), '')
        assert store.add_event_batch(events, cursor) == len(events)
        assert store.add_event_batch(events, cursor) == 0
        seen = []
        def chat(messages):
            seen.append(messages)
            return json.dumps({'title': 'Windows memory work', 'description': 'Working in Code',
                               'applications': ['windows:code.exe'], 'summary': 'Worked on Astra Windows memory.', 'context': []})
        assert summarize_window(store, start=start, end=start + timedelta(minutes=10), chat=chat,
                                now=start + timedelta(minutes=11))
        assert 'sampled_active_seconds=60' in seen[0][1]['content']
        assert 'macOS or Windows' in seen[0][0]['content']
        assert store.connection.execute('SELECT count(*) FROM activity_summaries').fetchone()[0] == 1
    finally:
        store.close()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows file-lock control path')
def test_clear_uses_owner_lock_and_clears_only_selected_archive(monkeypatch, tmp_path):
    import msvcrt
    from agent.runtime.activity_recorder import windows
    from agent.runtime.context_index import vector_index
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setattr(windows, 'STATE', state)
    monkeypatch.setattr(vector_index, 'default_vectors_db_path', lambda: tmp_path / 'vectors.db')
    store = ActivityStore(tmp_path / 'activity.db')
    rows = []
    Recorder(rows.append).observe(Snapshot('test.exe', 'synthetic history'), 1700000000)
    store.add_event_batch([normalize_event('clear-test', rows[0])],
                          SourceCursor('astra://clear-test', 'events', 'clear-test', 1, 0, 0,
                                       rows[0]['timestamp'], ''))
    store.close()
    vectors = vector_index.VectorStore(tmp_path / 'vectors.db')
    vectors.upsert('test-summary', 'hash', [1., 0.])
    vectors.close()
    lock = (state / 'owner.lock').open('w+b')
    lock.write(b'0')
    lock.flush()
    lock.seek(0)
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    try:
        with pytest.raises(SystemExit):
            windows.main(['--store', str(tmp_path / 'activity.db'), '--clear-history'])
    finally:
        lock.close()
    windows.main(['--store', str(tmp_path / 'activity.db'), '--clear-history'])
    assert (tmp_path / 'activity.db').exists()
    store = ActivityStore(tmp_path / 'activity.db')
    assert store.connection.execute('SELECT count(*) FROM activity_events').fetchone()[0] == 0
    store.close()
    vectors = vector_index.VectorStore(tmp_path / 'vectors.db')
    assert vectors.hashes() == {}
    vectors.close()
