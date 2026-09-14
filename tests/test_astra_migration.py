import json
import sqlite3

import pytest

from scripts import migrate_legacy_astra_state as migration


def _create_legacy_task_schema(path, *, case_id: str, task_id: str = "") -> None:
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        db.executescript(
            """
            CREATE TABLE cases (
                id TEXT PRIMARY KEY,
                goal TEXT NOT NULL
            );
            CREATE TABLE case_sessions (
                case_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                PRIMARY KEY(case_id, session_id)
            );
            CREATE TABLE task_runs (
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                case_id TEXT NOT NULL DEFAULT ''
            );
            """
        )
        db.execute("INSERT INTO cases(id, goal) VALUES (?, ?)", (case_id, f"goal-{case_id}"))
        db.execute(
            "INSERT INTO case_sessions(case_id, session_id) VALUES (?, ?)",
            (case_id, f"session-{case_id}"),
        )
        if task_id:
            db.execute(
                "INSERT INTO task_runs(id, request_id, case_id) VALUES (?, ?, ?)",
                (task_id, f"request-{task_id}", case_id),
            )


def test_legacy_state_merge_is_idempotent_and_current_values_win(tmp_path, monkeypatch):
    legacy = tmp_path / ".agent_system"
    astra = tmp_path / ".astra"
    legacy.mkdir()
    astra.mkdir()
    (legacy / "settings.json").write_text(
        json.dumps({
            "model": "legacy",
            "nested": {"legacy_only": 1, "shared": "legacy"},
        }),
        encoding="utf-8",
    )
    (astra / "settings.json").write_text(
        json.dumps({
            "model": "current",
            "nested": {"shared": "current"},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(migration, "LEGACY_DIR", legacy)
    monkeypatch.setattr(migration, "ASTRA_DIR", astra)

    migration.migrate()
    first = json.loads((astra / "settings.json").read_text(encoding="utf-8"))
    migration.migrate()
    second = json.loads((astra / "settings.json").read_text(encoding="utf-8"))

    assert first == second
    assert first == {
        "model": "current",
        "nested": {"legacy_only": 1, "shared": "current"},
    }


def test_existing_task_database_preserves_current_cases_without_importing_legacy_cases(
    tmp_path, monkeypatch
):
    legacy = tmp_path / ".agent_system"
    astra = tmp_path / ".astra"
    legacy.mkdir()
    astra.mkdir()
    _create_legacy_task_schema(legacy / "tasks.db", case_id="legacy-case")
    _create_legacy_task_schema(astra / "tasks.db", case_id="current-case")
    monkeypatch.setattr(migration, "LEGACY_DIR", legacy)
    monkeypatch.setattr(migration, "ASTRA_DIR", astra)

    migration.migrate()

    with sqlite3.connect(astra / "tasks.db") as db:
        case_ids = {row[0] for row in db.execute("SELECT id FROM cases")}
        session_case_ids = {
            row[0] for row in db.execute("SELECT case_id FROM case_sessions")
        }
    assert case_ids == {"current-case"}
    assert session_case_ids == {"current-case"}


def test_first_task_database_migration_keeps_tasks_without_case_tables(tmp_path, monkeypatch):
    legacy = tmp_path / ".agent_system"
    astra = tmp_path / ".astra"
    legacy.mkdir()
    astra.mkdir()
    _create_legacy_task_schema(
        legacy / "tasks.db",
        case_id="legacy-case",
        task_id="legacy-task",
    )
    monkeypatch.setattr(migration, "LEGACY_DIR", legacy)
    monkeypatch.setattr(migration, "ASTRA_DIR", astra)

    migration.migrate()

    with sqlite3.connect(astra / "tasks.db") as db:
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        task_columns = {
            row[1] for row in db.execute("PRAGMA table_info(task_runs)")
        }
        task_rows = set(db.execute("SELECT id, case_id FROM task_runs"))
    assert "cases" not in tables
    assert "case_sessions" not in tables
    assert "case_id" in task_columns
    assert task_rows == {("legacy-task", "legacy-case")}
    assert list(astra.glob(".tasks.db.*.tmp*")) == []


def test_first_task_database_cleanup_failure_is_retryable(tmp_path, monkeypatch):
    legacy = tmp_path / ".agent_system"
    astra = tmp_path / ".astra"
    legacy.mkdir()
    astra.mkdir()
    _create_legacy_task_schema(
        legacy / "tasks.db",
        case_id="legacy-case",
        task_id="legacy-task",
    )
    monkeypatch.setattr(migration, "LEGACY_DIR", legacy)
    monkeypatch.setattr(migration, "ASTRA_DIR", astra)
    table_exists = migration._table_exists

    def fail_before_second_drop(conn, table, schema="main"):
        if table == "cases":
            raise RuntimeError("injected cleanup failure")
        return table_exists(conn, table, schema)

    monkeypatch.setattr(migration, "_table_exists", fail_before_second_drop)

    with pytest.raises(RuntimeError, match="injected cleanup failure"):
        migration.migrate()

    assert not (astra / "tasks.db").exists()
    assert list(astra.glob(".tasks.db.*.tmp*")) == []
    monkeypatch.setattr(migration, "_table_exists", table_exists)

    migration.migrate()

    with sqlite3.connect(astra / "tasks.db") as db:
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        task_ids = {row[0] for row in db.execute("SELECT id FROM task_runs")}
    assert "cases" not in tables
    assert "case_sessions" not in tables
    assert task_ids == {"legacy-task"}
    assert list(astra.glob(".tasks.db.*.tmp*")) == []


@pytest.mark.parametrize("fail_cleanup", [False, True])
def test_copied_database_connections_close_before_publish_or_cleanup(
    tmp_path, monkeypatch, fail_cleanup
):
    legacy = tmp_path / "legacy"
    astra = tmp_path / "astra"
    legacy.mkdir()
    _create_legacy_task_schema(legacy / "tasks.db", case_id="old")
    monkeypatch.setattr(migration, "LEGACY_DIR", legacy)
    monkeypatch.setattr(migration, "ASTRA_DIR", astra)
    connections = []
    connect = sqlite3.connect

    def track_connection(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(migration.sqlite3, "connect", track_connection)
    if fail_cleanup:
        def fail(*args, **kwargs):
            raise RuntimeError("cleanup failed")
        monkeypatch.setattr(migration, "_table_exists", fail)
        with pytest.raises(RuntimeError, match="cleanup failed"):
            migration._merge_database("tasks.db", (), drop_after_copy=("cases",))
    else:
        migration._merge_database("tasks.db", (), drop_after_copy=("cases",))

    assert len(connections) == 2
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    assert list(astra.glob(".tasks.db.*.tmp*")) == []
