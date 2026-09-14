import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent.runtime import session_recall as recall_module
from agent.runtime.tools import session_recall as session_recall_tool
from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.session_recall import SessionRecall


def test_default_path_reads_environment_when_instance_is_constructed(
    monkeypatch, tmp_path
):
    configured = tmp_path / "configured" / "sessions.db"
    monkeypatch.setenv("ASTRA_SESSION_RECALL_DB", str(configured))

    recall = SessionRecall()

    assert recall._db_path == configured


def test_explicit_path_wins_over_environment(monkeypatch, tmp_path):
    configured = tmp_path / "configured.db"
    explicit = tmp_path / "explicit.db"
    monkeypatch.setenv("ASTRA_SESSION_RECALL_DB", str(configured))

    recall = SessionRecall(explicit)

    assert recall._db_path == explicit


def test_blank_environment_uses_current_module_default(monkeypatch, tmp_path):
    fallback = tmp_path / "fallback.db"
    monkeypatch.setenv("ASTRA_SESSION_RECALL_DB", "   ")
    monkeypatch.setattr(recall_module, "DB_PATH", fallback)

    recall = SessionRecall()

    assert recall._db_path == fallback


def test_pytest_default_session_recall_path_is_per_test_tmp_path(tmp_path):
    assert SessionRecall()._db_path == tmp_path / "session-recall.db"


def test_pytest_default_context_index_reader_path_is_per_test_tmp_path(tmp_path):
    assert os.environ["ASTRA_CONTEXT_INDEX_SESSIONS_DB"] == str(tmp_path / "session-recall.db")


def test_pytest_subprocess_overrides_ambient_context_reader_path(tmp_path):
    ambient_path = tmp_path / "ambient-reader.db"
    env = os.environ.copy()
    env["ASTRA_CONTEXT_INDEX_SESSIONS_DB"] = str(ambient_path)
    env["ASTRA_SESSION_RECALL_DB"] = str(tmp_path / "ambient-writer.db")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_context_index_runtime.py::test_pytest_isolation_binds_context_index_factory_to_test_archive",
            "-q",
        ],
        cwd=Path(__file__).parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not ambient_path.exists()


def _index_columns(connection: sqlite3.Connection, index_name: str) -> tuple[str, ...]:
    return tuple(str(row[2]) for row in connection.execute(f"PRAGMA index_info({index_name})"))


def test_recall_writer_creates_context_index_query_indexes(tmp_path):
    recall = SessionRecall(tmp_path / "indexed.db")
    recall.init_db()
    connection = recall._get_conn()

    assert _index_columns(connection, "idx_messages_timestamp") == ("timestamp", "id")
    assert _index_columns(connection, "idx_messages_session_timestamp") == (
        "session_id", "timestamp", "id",
    )
    assert _index_columns(connection, "idx_sessions_workspace_key") == ("workspace_key",)
    recall.close()


def test_recall_writer_adds_query_indexes_to_legacy_archive_idempotently(tmp_path):
    path = tmp_path / "legacy-indexes.db"
    first = SessionRecall(path)
    first.init_db()
    first.close()

    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX idx_messages_timestamp")
        connection.execute("DROP INDEX idx_messages_session_timestamp")
        connection.execute("DROP INDEX idx_sessions_workspace_key")

    for _ in range(2):
        reopened = SessionRecall(path)
        reopened.init_db()
        reopened.close()

    with sqlite3.connect(path) as connection:
        assert _index_columns(connection, "idx_messages_timestamp") == ("timestamp", "id")
        assert _index_columns(connection, "idx_messages_session_timestamp") == (
            "session_id", "timestamp", "id",
        )
        assert _index_columns(connection, "idx_sessions_workspace_key") == ("workspace_key",)


def test_recall_session_mapping_survives_restart_and_reopens(tmp_path):
    db_path = tmp_path / "sessions.db"
    first = SessionRecall(db_path)
    first.init_db()
    sid = first.get_or_create_session(
        "session_alpha",
        title="session_alpha",
        personality="work",
    )
    first.log_message(sid, "user", "first")
    first.close_session(sid)
    first.close()

    second = SessionRecall(db_path)
    second.init_db()
    reopened = second.get_or_create_session(
        "session_alpha",
        title="session_alpha",
        personality="work",
    )
    second.log_message(reopened, "assistant", "second")

    assert reopened == sid
    conn = sqlite3.connect(db_path)
    count, ended_at = conn.execute(
        "SELECT message_count, ended_at FROM sessions WHERE id = ?",
        (sid,),
    ).fetchone()
    assert count == 2
    assert ended_at is None
    assert conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE source_session_key = ?",
        ("session_alpha",),
    ).fetchone()[0] == 1
    conn.close()
    second.close()


def test_recall_schema_migrates_existing_database(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE sessions ("
        "id TEXT PRIMARY KEY, title TEXT DEFAULT '', started_at REAL NOT NULL, "
        "ended_at REAL, message_count INTEGER DEFAULT 0, personality TEXT DEFAULT '')"
    )
    conn.commit()
    conn.close()

    recall = SessionRecall(db_path)
    recall.init_db()
    columns = {
        row[1]
        for row in recall._get_conn().execute("PRAGMA table_info(sessions)")
    }
    assert {"source_session_key", "workspace_key", "workspace_root"} <= columns
    recall.close()


def test_recall_reopen_updates_only_nonempty_workspace_values(tmp_path):
    recall = SessionRecall(tmp_path / "workspace.db")
    recall.init_db()
    sid = recall.get_or_create_session(
        "session_alpha",
        workspace_key="first-key",
        workspace_root="/first/root",
    )

    reopened = recall.get_or_create_session(
        "session_alpha",
        workspace_key="second-key",
    )

    assert reopened == sid
    assert tuple(recall._get_conn().execute(
        "SELECT workspace_key, workspace_root FROM sessions WHERE id = ?", (sid,)
    ).fetchone()) == ("second-key", "/first/root")


def test_recall_keeps_canonical_writes_when_fts_trigger_fails(tmp_path):
    db_path = tmp_path / "fts-fail-open.db"
    recall = SessionRecall(db_path)
    recall.init_db()
    sid = recall.create_session("fts recovery")
    conn = recall._get_conn()
    conn.executescript("""
        DROP TRIGGER messages_ai;
        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts_broken(rowid, content)
            VALUES (new.id, new.content);
        END;
    """)

    message_id = recall.log_message(sid, "user", "canonical survives broken search index")

    assert conn.execute("SELECT content FROM messages WHERE id=?", (message_id,)).fetchone()[0].startswith("canonical")
    assert conn.execute("SELECT value FROM recall_meta WHERE key='fts_stale'").fetchone()[0] == "1"
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'messages_a%'"
    ).fetchone()[0] == 0
    assert recall.search("canonical")[0]["message_id"] == message_id
    recall.close()

    reopened = SessionRecall(db_path)
    reopened.init_db()
    assert reopened._fts_stale is False
    assert reopened._get_conn().execute(
        "SELECT 1 FROM recall_meta WHERE key='fts_stale'"
    ).fetchone() is None
    assert reopened.search("canonical")[0]["message_id"] == message_id
    reopened.close()



def test_init_db_retries_once_after_operational_error(tmp_path, monkeypatch):
    recall = SessionRecall(tmp_path / "retry.db")
    calls = []
    original = recall._init_db_once

    def flaky_once():
        calls.append("init")
        if len(calls) == 1:
            raise sqlite3.OperationalError("disk I/O error")
        original()

    monkeypatch.setattr(recall, "_init_db_once", flaky_once)
    monkeypatch.setattr(recall_module.time, "sleep", lambda _seconds: None)

    recall.init_db()
    assert calls == ["init", "init"]
    assert recall._get_conn().execute("SELECT 1").fetchone()[0] == 1
    recall.close()


class _WalFailOnceConnection:
    def __init__(self, real: sqlite3.Connection):
        self._real = real
        self._wal_failed = False

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        if name in {"_real", "_wal_failed"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._real, name, value)

    def execute(self, sql, *args):
        if "JOURNAL_MODE=WAL" in str(sql).upper() and not self._wal_failed:
            self._wal_failed = True
            raise sqlite3.OperationalError("disk I/O error")
        return self._real.execute(sql, *args)


def test_wal_failure_falls_back_without_breaking_init(tmp_path, monkeypatch):
    wrapped = []
    real_connect = recall_module.sqlite3.connect

    def flaky_connect(*args, **kwargs):
        connection = _WalFailOnceConnection(real_connect(*args, **kwargs))
        wrapped.append(connection)
        return connection

    monkeypatch.setattr(recall_module.sqlite3, "connect", flaky_connect)

    recall = SessionRecall(tmp_path / "wal-fallback.db")
    recall.init_db()
    recall.create_session("wal fallback")
    assert wrapped[0]._wal_failed is True
    assert recall.search("wal fallback") is not None
    recall.close()


def test_source_filter_applies_to_fts_like_and_unfiltered_search(tmp_path):
    recall = SessionRecall(tmp_path / "sources.db")
    recall.init_db()
    session_ids = {
        "astra": recall.get_or_create_session("session_native", title="native"),
        "hermes": recall.get_or_create_session("hermes:source", title="hermes"),
        "api": recall.get_or_create_session("session_api_sister", title="api"),
    }
    astra_wildcard_decoy = recall.get_or_create_session(
        "sessionXapiYnative",
        title="native wildcard decoy",
    )
    for sid in [*session_ids.values(), astra_wildcard_decoy]:
        recall.log_message(sid, "user", "sharedneedle hi")

    assert {
        result["session_id"] for result in recall.search("sharedneedle", limit=10)
    } == {*session_ids.values(), astra_wildcard_decoy}
    for source_type, expected_id in session_ids.items():
        expected_ids = (
            {expected_id, astra_wildcard_decoy}
            if source_type == "astra"
            else {expected_id}
        )
        assert {
            result["session_id"]
            for result in recall.search(
                "sharedneedle",
                limit=10,
                source_type=source_type,
            )
        } == expected_ids
        assert {
            result["session_id"]
            for result in recall.search("hi", limit=10, source_type=source_type)
        } == expected_ids

    with pytest.raises(ValueError, match="source_type"):
        recall.search("sharedneedle", source_type="unknown")
    recall.close()


def test_source_filter_applies_before_browse_limit_and_includes_legacy_astra(tmp_path):
    recall = SessionRecall(tmp_path / "browse-sources.db")
    recall.init_db()
    api_id = recall.get_or_create_session("session_api_sister", title="api")
    legacy_astra_id = recall.create_session("legacy native")
    hermes_id = recall.get_or_create_session("hermes:source", title="hermes")
    conn = recall._get_conn()
    conn.execute("UPDATE sessions SET started_at = 1 WHERE id = ?", (api_id,))
    conn.execute("UPDATE sessions SET started_at = 2 WHERE id = ?", (legacy_astra_id,))
    conn.execute("UPDATE sessions SET started_at = 3 WHERE id = ?", (hermes_id,))
    conn.commit()

    assert recall.browse(limit=1, source_type="api")[0]["session_id"] == api_id
    assert recall.browse(limit=1, source_type="astra")[0]["session_id"] == legacy_astra_id
    assert recall.browse(limit=1, source_type="hermes")[0]["session_id"] == hermes_id
    recall.close()


def test_session_search_tool_exposes_and_forwards_source_filter(monkeypatch):
    calls = []

    class FakeRecall:
        def search(self, query, *, limit, window, sort, source_type):
            calls.append((query, limit, window, sort, source_type))
            return []

    monkeypatch.setattr(session_recall_tool, "_SR", FakeRecall())
    payload = session_recall_tool._search(query="needle", source_type="api")
    registry = ToolRegistry()
    session_recall_tool.register_session_recall_tools(registry)
    source_schema = registry.get("session_search").parameters["properties"]["source_type"]

    assert calls == [("needle", 3, 3, None, "api")]
    assert '"source_type": "api"' in payload
    assert source_schema["enum"] == ["", "astra", "hermes", "api", "learning"]
