import json
import os
import sqlite3
from pathlib import Path

import pytest

from agent.runtime.activity_store import (
    ActivityEvent,
    ActivityStore,
    ActivitySummary,
    SourceCursor,
    normalize_timestamp,
    sanitize_url,
)


def event(event_id: int = 7) -> ActivityEvent:
    return ActivityEvent(
        segment_id="2026-08-26T06-40-00Z",
        event_id=event_id,
        occurred_at="2026-08-26T06:41:00+00:00",
        kind="selection",
        app_name="Safari",
        bundle_id="com.apple.Safari",
        window_title="Astra design",
        url="https://example.com/page?token=secret#anchor",
        selection_text="activity archive",
        searchable_text="Safari Astra design activity archive",
        raw_json='{"id":7,"kind":"selection"}',
        imported_at="2026-08-26T06:42:00+00:00",
    )


def test_normalize_timestamp_exposes_canonical_search_time_validation():
    assert normalize_timestamp("2026-08-26T06:41:00Z")[1] == "2026-08-26T06:41:00+00:00"
    with pytest.raises(ValueError, match="timezone"):
        normalize_timestamp("2026-08-26T06:41:00")


def test_activity_store_initializes_wal_database(tmp_path: Path):
    path = tmp_path / "private" / "activity.sqlite3"
    with ActivityStore(path) as store:
        assert {"activity_events", "activity_summaries", "sync_files", "activity_meta"} <= store.table_names()
        assert {"activity_events_fts", "activity_summaries_fts"} <= store.table_names()
        assert store.journal_mode() == "wal"


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner-only mode bits are not Windows ACLs")
def test_activity_store_initializes_private_posix_modes(tmp_path: Path):
    path = tmp_path / "private" / "activity.sqlite3"
    with ActivityStore(path):
        pass
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o077 == 0


def test_event_batch_and_cursor_commit_idempotently(tmp_path: Path):
    cursor = SourceCursor(
        source_path="/segments/s/events.jsonl", source_kind="events",
        source_identity="1:2", byte_offset=120, observed_size=120,
        observed_mtime_ns=5, last_success_at="2026-08-26T06:42:00+00:00",
        last_error="",
    )
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        assert store.add_event_batch([event()], cursor) == 1
        assert store.add_event_batch([event()], cursor) == 0
        assert store.stats()["events"] == 1
        assert store.get_cursor(cursor.source_path) == cursor
        raw = store.connection.execute(
            "SELECT raw_json FROM activity_events WHERE segment_id=? AND event_id=?",
            (event().segment_id, event().event_id),
        ).fetchone()[0]
        assert raw == event().raw_json


def test_event_batch_persists_indexable_utc_microseconds_without_rewriting_raw_timestamp(tmp_path: Path):
    cursor = SourceCursor("/events", "events", "1", 1, 1, 1, "2026-08-26T00:00:00Z", "")
    offset_event = ActivityEvent(
        **{**event().__dict__, "occurred_at": "2026-08-26T08:41:00+02:00"}
    )
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        store.add_event_batch([offset_event], cursor)
        row = store.connection.execute(
            "SELECT occurred_at, occurred_at_us FROM activity_events"
        ).fetchone()
        indexes = {item[1] for item in store.connection.execute("PRAGMA index_list(activity_events)")}

    assert row[0] == "2026-08-26T08:41:00+02:00"
    assert isinstance(row[1], int)
    assert "idx_activity_events_occurred_at_us" in indexes


def test_summary_writes_and_migrates_indexable_utc_microseconds(tmp_path: Path):
    path = tmp_path / "legacy-summary.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.execute(
        """CREATE TABLE activity_summaries (
            summary_id TEXT PRIMARY KEY, source_path TEXT NOT NULL, granularity TEXT NOT NULL,
            period_start TEXT NOT NULL DEFAULT '', period_end TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL, content_hash TEXT NOT NULL, source_mtime_ns INTEGER NOT NULL,
            imported_at TEXT NOT NULL, UNIQUE(source_path))"""
    )
    legacy.execute(
        "INSERT INTO activity_summaries VALUES ('legacy', '/legacy', 'day', ?, ?, 'old', 'h', 1, 'now')",
        ("2026-08-30T00:00:00+00:00", "2026-08-31T23:59:00+14:00"),
    )
    legacy.commit()
    legacy.close()

    summary = ActivitySummary(
        "new", "/new", "day", "2026-08-31T00:00:00+00:00",
        "2026-08-31T10:00:00-12:00", "new", "h2", 2, "now",
    )
    with ActivityStore(path) as store:
        assert store.upsert_summary(summary)
        columns = {row[1] for row in store.connection.execute("PRAGMA table_info(activity_summaries)")}
        indexes = {row[1] for row in store.connection.execute("PRAGMA index_list(activity_summaries)")}
        rows = store.connection.execute(
            "SELECT summary_id, period_end_us FROM activity_summaries ORDER BY period_end_us DESC"
        ).fetchall()
        # A second opener proves migration/backfill/index creation is idempotent.
    with ActivityStore(path) as reopened:
        assert reopened.connection.execute("SELECT COUNT(*) FROM activity_summaries").fetchone()[0] == 2

    assert "period_end_us" in columns
    assert "idx_activity_summaries_period_end_us" in indexes
    assert [row[0] for row in rows] == ["new", "legacy"]
    assert all(isinstance(row[1], int) for row in rows)


def test_event_batch_rolls_back_when_cursor_upsert_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cursor = SourceCursor(
        source_path="/segments/s/events.jsonl", source_kind="events",
        source_identity="1:2", byte_offset=120, observed_size=120,
        observed_mtime_ns=5, last_success_at="2026-08-26T06:42:00+00:00",
        last_error="",
    )
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        def fail_cursor_upsert(_cursor: SourceCursor) -> None:
            raise RuntimeError("cursor write failed")

        monkeypatch.setattr(store, "_upsert_cursor", fail_cursor_upsert)
        with pytest.raises(RuntimeError, match="cursor write failed"):
            store.add_event_batch([event()], cursor)
        assert store.stats()["events"] == 0
        assert store.get_cursor(cursor.source_path) is None


def test_canonical_keys_and_source_unique_constraint(tmp_path: Path):
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        event_columns = store.connection.execute("PRAGMA table_info(activity_events)").fetchall()
        assert {row[1]: row[5] for row in event_columns}["segment_id"] == 1
        assert {row[1]: row[5] for row in event_columns}["event_id"] == 2
        summary_columns = store.connection.execute("PRAGMA table_info(activity_summaries)").fetchall()
        assert {row[1]: row[5] for row in summary_columns}["summary_id"] == 1
        summary = ActivitySummary("sum-1", "/source/a", "day", "2026-08-26T00:00:00Z", "2026-08-26T01:00:00Z", "content", "h", 1, "i")
        assert store.upsert_summary(summary) is True
        assert store.upsert_summary(summary) is False
        changed = ActivitySummary("sum-1", "/source/a", "day", "2026-08-26T00:00:00Z", "2026-08-26T01:00:00Z", "changed", "h2", 2, "i")
        assert store.upsert_summary(changed) is True
        with pytest.raises(sqlite3.IntegrityError):
            store.upsert_summary(ActivitySummary("sum-2", "/source/a", "day", "2026-08-26T00:00:00Z", "2026-08-26T01:00:00Z", "other", "h2", 2, "i"))


def test_get_summary_by_source_returns_saved_summary(tmp_path: Path):
    summary = ActivitySummary("sum-1", "/source/a", "10min", "2026-08-26T00:00:00Z", "2026-08-26T00:10:00Z", "content", "hash", 1, "imported")
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        assert store.get_summary_by_source(summary.source_path) is None
        store.upsert_summary(summary)
        assert store.get_summary_by_source(summary.source_path) == summary


def test_schema_defaults_cover_normalized_optional_fields(tmp_path: Path):
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        def defaults(table: str) -> dict[str, str | None]:
            return {row[1]: row[4] for row in store.connection.execute(f"PRAGMA table_info({table})")}
        assert {k: defaults("activity_events")[k] for k in (
            "app_name", "bundle_id", "window_title", "url", "selection_text", "searchable_text"
        )} == {
            "app_name": "''", "bundle_id": "''", "window_title": "''", "url": "''",
            "selection_text": "''", "searchable_text": "''",
        }
        assert {k: defaults("activity_summaries")[k] for k in ("period_start", "period_end")} == {"period_start": "''", "period_end": "''"}
        assert {k: defaults("sync_files")[k] for k in (
            "source_identity", "byte_offset", "observed_size", "observed_mtime_ns", "last_success_at", "last_error"
        )} == {
            "source_identity": "''", "byte_offset": "0", "observed_size": "0",
            "observed_mtime_ns": "0", "last_success_at": "''", "last_error": "''",
        }


def test_event_fts_indexes_and_tracks_updates_and_deletes(tmp_path: Path):
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        cursor = SourceCursor("/source", "events", "id", 1, 1, 1, "now", "")
        store.add_event_batch([event()], cursor)
        for term in ("Safari", "Astra", "activity", "archive", "example"):
            assert store.connection.execute(
                "SELECT rowid FROM activity_events_fts WHERE activity_events_fts MATCH ?", (term,)
            ).fetchone()
        rowid = store.connection.execute("SELECT rowid FROM activity_events").fetchone()[0]
        store.connection.execute("UPDATE activity_events SET app_name='Firefox', searchable_text='Firefox replacement' WHERE rowid=?", (rowid,))
        store.connection.commit()
        assert store.connection.execute("SELECT rowid FROM activity_events_fts WHERE activity_events_fts MATCH 'Firefox'").fetchone()
        assert not store.connection.execute("SELECT rowid FROM activity_events_fts WHERE activity_events_fts MATCH 'Safari'").fetchone()
        store.connection.execute("DELETE FROM activity_events WHERE rowid=?", (rowid,))
        store.connection.commit()
        assert not store.connection.execute("SELECT rowid FROM activity_events_fts WHERE activity_events_fts MATCH 'Firefox'").fetchone()


def test_summary_fts_indexes_and_tracks_updates_and_deletes(tmp_path: Path):
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        summary = ActivitySummary("sum-1", "/source/a", "day", "2026-08-26T00:00:00Z", "2026-08-26T01:00:00Z", "original summary", "h", 1, "i")
        store.upsert_summary(summary)
        assert store.connection.execute(
            "SELECT rowid FROM activity_summaries_fts WHERE activity_summaries_fts MATCH 'original'"
        ).fetchone()
        rowid = store.connection.execute("SELECT rowid FROM activity_summaries").fetchone()[0]
        store.connection.execute("UPDATE activity_summaries SET content='updated summary' WHERE rowid=?", (rowid,))
        store.connection.commit()
        assert store.connection.execute("SELECT rowid FROM activity_summaries_fts WHERE activity_summaries_fts MATCH 'updated'").fetchone()
        assert not store.connection.execute("SELECT rowid FROM activity_summaries_fts WHERE activity_summaries_fts MATCH 'original'").fetchone()
        store.connection.execute("DELETE FROM activity_summaries WHERE rowid=?", (rowid,))
        store.connection.commit()
        assert not store.connection.execute("SELECT rowid FROM activity_summaries_fts WHERE activity_summaries_fts MATCH 'updated'").fetchone()


@pytest.fixture
def activity_store(tmp_path: Path):
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        store.add_event_batch([event()], SourceCursor("/events", "events", "1:2", 1, 1, 1, "now", ""))
        store.upsert_summary(ActivitySummary(
            "summary-1", "/summaries/one.md", "10min",
            "2026-08-26T06:40:00+00:00", "2026-08-26T06:50:00+00:00",
            "Astra archive summary", "hash-1", 1, "now",
        ))
        yield store


def test_search_ranks_summary_and_returns_sanitized_evidence(activity_store: ActivityStore):
    results = activity_store.search(
        "Astra archive", start="2026-08-26T00:00:00Z",
        end="2026-08-27T00:00:00Z", app="Safari", domain="example.com", limit=5,
    )
    assert results[0]["evidence_type"] == "summary"
    raw = next(item for item in results if item["evidence_type"] == "raw_event")
    assert raw["url"] == "https://example.com/page"
    assert raw["source"] == "computer_history"
    assert raw["untrusted_observation"] is True
    serialized = json.dumps(results)
    assert "token=" not in serialized
    assert "raw_json" not in serialized


def test_search_supports_cjk_and_mixed_fts(activity_store: ActivityStore):
    activity_store.upsert_summary(ActivitySummary(
        "summary-cjk", "/summaries/cjk.md", "10min",
        "2026-08-26T06:40:00+00:00", "2026-08-26T06:50:00+00:00",
        "Astra 活动记录已归档", "hash-cjk", 2, "now",
    ))
    assert [row["summary_id"] for row in activity_store.search("活动记录")] == ["summary-cjk"]
    assert [row["summary_id"] for row in activity_store.search("Astra 活动")] == ["summary-cjk"]


def test_search_applies_predicates_before_limit_and_clamps_bounds(tmp_path: Path):
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        first = event(1)
        second = ActivityEvent(**{
            **event(2).__dict__, "app_name": "Firefox", "window_title": "Firefox archive",
            "searchable_text": "Firefox archive", "url": "https://match.example/work",
        })
        store.add_event_batch([first, second], SourceCursor("/events", "events", "1:2", 1, 1, 1, "now", ""))
        assert [row["event_id"] for row in store.search("archive", app="Firefox", domain="match.example", limit=0)] == [2]
        assert len(store.browse(limit=999)) == 1
        with pytest.raises(ValueError):
            store.search("archive", start="not-a-timestamp")
        with pytest.raises(ValueError):
            store.search("archive", start="2026-08-26T07:00:00.1000001Z")
        with pytest.raises(ValueError):
            store.search("archive", start="2026-08-27T00:00:00Z", end="2026-08-26T00:00:00Z")


def test_expand_summary_and_event_return_bounded_sanitized_evidence(activity_store: ActivityStore):
    summary = activity_store.expand(summary_id="summary-1")
    raw = activity_store.expand(segment_id=event().segment_id, event_id=event().event_id)
    assert summary["evidence_type"] == "summary"
    assert raw["evidence_type"] == "raw_event"
    assert raw["url"] == "https://example.com/page"
    assert raw["untrusted_observation"] is True
    assert "raw_json" not in json.dumps((summary, raw))


def test_model_visible_outputs_are_limited_per_item_and_in_total(tmp_path: Path):
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        long = "a" * 20_000
        for index in range(20):
            store.upsert_summary(ActivitySummary(
                f"long-summary-{index}", f"/summaries/long-{index}.md", "day",
                "2026-08-26T00:00:00+00:00", "2026-08-26T23:59:00+00:00",
                f"{index} {long}", f"hash-{index}", index, "now",
            ))
        rows = store.search("aaa", limit=20)
        assert len(rows) == 20
        assert all(len(json.dumps(row, ensure_ascii=False)) <= 500 for row in rows)
        assert len(json.dumps(rows, ensure_ascii=False)) <= 12_000


def test_model_visible_evidence_redacts_bearer_tokens(tmp_path: Path):
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        secret_event = ActivityEvent(**{
            **event().__dict__, "selection_text": "Authorization: Bearer TOPSECRET",
            "searchable_text": "Astra archive Authorization: Bearer TOPSECRET",
        })
        store.add_event_batch([secret_event], SourceCursor("/events", "events", "1:2", 1, 1, 1, "now", ""))
        serialized = json.dumps(store.search("archive"))
        assert "TOPSECRET" not in serialized


def test_model_visible_evidence_redacts_authorization_and_client_secret(tmp_path: Path):
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        secret_event = ActivityEvent(**{
            **event().__dict__,
            "selection_text": "Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ== client_secret=CLIENTSECRET",
            "searchable_text": "Astra archive Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ== client_secret=CLIENTSECRET",
        })
        store.add_event_batch([secret_event], SourceCursor("/events", "events", "1:2", 1, 1, 1, "now", ""))
        serialized = json.dumps(store.search("archive"))
        assert "QWxhZGRpbjpvcGVuIHNlc2FtZQ==" not in serialized
        assert "CLIENTSECRET" not in serialized


def test_model_visible_evidence_redacts_quoted_json_secret_values_everywhere(tmp_path: Path):
    secrets = ("JSON_ACCESS_SUFFIX", "JSON_CLIENT_SUFFIX", "JSON_BEARER_SUFFIX", "JSON_BASIC_SUFFIX")
    quoted = (
        '{"access_token":"token value JSON_ACCESS_SUFFIX","client_secret":"client value JSON_CLIENT_SUFFIX",'
        '"Authorization":"Bearer bearer value JSON_BEARER_SUFFIX and more",'
        '"authorization":"Basic basic value JSON_BASIC_SUFFIX and more"}'
    )
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        secret_event = ActivityEvent(**{
            **event().__dict__, "selection_text": quoted, "searchable_text": f"archive {quoted}",
        })
        summary = ActivitySummary(
            "quoted-summary", "/summaries/quoted.md", "10min",
            "2026-08-26T06:40:00Z", "2026-08-26T06:50:00Z", quoted, "quoted", 1, "now",
        )
        store.add_event_batch([secret_event], SourceCursor("/events", "events", "1:2", 1, 1, 1, "now", ""))
        store.upsert_summary(summary)
        outputs = (store.browse(), store.search("archive"), store.expand(summary_id=summary.summary_id),
                   store.expand(segment_id=secret_event.segment_id, event_id=secret_event.event_id))
        serialized = json.dumps(outputs)
        assert all(secret not in serialized for secret in secrets)


def test_model_visible_evidence_redacts_quoted_secret_assignments_with_spaces(tmp_path: Path):
    secrets = ("EQUALSSECRET", "EQUALSACCESS", "EQUALSKEY")
    quoted = (
        'client_secret="value with spaces EQUALSSECRET" '
        "access_token = 'value with spaces EQUALSACCESS' api_key: \"value with spaces EQUALSKEY\""
    )
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        secret_event = ActivityEvent(**{**event().__dict__, "selection_text": quoted, "searchable_text": f"archive {quoted}"})
        store.add_event_batch([secret_event], SourceCursor("/events", "events", "1:2", 1, 1, 1, "now", ""))
        outputs = (store.browse(), store.search("archive"),
                   store.expand(segment_id=secret_event.segment_id, event_id=secret_event.event_id))
        serialized = json.dumps(outputs)
        assert all(secret not in serialized for secret in secrets)


def test_model_visible_evidence_redacts_escaped_json_keys_and_non_string_values(tmp_path: Path):
    secrets = ("987654321", "123456789", "246813579")
    payload = r'{"access\u005ftoken":987654321,"client\u005fsecret":123456789,"Authorization":246813579}'
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        secret_event = ActivityEvent(**{**event().__dict__, "selection_text": payload, "searchable_text": f"archive {payload}"})
        store.add_event_batch([secret_event], SourceCursor("/events", "events", "1:2", 1, 1, 1, "now", ""))
        outputs = (store.browse(), store.search("archive"),
                   store.expand(segment_id=secret_event.segment_id, event_id=secret_event.event_id))
        serialized = json.dumps(outputs)
        assert all(secret not in serialized for secret in secrets)


@pytest.mark.parametrize(
    ("payload", "secret"),
    [
        ("[" * 1_500 + '"DEEP_JSON_SECRET"' + "]" * 1_500, "DEEP_JSON_SECRET"),
        ('{"access_token":' + "9" * 5_000 + "}", "9" * 5_000),
    ],
)
def test_model_visible_evidence_fails_closed_for_adversarial_json(payload: str, secret: str, tmp_path: Path):
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        secret_event = ActivityEvent(**{**event().__dict__, "selection_text": payload, "searchable_text": f"archive {payload}"})
        store.add_event_batch([secret_event], SourceCursor("/events", "events", "1:2", 1, 1, 1, "now", ""))
        outputs = (store.browse(), store.search("archive"),
                   store.expand(segment_id=secret_event.segment_id, event_id=secret_event.event_id))
        serialized = json.dumps(outputs)
        assert secret not in serialized
        assert "<redacted structured text>" in serialized


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://user:password@example.com:8443/work?token=secret#fragment", "https://example.com:8443/work"),
        ("https://user:password@[2001:db8::1]:8443/work?token=secret#fragment", "https://[2001:db8::1]:8443/work"),
        ("not a url", ""),
        ("https://example.com:bad/work", ""),
    ],
)
def test_sanitize_url_drops_sensitive_components_and_handles_ipv6(value: str, expected: str):
    assert sanitize_url(value) == expected


def test_clear_sets_monotonic_cutoff_and_prevents_event_and_summary_reimport(tmp_path: Path):
    cursor = SourceCursor("/events", "events", "1:2", 1, 1, 1, "now", "")
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        store.add_event_batch([event()], cursor)
        store.upsert_summary(ActivitySummary(
            "old-summary", "/summaries/old.md", "10min",
            "2026-08-26T06:40:00+00:00", "2026-08-26T06:50:00+00:00", "old", "hash", 1, "now",
        ))
        outcome = store.clear(before="2026-08-26T07:00:00+00:00")
        assert outcome["events_deleted"] == 1
        assert outcome["summaries_deleted"] == 1
        assert store.get_meta("do_not_import_before") == "2026-08-26T07:00:00+00:00"
        assert store.add_event_batch([event()], cursor) == 0
        assert store.upsert_summary(ActivitySummary(
            "old-summary", "/summaries/old.md", "10min",
            "2026-08-26T06:40:00+00:00", "2026-08-26T06:50:00+00:00", "old", "hash", 1, "now",
        )) is False
        store.clear(before="2026-08-26T06:00:00Z")
        assert store.get_meta("do_not_import_before") == "2026-08-26T07:00:00+00:00"


def test_timestamp_bounds_and_clear_keep_fractional_second_precision(tmp_path: Path):
    cursor = SourceCursor("/events", "events", "1:2", 1, 1, 1, "now", "")
    early = ActivityEvent(**{**event(1).__dict__, "occurred_at": "2026-08-26T07:00:00.100Z"})
    late = ActivityEvent(**{**event(2).__dict__, "occurred_at": "2026-08-26T07:00:00.900Z"})
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        store.add_event_batch([early, late], cursor)
        assert [row["event_id"] for row in store.search("archive", end="2026-08-26T07:00:00.100Z")] == [1]
        assert store.clear(before="2026-08-26T07:00:00.900Z")["events_deleted"] == 1
        assert [row["event_id"] for row in store.search("archive")] == [2]


def test_model_visible_evidence_sanitizes_ipv6_url_without_secret_leak(tmp_path: Path):
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        ipv6_event = ActivityEvent(**{
            **event().__dict__, "url": "https://user:password@[2001:db8::1]:8443/work?access_token=IPV6SECRET#x",
        })
        store.add_event_batch([ipv6_event], SourceCursor("/events", "events", "1:2", 1, 1, 1, "now", ""))
        raw = store.search("archive")[0]
        assert raw["url"] == "https://[2001:db8::1]:8443/work"
        assert "IPV6SECRET" not in json.dumps(raw)


def test_expand_bounds_each_evidence_item_before_aggregate(tmp_path: Path):
    long = "x" * 20_000
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        summary = ActivitySummary(
            "long-summary", "/summaries/long.md", "10min",
            "2026-08-26T06:40:00Z", "2026-08-26T06:50:00Z", long, "hash", 1, "now",
        )
        events = [ActivityEvent(**{**event(index).__dict__, "selection_text": long, "searchable_text": long}) for index in (1, 2, 3)]
        store.upsert_summary(summary)
        store.add_event_batch(events, SourceCursor("/events", "events", "1:2", 1, 1, 1, "now", ""))
        expanded_summary = store.expand(summary_id=summary.summary_id)
        expanded_event = store.expand(segment_id=events[1].segment_id, event_id=events[1].event_id, window=1)
        target = {key: value for key, value in expanded_event.items() if key != "context"}
        assert len(json.dumps(expanded_summary, ensure_ascii=False)) <= 500
        assert len(json.dumps(target, ensure_ascii=False)) <= 500
        assert all(len(json.dumps(item, ensure_ascii=False)) <= 500 for item in expanded_event["context"])
        assert len(json.dumps(expanded_event, ensure_ascii=False)) <= 12_000


def test_domain_filter_matches_only_the_normalized_exact_host(tmp_path: Path):
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        ipv6 = ActivityEvent(**{
            **event(1).__dict__, "url": "https://[2001:db8::1]:8443/work", "searchable_text": "archive ipv6",
        })
        suffix = ActivityEvent(**{
            **event(2).__dict__, "url": "https://example.com.evil/work", "searchable_text": "archive suffix",
        })
        store.add_event_batch([ipv6, suffix], SourceCursor("/events", "events", "1:2", 1, 1, 1, "now", ""))
        assert [row["event_id"] for row in store.search("archive", domain="[2001:db8::1]:8443")] == [1]
        assert store.search("archive", domain="example.com") == []


@pytest.mark.parametrize("domain", ("not a valid domain?", "example..com"))
def test_invalid_domain_does_not_match_url_less_event(tmp_path: Path, domain: str):
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        no_url = ActivityEvent(**{**event().__dict__, "url": ""})
        malformed_url = ActivityEvent(**{**event(8).__dict__, "url": "https://example..com/work"})
        store.add_event_batch([no_url, malformed_url], SourceCursor("/events", "events", "1:2", 1, 1, 1, "now", ""))
        assert store.search("archive", domain=domain) == []


def test_domain_with_scheme_but_no_netloc_fails_closed(tmp_path: Path):
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        misleading = ActivityEvent(**{**event().__dict__, "url": "https://https/work"})
        store.add_event_batch([misleading], SourceCursor("/events", "events", "1:2", 1, 1, 1, "now", ""))
        assert store.search("archive", domain="https:///example.com") == []


def test_clear_and_writers_fail_closed_for_malformed_timestamps(tmp_path: Path):
    cursor = SourceCursor("/events", "events", "1:2", 1, 1, 1, "now", "")
    invalid_event = ActivityEvent(**{**event().__dict__, "occurred_at": "not-a-timestamp"})
    invalid_summary = ActivitySummary(
        "invalid-summary", "/summaries/invalid.md", "10min", "not-a-timestamp", "not-a-timestamp", "archive", "hash", 1, "now",
    )
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        assert store.add_event_batch([invalid_event], cursor) == 0
        assert store.upsert_summary(invalid_summary) is False
        store.connection.execute(
            """INSERT INTO activity_events(segment_id, event_id, occurred_at, kind, app_name, bundle_id,
               window_title, url, url_search_text, selection_text, searchable_text, raw_json, imported_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("legacy", 1, "not-a-timestamp", "selection", "Safari", "", "legacy", "", "", "archive", "archive", "{}", "now"),
        )
        store.connection.execute(
            """INSERT INTO activity_summaries(summary_id, source_path, granularity, period_start, period_end,
               content, content_hash, source_mtime_ns, imported_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("legacy-summary", "/summaries/legacy.md", "10min", "not-a-timestamp", "not-a-timestamp", "archive", "hash", 1, "now"),
        )
        store.connection.commit()
        outcome = store.clear(before="2026-08-26T07:00:00Z")
        assert outcome == {"events_deleted": 1, "summaries_deleted": 1}
        assert store.add_event_batch([invalid_event], cursor) == 0
        assert store.upsert_summary(invalid_summary) is False


def test_expand_window_uses_neighboring_rows_not_event_id_distance(tmp_path: Path):
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        events = [ActivityEvent(**{**event(event_id).__dict__, "searchable_text": f"archive {event_id}"}) for event_id in (10, 100, 1000)]
        store.add_event_batch(events, SourceCursor("/events", "events", "1:2", 1, 1, 1, "now", ""))
        expanded = store.expand(segment_id=events[1].segment_id, event_id=100, window=1)
        assert [item["event_id"] for item in expanded["context"]] == [10, 1000]


def test_rebuild_fts_repairs_deliberate_damage_and_matches_canonical_counts(activity_store: ActivityStore):
    activity_store.connection.execute("DELETE FROM activity_events_fts")
    activity_store.connection.execute("DELETE FROM activity_summaries_fts")
    activity_store.connection.commit()
    with pytest.raises(sqlite3.DatabaseError):
        activity_store._validate_fts()
    outcome = activity_store.rebuild_fts()
    assert outcome == {"events": 1, "summaries": 1}
    activity_store._validate_fts()


def test_integrity_status_detects_damaged_external_content_fts(activity_store: ActivityStore):
    assert activity_store.integrity_status() == {"integrity_status": "ok", "fts_status": "ok"}
    activity_store.connection.execute("DELETE FROM activity_events_fts")
    activity_store.connection.commit()

    assert activity_store.integrity_status() == {"integrity_status": "ok", "fts_status": "error"}


def test_browse_returns_coarse_buckets_without_individual_event_evidence(tmp_path: Path):
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        events = [
            ActivityEvent(**{
                **event(1).__dict__,
                "occurred_at": "2026-08-26T06:05:00Z",
                "app_name": "Safari",
                "window_title": "PRIVATE WINDOW ONE",
                "selection_text": "PRIVATE SELECTION ONE",
            }),
            ActivityEvent(**{
                **event(2).__dict__,
                "occurred_at": "2026-08-26T06:45:00Z",
                "app_name": "Xcode",
                "window_title": "PRIVATE WINDOW TWO",
                "selection_text": "PRIVATE SELECTION TWO",
            }),
            ActivityEvent(**{
                **event(3).__dict__,
                "occurred_at": "2026-08-26T07:05:00Z",
                "app_name": "Safari",
                "window_title": "PRIVATE WINDOW THREE",
                "selection_text": "PRIVATE SELECTION THREE",
            }),
            ActivityEvent(**{
                **event(4).__dict__,
                "occurred_at": "2026-08-26T08:05:00Z",
                "app_name": "Safari",
                "window_title": "PRIVATE WINDOW FOUR",
                "selection_text": "PRIVATE SELECTION FOUR",
            }),
        ]
        store.add_event_batch(events, SourceCursor("/events", "events", "1:2", 1, 1, 1, "now", ""))
        store.upsert_summary(ActivitySummary(
            "summary-browse", "/summaries/browse.md", "10min",
            "2026-08-26T06:40:00Z", "2026-08-26T06:50:00Z",
            "Worked on the activity archive.", "browse-hash", 1, "now",
        ))

        buckets = store.browse(limit=10)

    assert [bucket["bucket_start"] for bucket in buckets] == [
        "2026-08-26T08:00:00+00:00",
        "2026-08-26T07:00:00+00:00",
        "2026-08-26T06:00:00+00:00",
    ]
    assert all(bucket["source"] == "computer_history" for bucket in buckets)
    assert all(bucket["evidence_type"] == "summary" for bucket in buckets)
    assert buckets[2]["principal_apps"] == ["Safari", "Xcode"]
    assert buckets[2]["summary_previews"] == ["Worked on the activity archive."]
    serialized = json.dumps(buckets, ensure_ascii=False)
    assert "raw_event" not in serialized
    assert "event_id" not in serialized
    assert "window_title" not in serialized
    assert "url" not in serialized
    assert "PRIVATE" not in serialized


def test_summary_evidence_uses_uniform_computer_history_source(tmp_path: Path):
    summary = ActivitySummary(
        "summary-source", "/summaries/source.md", "10min",
        "2026-08-26T06:40:00Z", "2026-08-26T06:50:00Z",
        "Astra activity source marker", "source-hash", 1, "now",
    )
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        store.upsert_summary(summary)
        discovery = store.search("activity source")
        expanded = store.expand(summary_id=summary.summary_id)

    assert discovery[0]["source"] == "computer_history"
    assert discovery[0]["evidence_type"] == "summary"
    assert expanded["source"] == "computer_history"
    assert expanded["evidence_type"] == "summary"
