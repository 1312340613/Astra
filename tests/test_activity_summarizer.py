"""M4 summarizer tests: pure functions + store round-trip with a fake chat."""
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.runtime.activity_recorder import summarizer
from agent.runtime.activity_store import ActivityStore

BUCKET = datetime(2026, 9, 5, 2, 0, 0, tzinfo=UTC)


def test_summary_config_is_independent_of_chat(monkeypatch):
    for key in ('ASTRA_ACTIVITY_SUMMARY_MODEL', 'ASTRA_ACTIVITY_SUMMARY_BASE_URL', 'ASTRA_ACTIVITY_SUMMARY_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('LLM_MODEL', 'different-chat-model')
    monkeypatch.setenv('LLM_BASE_URL', 'https://chat.invalid/v1')
    monkeypatch.setenv('LLM_API_KEY', 'chat-secret')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'summary-secret')
    config = summarizer.summary_config()
    assert config.model == 'deepseek-flash'
    assert config.base_url == 'https://api.deepseek.com'
    assert config.api_key == 'summary-secret'
    assert config.thinking_mode == 'disabled'
    monkeypatch.setenv('ASTRA_ACTIVITY_SUMMARY_MODEL', 'custom-summary')
    monkeypatch.setenv('ASTRA_ACTIVITY_SUMMARY_BASE_URL', 'http://localhost:8080/v1')
    monkeypatch.setenv('ASTRA_ACTIVITY_SUMMARY_API_KEY', 'custom-secret')
    config = summarizer.summary_config()
    assert (config.model, config.base_url, config.api_key) == ('custom-summary', 'http://localhost:8080/v1', 'custom-secret')


def _store(tmp_path: Path) -> ActivityStore:
    return ActivityStore(tmp_path / "activity.sqlite3")


def _seed_events(store: ActivityStore, count: int = 5) -> None:
    for i in range(count):
        occurred = (BUCKET + timedelta(seconds=i * 30)).isoformat().replace("+00:00", "Z")
        store.connection.execute(
            "INSERT INTO activity_events (segment_id, event_id, occurred_at, kind, app_name, "
            "bundle_id, window_title, url, url_search_text, selection_text, searchable_text, raw_json, imported_at) "
            "VALUES (?, ?, ?, 'window.changed', '终端', 'com.apple.Terminal', 'astra-work', '', '', '', "
            "'终端 astra-work', '{}', ?)",
            (f"seed-{i}", i + 1, occurred, occurred),
        )
    # 关闭测试手写的开放事务：真实 sync 从不留悬挂写，upsert_summary 的
    # 内部事务才会撞上 "transaction within a transaction"。
    store.connection.commit()


GOLDEN_REPLY = json.dumps({
    "title": "Astra Recorder Overnight Build",
    "description": "Worked on the native activity recorder in Terminal.",
    "applications": ["com.apple.Terminal", "com.microsoft.edgemac"],
    "summary": "The user compiled the recorder daemon and ran smoke buckets in Terminal. "
               "Edge was opened briefly for API doc lookup. Work continued on the Astra repo.",
    "context": ["/home/example/astra active"],
})


def test_window_bucket_aligns_to_10min():
    assert summarizer.window_bucket(BUCKET + timedelta(seconds=599)) == BUCKET
    assert summarizer.window_bucket(BUCKET + timedelta(seconds=601)) == BUCKET + timedelta(minutes=10)


def test_digest_lists_events_with_caps():
    rows = [(f"2026-09-05T02:0{i}:00Z", "mouse.click", "Edge", f"tab {i}", "") for i in range(5)]
    digest = summarizer.build_digest(rows, tzinfo=UTC)
    assert "02:00 mouse.click: Edge tab 0" in digest
    assert digest.count("\n") == 4
    long_rows = [(f"2026-09-05T02:{i:02d}:00Z", "x", "a", "b", "s" * 500) for i in range(90)]
    capped = summarizer.build_digest(long_rows)
    assert "+10 more events" in capped


def test_digest_clock_is_local_not_utc():
    # Stored stamps are UTC; the digest must show the recorder's wall clock so
    # the model cannot read 10:20 SGT as "02:20" and call it late night.
    plus8 = timezone(timedelta(hours=8))
    row = ("2026-09-05T02:20:00Z", "window.changed", "Code", "notebook.ipynb", "")
    assert "10:20 window.changed: Code notebook.ipynb" in summarizer.build_digest([row], tzinfo=plus8)
    naive = ("2026-09-05T02:20:00", "window.changed", "Code", "notebook.ipynb", "")
    assert "10:20" in summarizer.build_digest([naive], tzinfo=plus8)
    assert summarizer.build_digest([row], tzinfo=UTC).startswith("- 02:20 ")


def test_system_prompt_declares_digest_timezone(tmp_path):
    captured: dict[str, str] = {}

    def chat(messages):
        captured["system"] = messages[0]["content"]
        return GOLDEN_REPLY

    store = _store(tmp_path)
    _seed_events(store, count=5)
    summarizer.summarize_window(
        store,
        start=BUCKET,
        end=BUCKET + timedelta(minutes=10),
        chat=chat,
        now=BUCKET + timedelta(minutes=11),
        tzinfo=timezone(timedelta(hours=8)),
    )
    assert "local time" in captured["system"]
    assert "UTC+08:00" in captured["system"]


def test_parse_accepts_fenced_and_rejects_garbage():
    assert summarizer.parse_llm_json(GOLDEN_REPLY)["title"] == "Astra Recorder Overnight Build"
    assert summarizer.parse_llm_json(f"```json\n{GOLDEN_REPLY}\n```") is not None
    assert summarizer.parse_llm_json("I think the user was working.") is None
    assert summarizer.parse_llm_json('{"title": "x"}') is None  # missing required fields
    assert summarizer.parse_llm_json("[1,2,3]") is None


def test_render_content_is_skysight_shaped():
    parsed = summarizer.parse_llm_json(GOLDEN_REPLY)
    content = summarizer.render_content(parsed)
    assert content.startswith("---\ntitle: Astra Recorder Overnight Build\n")
    assert "applications: [com.apple.Terminal, com.microsoft.edgemac]" in content
    assert "## Memory summary" in content
    assert "generator: astra-" in content


def test_summarize_window_writes_store_and_is_idempotent(tmp_path):
    store = _store(tmp_path)
    _seed_events(store)
    calls = []

    def fake_chat(messages):
        calls.append(messages)
        assert "system" == messages[0]["role"]
        assert "window.changed" in messages[1]["content"]
        return GOLDEN_REPLY

    sid = summarizer.summarize_window(
        store, start=BUCKET, end=BUCKET + timedelta(minutes=10), chat=fake_chat,
        now=BUCKET + timedelta(minutes=11),
    )
    assert sid is not None and len(calls) == 1
    row = store.connection.execute(
        "SELECT granularity, source_path, content FROM activity_summaries"
    ).fetchone()
    assert tuple(row)[0] == "10min"
    assert row["source_path"].startswith("astra://activity/2026-09-05T02-00-00Z")
    assert "Astra Recorder Overnight Build" in row["content"]

    # re-run same window: skipped, no second LLM call
    assert summarizer.summarize_window(
        store, start=BUCKET, end=BUCKET + timedelta(minutes=10), chat=fake_chat
    ) is None
    assert len(calls) == 1


def test_summarize_window_skips_sparse_and_bad_llm_output(tmp_path):
    store = _store(tmp_path)
    store.connection.execute(
        "INSERT INTO activity_events (segment_id, event_id, occurred_at, kind, app_name, "
        "bundle_id, window_title, url, url_search_text, selection_text, searchable_text, raw_json, imported_at) "
        "VALUES ('s', 1, '2026-09-05T02:00:00Z', 'x', 'a', 'b', '', '', '', '', 'a', '{}', 'now')"
    )
    store.connection.commit()
    assert summarizer.summarize_window(
        store, start=BUCKET, end=BUCKET + timedelta(minutes=10),
        chat=lambda m: GOLDEN_REPLY, now=BUCKET + timedelta(minutes=11),
    ) is None  # <3 events

    _seed_events(store, count=1)  # now exactly 3 rows total
    assert summarizer.summarize_window(
        store, start=BUCKET, end=BUCKET + timedelta(minutes=10),
        chat=lambda m: "not json at all", now=BUCKET + timedelta(minutes=11),
    ) is None  # fail-closed
    assert store.connection.execute("SELECT count(*) c FROM activity_summaries").fetchone()["c"] == 0


def test_digest_spans_entire_window_with_bounded_output():
    rows = [(f"2026-09-05T02:{i // 60:02d}:{i % 60:02d}Z", "window.changed", "Editor", f"task-{i}", "") for i in range(500)]
    digest = summarizer.build_digest(rows)
    assert "task-0" in digest and "task-499" in digest
    assert len(digest.splitlines()) <= 81


def test_summary_reads_late_events_beyond_sql_prefix(tmp_path):
    store = _store(tmp_path)
    _seed_events(store, count=300)
    # Pack all events into the same window, with the final task at minute 9.
    store.connection.execute("UPDATE activity_events SET occurred_at='2026-09-05T02:00:00Z'")
    store.connection.execute("UPDATE activity_events SET occurred_at='2026-09-05T02:09:00Z', window_title='late-research' WHERE event_id=300")
    store.connection.commit()
    calls = []
    summarizer.summarize_window(store, start=BUCKET, end=BUCKET + timedelta(minutes=10), now=BUCKET + timedelta(minutes=11),
                               chat=lambda messages: calls.append(messages) or GOLDEN_REPLY)
    assert "late-research" in calls[0][1]["content"]
    assert len(calls[0][1]["content"].splitlines()) <= 81


def test_changed_input_regenerates_and_failed_refresh_preserves_summary(tmp_path):
    store = _store(tmp_path)
    _seed_events(store)
    calls = []
    def generate(reply=GOLDEN_REPLY):
        return summarizer.summarize_window(store, start=BUCKET, end=BUCKET + timedelta(minutes=10), now=BUCKET + timedelta(minutes=11),
                    chat=lambda messages: calls.append(messages) or reply)
    sid = generate()
    original = store.connection.execute("SELECT content FROM activity_summaries").fetchone()[0]
    store.connection.execute("UPDATE activity_events SET window_title='new-task' WHERE event_id=5")
    store.connection.commit()
    assert generate("invalid JSON") is None
    assert len(calls) == 2
    assert store.connection.execute("SELECT content FROM activity_summaries").fetchone()[0] == original
    changed = json.loads(GOLDEN_REPLY)
    changed["summary"] = "Updated task after late import."
    assert generate(json.dumps(changed)) == sid
    assert generate() is None
    assert len(calls) == 3


def test_provider_failure_keeps_old_summary_and_remains_retryable(tmp_path):
    store = _store(tmp_path)
    _seed_events(store)
    kwargs = dict(store=store, start=BUCKET, end=BUCKET + timedelta(minutes=10), now=BUCKET + timedelta(minutes=11))
    sid = summarizer.summarize_window(**kwargs, chat=lambda _: GOLDEN_REPLY)
    store.connection.execute("UPDATE activity_events SET selection_text='new evidence'")
    store.connection.commit()
    def broken(_):
        raise RuntimeError("provider unavailable")
    assert summarizer.summarize_window(**kwargs, chat=broken) is None
    assert summarizer.summarize_window(**kwargs, chat=lambda _: GOLDEN_REPLY) == sid


def test_cli_indexes_and_retries_after_embedding_failure_even_without_new_summary(tmp_path, monkeypatch, capsys):
    import agent.cli.environment
    import agent.runtime.llm
    from agent.runtime.context_index import vector_indexer, vector_index
    monkeypatch.setattr(agent.cli.environment, "load_project_env", lambda _: None)
    class Provider:
        def __init__(self, config):
            pass
        async def chat(self, *args, **kwargs):
            raise AssertionError("No model call for zero lookback")
    monkeypatch.setattr(agent.runtime.llm, "OpenAICompatibleProvider", Provider)
    vector_path = tmp_path / "vectors.sqlite3"
    monkeypatch.setattr(vector_index, "default_vectors_db_path", lambda: vector_path)
    calls = []
    def rebuild(activity_db, vectors_db, *, force):
        calls.append((activity_db, vectors_db, force))
        if len(calls) == 1:
            raise SystemExit("embedding unavailable")
        return 1
    monkeypatch.setattr(vector_indexer, "rebuild", rebuild)
    path = tmp_path / "activity.sqlite3"
    assert summarizer.main(["--store", str(path), "--lookback", "0"]) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["consecutive_failures"] == 1 and failed["last_success_at"] is None
    assert failed["vector_index"] == "deferred" and failed["result"] == "partial_failure"
    assert summarizer.main(["--store", str(path), "--lookback", "0"]) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["consecutive_failures"] == 0 and recovered["last_success_at"] is not None
    assert recovered["vector_index"] == "ready" and recovered["result"] == "ok"
    assert calls == [(path, vector_path, False)] * 2


def test_late_import_refreshes_existing_window_with_same_source(tmp_path):
    store = _store(tmp_path)
    _seed_events(store)
    kwargs = dict(store=store, start=BUCKET, end=BUCKET + timedelta(minutes=10), now=BUCKET + timedelta(minutes=11))
    sid = summarizer.summarize_window(**kwargs, chat=lambda _: GOLDEN_REPLY)
    store.connection.execute(
        "INSERT INTO activity_events (segment_id, event_id, occurred_at, kind, app_name, bundle_id, "
        "window_title, url, url_search_text, selection_text, searchable_text, raw_json, imported_at) "
        "VALUES ('late-import', 1, '2026-09-05T02:09:30Z', 'window.changed', 'Browser', 'browser', "
        "'Late task transition', '', '', '', 'Late task transition', '{}', '2026-09-05T02:12:00Z')"
    )
    store.connection.commit()
    calls = []
    assert summarizer.summarize_window(**kwargs, chat=lambda messages: calls.append(messages) or GOLDEN_REPLY) == sid
    assert "Late task transition" in calls[0][1]["content"]
    assert store.connection.execute("SELECT count(*) FROM activity_summaries").fetchone()[0] == 1


def test_cli_indexes_committed_summary_after_generation(tmp_path, monkeypatch):
    import agent.cli.environment
    import agent.runtime.llm
    from agent.runtime.context_index import vector_indexer, vector_index
    store = _store(tmp_path)
    _seed_events(store)
    store.close()
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return BUCKET + timedelta(minutes=11)
    monkeypatch.setattr(summarizer, "datetime", Clock)
    monkeypatch.setattr(agent.cli.environment, "load_project_env", lambda _: None)
    class Provider:
        def __init__(self, config):
            pass
        async def chat(self, *args, **kwargs):
            return {"content": GOLDEN_REPLY}
    monkeypatch.setattr(agent.runtime.llm, "OpenAICompatibleProvider", Provider)
    monkeypatch.setattr(vector_index, "default_vectors_db_path", lambda: tmp_path / "vectors.sqlite3")
    indexed = []
    def rebuild(activity_db, vectors_db, *, force):
        fresh = ActivityStore(activity_db)
        try:
            indexed.extend(fresh.connection.execute("SELECT summary_id FROM activity_summaries").fetchall())
        finally:
            fresh.close()
        return len(indexed)
    monkeypatch.setattr(vector_indexer, "rebuild", rebuild)
    assert summarizer.main(["--store", str(tmp_path / "activity.sqlite3"), "--lookback", "10"]) == 0
    assert len(indexed) == 1


def test_cli_reports_distinct_failure_diagnostics(tmp_path, monkeypatch, capsys):
    import agent.cli.environment
    import agent.runtime.llm
    from agent.runtime.context_index import vector_indexer, vector_index

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return BUCKET + timedelta(minutes=41)

    store = _store(tmp_path)
    _seed_events(store)
    # Four windows: sparse, provider failure, invalid JSON, and success.
    rows = store.connection.execute("SELECT * FROM activity_events").fetchall()
    store.connection.execute("DELETE FROM activity_events")
    for window, count in enumerate((2, 5, 5, 5)):
        for row in rows[:count]:
            values = list(row)
            values[row.keys().index("segment_id")] = f"window-{window}-{row['segment_id']}"
            values[row.keys().index("occurred_at")] = (
                BUCKET + timedelta(minutes=10 * window, seconds=30 * row["event_id"])
            ).isoformat().replace("+00:00", "Z")
            store.connection.execute(
                f"INSERT INTO activity_events VALUES ({','.join('?' for _ in values)})", values
            )
    store.connection.commit()
    store.close()
    calls = []

    class Provider:
        def __init__(self, config):
            pass

        async def chat(self, *args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise RuntimeError("private provider details must not be reported")
            if len(calls) == 2:
                return {"content": "not JSON"}
            return {"content": GOLDEN_REPLY}

    monkeypatch.setattr(summarizer, "datetime", Clock)
    monkeypatch.setattr(agent.cli.environment, "load_project_env", lambda _: None)
    monkeypatch.setattr(agent.runtime.llm, "OpenAICompatibleProvider", Provider)
    monkeypatch.setattr(vector_index, "default_vectors_db_path", lambda: tmp_path / "vectors.sqlite3")
    monkeypatch.setattr(vector_indexer, "rebuild", lambda *args, **kwargs: 0)
    assert summarizer.main(["--store", str(tmp_path / "activity.sqlite3"), "--lookback", "40"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert {key: report.get(key) for key in ("sparse", "llm_error", "parse_error")} == {
        "sparse": 1, "llm_error": 1, "parse_error": 1,
    }
    assert report["written"] == 1 and report["empty_or_failed"] == 3
    assert len(calls) == 3
    assert report["pending_windows"] == 2
    assert report["oldest_pending_at"] == "2026-09-05T02:10:00Z"
    assert report["consecutive_failures"] == 1
    assert "private provider details" not in json.dumps(report)
    with ActivityStore(tmp_path / "activity.sqlite3") as reopened:
        assert reopened.connection.execute("SELECT count(*) FROM activity_summaries").fetchone()[0] == 1


def test_failed_refresh_records_diagnostics_and_remains_retryable(tmp_path):
    from collections import Counter

    store = _store(tmp_path)
    _seed_events(store)
    kwargs = dict(store=store, start=BUCKET, end=BUCKET + timedelta(minutes=10), now=BUCKET + timedelta(minutes=11))
    sid = summarizer.summarize_window(**kwargs, chat=lambda _: GOLDEN_REPLY)
    original = store.connection.execute("SELECT content FROM activity_summaries").fetchone()[0]
    store.connection.execute("UPDATE activity_events SET selection_text='new evidence'")
    store.connection.commit()
    diagnostics = Counter()

    def broken(_):
        raise RuntimeError("provider unavailable")

    assert summarizer.summarize_window(**kwargs, chat=broken, diagnostics=diagnostics) is None
    assert summarizer.summarize_window(**kwargs, chat=lambda _: None, diagnostics=diagnostics) is None
    assert diagnostics == {"llm_error": 1, "parse_error": 1}
    assert store.connection.execute("SELECT content FROM activity_summaries").fetchone()[0] == original
    assert summarizer.summarize_window(**kwargs, chat=lambda _: GOLDEN_REPLY, diagnostics=diagnostics) == sid
    assert summarizer.summarize_window(**kwargs, chat=lambda _: GOLDEN_REPLY, diagnostics=diagnostics) is None
    assert diagnostics == {"llm_error": 1, "parse_error": 1}
    store.close()


def test_parser_exception_is_fail_closed_and_counted_as_parse_error(tmp_path):
    from collections import Counter

    with _store(tmp_path) as store:
        _seed_events(store)
        diagnostics = Counter()
        # json.loads raises RecursionError, not JSONDecodeError, for deep nesting.
        result = summarizer.summarize_window(
            store, start=BUCKET, end=BUCKET + timedelta(minutes=10),
            now=BUCKET + timedelta(minutes=11),
            chat=lambda _: '[' * 2000 + ']' * 2000, diagnostics=diagnostics,
        )
        assert result is None
        assert diagnostics == {"parse_error": 1}
        assert store.connection.execute("SELECT count(*) FROM activity_summaries").fetchone()[0] == 0


def _seed_window(store, bucket, *, count=5, name="batch"):
    for i in range(count):
        stamp = (bucket + timedelta(seconds=i * 30)).isoformat().replace("+00:00", "Z")
        store.connection.execute(
            "INSERT INTO activity_events (segment_id, event_id, occurred_at, kind, app_name, bundle_id, "
            "window_title, url, url_search_text, selection_text, searchable_text, raw_json, imported_at) "
            "VALUES (?, ?, ?, 'window.changed', 'Editor', 'editor', ?, '', '', '', '', '{}', ?)",
            (f"{name}-{bucket.isoformat()}", i, stamp, name, stamp),
        )
    store.connection.commit()


def test_batch_catches_up_old_windows_with_a_hard_call_bound_and_no_regeneration(tmp_path):
    now = BUCKET + timedelta(days=1)
    with _store(tmp_path) as store:
        for i in range(7):
            _seed_window(store, BUCKET + timedelta(minutes=i * 10))
        first = summarizer.summarize_batch(store, chat=lambda _: GOLDEN_REPLY, now=now, max_windows=3)
        assert first["written"] == first["llm_calls"] == first["windows_processed"] == 3
        assert first["pending_windows"] == first["catchup_pending_windows"] == 4
        assert first["oldest_pending_at"] == "2026-09-05T02:30:00Z"
    # Progress and dirty tracking must survive scheduler process restarts.
    with _store(tmp_path) as store:
        second = summarizer.summarize_batch(store, chat=lambda _: GOLDEN_REPLY, now=now, max_windows=3)
        third = summarizer.summarize_batch(store, chat=lambda _: GOLDEN_REPLY, now=now, max_windows=3)
        assert second["written"] == 3 and third["written"] == 1
        assert third["pending_windows"] == 0
        idle = summarizer.summarize_batch(store, chat=lambda _: GOLDEN_REPLY, now=now, max_windows=3)
        assert idle["llm_calls"] == idle["windows_processed"] == 0


def test_batch_bootstrap_audits_existing_revisions_without_a_model_call(tmp_path):
    now = BUCKET + timedelta(days=1)
    with _store(tmp_path) as store:
        _seed_events(store)
        summarizer.summarize_window(store, start=BUCKET, end=BUCKET + timedelta(minutes=10),
                                    now=now, chat=lambda _: GOLDEN_REPLY)
        report = summarizer.summarize_batch(store, now=now, chat=lambda _: GOLDEN_REPLY)
        assert report["already_summarized"] == 1
        assert report["llm_calls"] == report["pending_windows"] == 0


def test_batch_refreshes_old_updates_and_late_imports_and_retries_failed_refresh(tmp_path):
    now = BUCKET + timedelta(days=1)
    with _store(tmp_path) as store:
        _seed_events(store)
        summarizer.summarize_batch(store, now=now, chat=lambda _: GOLDEN_REPLY)
    with _store(tmp_path) as store:
        store.connection.execute("UPDATE activity_events SET window_title='updated old task'")
        store.connection.commit()
        failed = summarizer.summarize_batch(store, now=now, chat=lambda _: "bad json")
        assert failed["parse_error"] == failed["pending_windows"] == 1
        assert store.connection.execute("SELECT count(*) FROM activity_summaries").fetchone()[0] == 1
        calls = []
        refreshed = summarizer.summarize_batch(
            store, now=now, chat=lambda messages: calls.append(messages) or GOLDEN_REPLY,
        )
        assert refreshed["written"] == 1 and refreshed["pending_windows"] == 0
        assert "updated old task" in calls[0][1]["content"]
        _seed_window(store, BUCKET + timedelta(minutes=5), count=1, name="late old import")
        imported = summarizer.summarize_batch(
            store, now=now, chat=lambda messages: calls.append(messages) or GOLDEN_REPLY,
        )
        assert imported["written"] == 1 and imported["pending_windows"] == 0
        assert "late old import" in calls[1][1]["content"]


def test_batch_change_during_generation_is_not_acknowledged(tmp_path):
    now = BUCKET + timedelta(days=1)
    with _store(tmp_path) as store:
        _seed_events(store)

        def chat(_):
            # Another recorder connection changes a row after the digest snapshot.
            with _store(tmp_path) as writer:
                writer.connection.execute("UPDATE activity_events SET selection_text='arrived during LLM'")
                writer.connection.commit()
            return GOLDEN_REPLY

        first = summarizer.summarize_batch(store, now=now, chat=chat)
        assert first["written"] == 0
        assert first["superseded"] == first["pending_windows"] == 1
        assert store.connection.execute("SELECT count(*) FROM activity_summaries").fetchone()[0] == 0
        second = summarizer.summarize_batch(store, now=now, chat=lambda _: GOLDEN_REPLY)
        assert second["written"] == 1 and second["pending_windows"] == 0


@pytest.mark.parametrize("change_before,change_after", [(False, False), (True, False), (False, True)])
def test_overlapping_batches_cannot_overwrite_a_newer_completed_summary(tmp_path, change_before, change_after):
    now = BUCKET + timedelta(days=1)
    stale = json.loads(GOLDEN_REPLY)
    stale["summary"] = "Stale response from the slower request."
    fresh = dict(stale, summary="Fresh response from the completed request.")
    with _store(tmp_path) as slow:
        _seed_events(slow)

        def slower_chat(_):
            # This second writer must finish while the first model call is in
            # progress. No database write lock may be held over that call.
            with _store(tmp_path) as fast:
                if change_before:
                    fast.connection.execute("UPDATE activity_events SET selection_text='new revision'")
                    fast.connection.commit()
                completed = summarizer.summarize_batch(
                    fast, now=now, chat=lambda _: json.dumps(fresh), max_windows=1,
                )
                assert completed["written"] == 1 and completed["pending_windows"] == 0
                if change_after:
                    # Queue deletion and recreation must not reset the revision
                    # to the value still held by the original in-flight worker.
                    _seed_window(fast, BUCKET, count=1, name="after completed request")
            return json.dumps(stale)

        report = summarizer.summarize_batch(slow, now=now, chat=slower_chat, max_windows=1)
        saved = slow.connection.execute("SELECT content FROM activity_summaries").fetchone()[0]
        assert fresh["summary"] in saved and stale["summary"] not in saved
        assert report["written"] == 0 and report["superseded"] == 1
        assert report["pending_windows"] == int(change_after)
        following = summarizer.summarize_batch(slow, now=now, chat=lambda _: json.dumps(fresh), max_windows=1)
        assert following["llm_calls"] == int(change_after)
        assert following["pending_windows"] == 0


def test_batch_summary_and_input_revision_roll_back_together(tmp_path):
    with _store(tmp_path) as store:
        _seed_events(store)

        def chat(_):
            store.connection.execute(
                "CREATE TRIGGER reject_summary_input BEFORE INSERT ON astra_activity_summary_inputs "
                "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END"
            )
            return GOLDEN_REPLY

        report = summarizer.summarize_batch(store, now=BUCKET + timedelta(days=1), chat=chat, max_windows=1)
        assert report["storage_error"] == 1 and report["pending_windows"] == 1
        assert store.connection.execute("SELECT count(*) FROM activity_summaries").fetchone()[0] == 0
        assert store.connection.execute("SELECT count(*) FROM astra_activity_summary_inputs").fetchone()[0] == 0


def test_cli_embedding_disabled_is_healthy_and_does_not_build_vectors(tmp_path, monkeypatch, capsys):
    import agent.cli.environment
    import agent.runtime.llm
    from agent.runtime.context_index import vector_index, vector_indexer

    monkeypatch.setattr(agent.cli.environment, "load_project_env", lambda _: None)
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_EMBEDDING", "off")
    monkeypatch.setattr(agent.runtime.llm, "OpenAICompatibleProvider", lambda _: None)
    vectors = tmp_path / "disabled-vectors" / "vectors.db"
    monkeypatch.setattr(vector_index, "default_vectors_db_path", lambda: vectors)
    monkeypatch.setattr(vector_indexer, "rebuild", lambda *_args, **_kwargs: pytest.fail("embedding is disabled"))

    assert summarizer.main(["--store", str(tmp_path / "activity.db"), "--max-windows", "0"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["vector_index"] == "disabled" and report["vectors_indexed"] == 0
    assert report["result"] == "ok" and report["consecutive_failures"] == 0
    assert report["last_success_at"] is not None
    assert not vectors.parent.exists()


def test_batch_failures_do_not_starve_other_work_or_get_lost(tmp_path):
    now = BUCKET + timedelta(days=1)
    with _store(tmp_path) as store:
        for i in range(4):
            _seed_window(store, BUCKET + timedelta(minutes=i * 10), name=f"task-{i}")
        attempts = []

        def chat(messages):
            text = messages[1]["content"]
            attempts.append(next(i for i in range(4) if f"task-{i}" in text))
            return "invalid" if "task-0" in text else GOLDEN_REPLY

        for _ in range(5):
            report = summarizer.summarize_batch(store, now=now, chat=chat, max_windows=1)
            assert report["llm_calls"] == 1
        assert attempts == [0, 1, 2, 3, 0]
        assert report["pending_windows"] == 1
        recovered = summarizer.summarize_batch(store, now=now, chat=lambda _: GOLDEN_REPLY, max_windows=1)
        assert recovered["written"] == 1 and recovered["pending_windows"] == 0


def test_batch_serves_recent_and_old_work_even_with_one_window_budget(tmp_path):
    now = BUCKET + timedelta(days=1)
    with _store(tmp_path) as store:
        _seed_window(store, BUCKET, name="old")
        for i in range(4):
            _seed_window(store, now - timedelta(minutes=(i + 1) * 10), name="recent")
        attempts = []
        for _ in range(4):
            summarizer.summarize_batch(
                store, now=now, max_windows=1,
                chat=lambda messages: attempts.append("old" if "old" in messages[1]["content"] else "recent") or GOLDEN_REPLY,
            )
        assert attempts == ["recent", "recent", "recent", "old"]


def test_batch_sparse_and_open_windows_wait_for_eligible_events(tmp_path):
    now = BUCKET + timedelta(minutes=11)
    with _store(tmp_path) as store:
        _seed_window(store, BUCKET, count=2)
        _seed_window(store, BUCKET + timedelta(minutes=10), count=3)
        report = summarizer.summarize_batch(store, now=now, chat=lambda _: GOLDEN_REPLY)
        assert report["sparse"] == 1 and report["llm_calls"] == report["pending_windows"] == 0
        _seed_window(store, BUCKET, count=1, name="late")
        report = summarizer.summarize_batch(store, now=now, chat=lambda _: GOLDEN_REPLY)
        assert report["written"] == 1
        report = summarizer.summarize_batch(store, now=now + timedelta(minutes=10), chat=lambda _: GOLDEN_REPLY)
        assert report["written"] == 1


def test_batch_does_not_recreate_retained_away_windows_or_leave_them_pending(tmp_path):
    now = BUCKET + timedelta(days=1)
    with _store(tmp_path) as store:
        _seed_events(store)

        def chat(_):
            with _store(tmp_path) as writer:
                writer.clear((BUCKET + timedelta(minutes=10)).isoformat())
            return GOLDEN_REPLY

        report = summarizer.summarize_batch(store, now=now, chat=chat)
        assert report["retention_skipped"] == 1 and report["pending_windows"] == 0
        assert store.connection.execute("SELECT count(*) FROM activity_summaries").fetchone()[0] == 0
        assert summarizer.summarize_batch(store, now=now, chat=lambda _: GOLDEN_REPLY)["llm_calls"] == 0


def test_batch_recovers_a_deleted_native_summary_while_events_remain(tmp_path):
    now = BUCKET + timedelta(days=1)
    with _store(tmp_path) as store:
        _seed_events(store)
        summarizer.summarize_batch(store, now=now, chat=lambda _: GOLDEN_REPLY)
        store.connection.execute("DELETE FROM activity_summaries")
        store.connection.commit()
        report = summarizer.summarize_batch(store, now=now, chat=lambda _: GOLDEN_REPLY)
        assert report["written"] == 1 and report["pending_windows"] == 0


def test_batch_zero_budget_preserves_pending_windows_without_calls(tmp_path):
    with _store(tmp_path) as store:
        _seed_events(store)
        report = summarizer.summarize_batch(
            store, now=BUCKET + timedelta(days=1), chat=lambda _: GOLDEN_REPLY, max_windows=0,
        )
        assert report["windows_processed"] == report["llm_calls"] == 0
        assert report["pending_windows"] == 1


def test_batch_preserves_microsecond_bucket_boundaries_in_bootstrap_and_triggers(tmp_path):
    now = BUCKET + timedelta(minutes=11)
    with _store(tmp_path) as store:
        _seed_events(store, count=3)
        store.connection.execute("UPDATE activity_events SET occurred_at='2026-09-05T02:09:59.999999Z'")
        store.connection.commit()
        first = summarizer.summarize_batch(store, now=now, chat=lambda _: GOLDEN_REPLY)
        assert first["written"] == 1 and first["pending_windows"] == 0
        store.connection.execute("UPDATE activity_events SET occurred_at='2026-09-05T02:00:00.000001Z'")
        store.connection.commit()
        second = summarizer.summarize_batch(store, now=now, chat=lambda _: GOLDEN_REPLY)
        assert second["written"] == 1 and second["pending_windows"] == 0


def test_batch_uses_normalized_epoch_for_offset_timestamps(tmp_path):
    with _store(tmp_path) as store:
        _seed_events(store, count=3)
        store.connection.execute("UPDATE activity_events SET occurred_at='2026-09-05T10:00:00.000001+08:00'")
        store.connection.commit()
    # Opening the store backfills the canonical epoch index from legacy rows.
    with _store(tmp_path) as store:
        report = summarizer.summarize_batch(
            store, now=BUCKET + timedelta(minutes=11), chat=lambda _: GOLDEN_REPLY,
        )
        assert report["written"] == 1 and report["pending_windows"] == 0


def test_batch_rejects_negative_limits(tmp_path):
    import pytest

    with _store(tmp_path) as store:
        for limits in ({"max_windows": -1}, {"lookback": -1}):
            with pytest.raises(ValueError, match="non-negative"):
                summarizer.summarize_batch(store, now=BUCKET, chat=lambda _: GOLDEN_REPLY, **limits)
