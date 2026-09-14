import sqlite3
from pathlib import Path

import pytest

from scripts import import_hermes_history


def _create_hermes_source(path: Path, session_ids: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE sessions (
                id TEXT, title TEXT, source TEXT, started_at REAL,
                ended_at REAL, message_count INTEGER
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                content TEXT, tool_name TEXT, timestamp REAL,
                active INTEGER, compacted INTEGER
            );
        """)
        for index, session_id in enumerate(session_ids, 1):
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, 'cli', ?, NULL, 2)",
                (session_id, f"Session {index}", float(index)),
            )
            connection.executemany(
                "INSERT INTO messages "
                "(session_id, role, content, tool_name, timestamp, active, compacted) "
                "VALUES (?, ?, ?, '', ?, 1, 0)",
                [
                    (session_id, "user", f"question {index}", float(index)),
                    (session_id, "assistant", f"answer {index}", float(index)),
                ],
            )


def _database_schema(path: Path) -> list[tuple[str, str, str]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()


def _append_hermes_message(
    path: Path,
    session_id: str,
    *,
    role: str,
    content: str,
    timestamp: float,
) -> int:
    with sqlite3.connect(path) as connection:
        cursor = connection.execute(
            "INSERT INTO messages "
            "(session_id, role, content, tool_name, timestamp, active, compacted) "
            "VALUES (?, ?, ?, '', ?, 1, 0)",
            (session_id, role, content, timestamp),
        )
        connection.execute(
            "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
            (session_id,),
        )
        return int(cursor.lastrowid)


def test_database_paths_default_to_home_and_repository(monkeypatch, tmp_path):
    home = tmp_path / "User With Spaces"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("HERMES_DB", raising=False)
    monkeypatch.delenv("ASTRA_SESSIONS_DB", raising=False)

    source, target = import_hermes_history.resolve_database_paths()

    assert source == home / ".hermes" / "state.db"
    assert target == import_hermes_history.PROJECT_ROOT / ".astra" / "sessions.db"


def test_database_paths_use_environment_overrides(monkeypatch, tmp_path):
    source_override = tmp_path / "Hermes Data" / "state.db"
    target_override = tmp_path / "Astra Data" / "sessions.db"
    monkeypatch.setenv("HERMES_DB", str(source_override))
    monkeypatch.setenv("ASTRA_SESSIONS_DB", str(target_override))

    assert import_hermes_history.resolve_database_paths() == (
        source_override,
        target_override,
    )


def test_database_path_cli_overrides_take_priority_over_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_DB", str(tmp_path / "env-source.db"))
    monkeypatch.setenv("ASTRA_SESSIONS_DB", str(tmp_path / "env-target.db"))
    cli_source = tmp_path / "CLI Source" / "state.db"
    cli_target = tmp_path / "CLI Target" / "sessions.db"

    assert import_hermes_history.resolve_database_paths(
        str(cli_source), str(cli_target)
    ) == (cli_source, cli_target)


def test_database_paths_preserve_overridable_legacy_constants(monkeypatch, tmp_path):
    source_override = tmp_path / "legacy-source.db"
    target_override = tmp_path / "legacy-target.db"
    monkeypatch.delenv("HERMES_DB", raising=False)
    monkeypatch.delenv("ASTRA_SESSIONS_DB", raising=False)
    monkeypatch.setattr(import_hermes_history, "HERMES_DB", source_override)
    monkeypatch.setattr(import_hermes_history, "SESSIONS_DB", target_override)

    assert import_hermes_history.resolve_database_paths() == (
        source_override,
        target_override,
    )


def test_main_forwards_database_cli_arguments(monkeypatch, tmp_path):
    source = tmp_path / "source path" / "state.db"
    target = tmp_path / "target path" / "sessions.db"
    received = {}

    def fake_sync(**kwargs):
        received.update(kwargs)

    monkeypatch.setattr(import_hermes_history, "sync", fake_sync)

    result = import_hermes_history.main(
        ["--full", "--dry-run", "--source-db", str(source), "--target-db", str(target)]
    )

    assert result == 0
    assert received == {
        "full": True,
        "dry_run": True,
        "source_db": str(source),
        "target_db": str(target),
    }


def test_sync_creates_a_missing_target_parent_with_spaced_paths(tmp_path):
    source = tmp_path / "Hermes Source" / "state.db"
    source.parent.mkdir()
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE sessions ("
            "id TEXT, title TEXT, source TEXT, started_at REAL, "
            "ended_at REAL, message_count INTEGER)"
        )
    target = tmp_path / "Astra Target" / ".astra" / "sessions.db"

    import_hermes_history.sync(source_db=str(source), target_db=str(target))

    assert target.is_file()


def test_dry_run_does_not_create_a_missing_target_and_reports_counts(tmp_path, capsys):
    source = tmp_path / "Hermes Source" / "state.db"
    _create_hermes_source(source, ("new-session",))
    target = tmp_path / "Missing Astra Parent" / ".astra" / "sessions.db"

    import_hermes_history.sync(
        dry_run=True,
        source_db=str(source),
        target_db=str(target),
    )

    output = capsys.readouterr().out
    assert not target.parent.exists()
    assert "New sessions: 1 (skipped 0 existing)" in output
    assert "New messages: 2" in output
    assert "DB total: 0 sessions, 0 msgs" in output


def test_dry_run_does_not_modify_an_existing_target_and_reports_counts(tmp_path, capsys):
    source = tmp_path / "Hermes Source" / "state.db"
    _create_hermes_source(source, ("existing-session", "new-session"))
    target = tmp_path / "Existing Astra" / "sessions.db"
    target.parent.mkdir(parents=True)
    with sqlite3.connect(target) as connection:
        import_hermes_history._ensure_schema(connection)
        connection.execute(
            "INSERT INTO sessions (id, title, started_at, message_count) "
            "VALUES ('existing-session', 'Existing', 1, 1)"
        )
        connection.execute(
            "INSERT INTO messages "
            "(session_id, role, content, timestamp, msg_index) "
            "VALUES ('existing-session', 'user', 'existing', 1, 1)"
        )
        connection.commit()

    bytes_before = target.read_bytes()
    schema_before = _database_schema(target)
    mtime_before = target.stat().st_mtime_ns
    sidecars = [Path(f"{target}{suffix}") for suffix in ("-journal", "-shm", "-wal")]
    sidecars_before = {path: path.read_bytes() for path in sidecars if path.exists()}

    import_hermes_history.sync(
        dry_run=True,
        source_db=str(source),
        target_db=str(target),
    )

    output = capsys.readouterr().out
    assert target.read_bytes() == bytes_before
    assert _database_schema(target) == schema_before
    assert target.stat().st_mtime_ns == mtime_before
    assert {path: path.read_bytes() for path in sidecars if path.exists()} == sidecars_before
    assert "New sessions: 2 (skipped 0 existing)" in output
    assert "New messages: 4" in output
    assert "DB total: 1 sessions, 1 msgs" in output


def test_invalid_source_does_not_create_target_parent(tmp_path):
    source = tmp_path / "missing-source.db"
    target = tmp_path / "Missing Astra Parent" / "sessions.db"

    try:
        import_hermes_history.sync(source_db=str(source), target_db=str(target))
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("missing Hermes source should exit")

    assert not target.parent.exists()


def test_existing_invalid_source_does_not_create_target_parent(tmp_path):
    source = tmp_path / "empty-source.db"
    source.touch()
    target = tmp_path / "Missing Astra Parent" / "sessions.db"

    with pytest.raises(sqlite3.OperationalError, match="Hermes source database"):
        import_hermes_history.sync(source_db=str(source), target_db=str(target))

    assert not target.parent.exists()


def test_stable_snapshot_retries_when_database_generation_changes(monkeypatch, tmp_path):
    states = iter([
        (b"old-main", b"old-wal"),
        (b"new-main", b"new-wal"),
        (b"new-main", b"new-wal"),
    ])
    monkeypatch.setattr(
        import_hermes_history,
        "_read_database_generation",
        lambda path: next(states),
        raising=False,
    )

    assert import_hermes_history._read_stable_database_files(tmp_path / "sessions.db") == (
        b"new-main",
        b"new-wal",
    )


def test_dry_run_reads_wal_state_without_modifying_target_sidecars(tmp_path, capsys):
    source = tmp_path / "source.db"
    _create_hermes_source(source, ("existing-session", "new-session"))
    target = tmp_path / "target.db"
    with sqlite3.connect(target) as connection:
        import_hermes_history._ensure_schema(connection)

    writer = sqlite3.connect(target)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute(
            "INSERT INTO sessions (id, title, started_at, message_count) "
            "VALUES ('existing-session', 'Existing', 1, 0)"
        )
        writer.commit()
        tracked_paths = [target, Path(f"{target}-shm"), Path(f"{target}-wal")]
        bytes_before = {path: path.read_bytes() for path in tracked_paths}
        mtimes_before = {path: path.stat().st_mtime_ns for path in tracked_paths}

        import_hermes_history.sync(
            dry_run=True,
            source_db=str(source),
            target_db=str(target),
        )

        output = capsys.readouterr().out
        assert {path: path.read_bytes() for path in tracked_paths} == bytes_before
        assert {path: path.stat().st_mtime_ns for path in tracked_paths} == mtimes_before
        assert "New sessions: 2 (skipped 0 existing)" in output
        assert "New messages: 4" in output
        assert "DB total: 1 sessions, 0 msgs" in output
    finally:
        writer.close()


def test_sync_adds_new_messages_to_an_existing_hermes_session_once(tmp_path, capsys):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _create_hermes_source(source, ("long-session",))

    import_hermes_history.sync(source_db=str(source), target_db=str(target))
    source_message_id = _append_hermes_message(
        source,
        "long-session",
        role="assistant",
        content="later answer",
        timestamp=3.0,
    )

    import_hermes_history.sync(source_db=str(source), target_db=str(target))
    second_output = capsys.readouterr().out
    import_hermes_history.sync(source_db=str(source), target_db=str(target))
    third_output = capsys.readouterr().out

    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM hermes_sync_messages "
            "WHERE source_session_id = ? AND source_message_id = ?",
            ("long-session", str(source_message_id)),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT message_count FROM sessions WHERE source_session_key = ?",
            ("hermes:long-session",),
        ).fetchone()[0] == 3
    assert "New messages: 1" in second_output
    assert "New messages: 0" in third_output


def test_sync_updates_changed_source_message_without_duplication(tmp_path, capsys):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _create_hermes_source(source, ("edited-session",))
    import_hermes_history.sync(source_db=str(source), target_db=str(target))
    capsys.readouterr()

    with sqlite3.connect(source) as connection:
        source_message_id = connection.execute(
            "SELECT id FROM messages WHERE session_id = ? AND role = 'assistant'",
            ("edited-session",),
        ).fetchone()[0]
        connection.execute(
            "UPDATE messages SET content = ? WHERE id = ?",
            ("corrected answer", source_message_id),
        )

    import_hermes_history.sync(source_db=str(source), target_db=str(target))
    output = capsys.readouterr().out

    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        assert connection.execute(
            "SELECT m.content FROM messages AS m "
            "JOIN hermes_sync_messages AS h ON h.target_message_id = m.id "
            "WHERE h.source_session_id = ? AND h.source_message_id = ?",
            ("edited-session", str(source_message_id)),
        ).fetchone()[0] == "corrected answer"
    assert "Updated messages: 1" in output


def test_message_level_dry_run_reports_delta_without_modifying_target(tmp_path, capsys):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _create_hermes_source(source, ("dry-long-session",))
    import_hermes_history.sync(source_db=str(source), target_db=str(target))
    _append_hermes_message(
        source,
        "dry-long-session",
        role="user",
        content="one more question",
        timestamp=4.0,
    )
    capsys.readouterr()
    before = target.read_bytes()

    import_hermes_history.sync(
        dry_run=True,
        source_db=str(source),
        target_db=str(target),
    )
    output = capsys.readouterr().out

    assert target.read_bytes() == before
    assert "New sessions: 0" in output
    assert "New messages: 1" in output


def test_sync_namespaces_hermes_session_when_target_id_is_already_used(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _create_hermes_source(source, ("shared-id",))
    with sqlite3.connect(target) as connection:
        import_hermes_history._ensure_schema(connection)
        connection.execute(
            "INSERT INTO sessions "
            "(id, title, started_at, message_count, personality, source_session_key) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("shared-id", "Astra session", 1.0, 0, "lyra", "astra:shared-id"),
        )

    import_hermes_history.sync(source_db=str(source), target_db=str(target))

    with sqlite3.connect(target) as connection:
        rows = connection.execute(
            "SELECT id, source_session_key FROM sessions ORDER BY source_session_key"
        ).fetchall()
        assert len(rows) == 2
        assert ("shared-id", "astra:shared-id") in rows
        assert connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE source_session_key = ?",
            ("hermes:shared-id",),
        ).fetchone()[0] == 1


def test_sync_adopts_matching_legacy_import_and_builds_message_mappings(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _create_hermes_source(source, ("legacy-session",))
    with sqlite3.connect(target) as connection:
        import_hermes_history._ensure_schema(connection)
        connection.execute(
            "INSERT INTO sessions "
            "(id, title, started_at, message_count, personality) "
            "VALUES (?, ?, ?, ?, ?)",
            ("legacy-session", "Legacy", 1.0, 2, "hermes-cli"),
        )
        connection.executemany(
            "INSERT INTO messages "
            "(session_id, role, content, tool_name, timestamp, msg_index) "
            "VALUES (?, ?, ?, '', ?, ?)",
            [
                ("legacy-session", "user", "question 1", 1.0, 1),
                ("legacy-session", "assistant", "answer 1", 1.0, 2),
            ],
        )

    import_hermes_history.sync(source_db=str(source), target_db=str(target))

    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM hermes_sync_messages").fetchone()[0] == 2
        assert connection.execute(
            "SELECT source_session_key FROM sessions WHERE id = 'legacy-session'"
        ).fetchone()[0] == "hermes:legacy-session"


def test_missing_target_dry_run_handles_one_hundred_sessions(tmp_path, capsys):
    source = tmp_path / "source.db"
    target = tmp_path / "missing" / "target.db"
    _create_hermes_source(
        source,
        tuple(f"session-{index}" for index in range(100)),
    )

    import_hermes_history.sync(
        dry_run=True,
        source_db=str(source),
        target_db=str(target),
    )

    output = capsys.readouterr().out
    assert not target.exists()
    assert "New sessions: 100" in output
    assert "New messages: 200" in output


def test_failed_large_sync_rolls_back_all_session_and_message_writes(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _create_hermes_source(
        source,
        tuple(f"session-{index}" for index in range(101)),
    )
    original = import_hermes_history._normalized_source_message

    def fail_on_last_session(message, index):
        if message["session_id"] == "session-100":
            raise RuntimeError("injected sync failure")
        return original(message, index)

    monkeypatch.setattr(
        import_hermes_history,
        "_normalized_source_message",
        fail_on_last_session,
    )

    with pytest.raises(RuntimeError, match="injected sync failure"):
        import_hermes_history.sync(source_db=str(source), target_db=str(target))

    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_sync_adopts_edited_legacy_message_then_updates_it(tmp_path, capsys):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _create_hermes_source(source, ("legacy-edited",))
    with sqlite3.connect(target) as connection:
        import_hermes_history._ensure_schema(connection)
        connection.execute(
            "INSERT INTO sessions "
            "(id, title, started_at, message_count, personality) "
            "VALUES ('legacy-edited', 'Legacy', 1, 2, 'hermes-cli')"
        )
        connection.executemany(
            "INSERT INTO messages "
            "(session_id, role, content, tool_name, timestamp, msg_index) "
            "VALUES ('legacy-edited', ?, ?, '', ?, ?)",
            [
                ("user", "question 1", 1.0, 1),
                ("assistant", "old answer", 1.0, 2),
            ],
        )

    import_hermes_history.sync(source_db=str(source), target_db=str(target))
    output = capsys.readouterr().out

    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM hermes_sync_messages").fetchone()[0] == 2
        assert connection.execute(
            "SELECT content FROM messages WHERE session_id='legacy-edited' AND role='assistant'"
        ).fetchone()[0] == "answer 1"
    assert "Updated messages: 1" in output


def test_ensure_schema_rebuilds_missing_fts_for_existing_messages(tmp_path):
    target = tmp_path / "legacy-no-fts.db"
    with sqlite3.connect(target) as connection:
        connection.executescript("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, title TEXT DEFAULT '', started_at REAL NOT NULL,
                ended_at REAL, message_count INTEGER DEFAULT 0, personality TEXT DEFAULT ''
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT DEFAULT '', tool_name TEXT DEFAULT '',
                timestamp REAL NOT NULL, msg_index INTEGER NOT NULL
            );
            INSERT INTO sessions VALUES ('old', 'Old', 1, NULL, 1, 'work');
            INSERT INTO messages
                (session_id, role, content, tool_name, timestamp, msg_index)
                VALUES ('old', 'user', 'searchable needle', '', 1, 1);
        """)
        import_hermes_history._ensure_schema(connection)

        assert connection.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'needle'"
        ).fetchone()[0] == 1


def test_sync_preserves_inactive_source_history_without_duplicate_indexes(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _create_hermes_source(source, ("archival-session",))
    import_hermes_history.sync(source_db=str(source), target_db=str(target))
    with sqlite3.connect(source) as connection:
        connection.execute(
            "UPDATE messages SET active=0, compacted=0 "
            "WHERE session_id='archival-session' AND role='user'"
        )

    import_hermes_history.sync(source_db=str(source), target_db=str(target))

    with sqlite3.connect(target) as connection:
        target_sid = connection.execute(
            "SELECT id FROM sessions WHERE source_session_key='hermes:archival-session'"
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT role, msg_index FROM messages WHERE session_id=? ORDER BY msg_index",
            (target_sid,),
        ).fetchall()
        assert rows == [("assistant", 1), ("user", 2)]
        assert connection.execute(
            "SELECT message_count FROM sessions WHERE id=?",
            (target_sid,),
        ).fetchone()[0] == 2


def test_sync_repairs_mapping_whose_target_message_was_deleted(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _create_hermes_source(source, ("repair-session",))
    import_hermes_history.sync(source_db=str(source), target_db=str(target))
    with sqlite3.connect(target) as connection:
        deleted_message_id = connection.execute(
            "SELECT target_message_id FROM hermes_sync_messages "
            "WHERE source_session_id='repair-session' ORDER BY source_message_id LIMIT 1"
        ).fetchone()[0]
        connection.execute("DELETE FROM messages WHERE id=?", (deleted_message_id,))

    import_hermes_history.sync(source_db=str(source), target_db=str(target))

    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id=m.session_id "
            "WHERE s.source_session_key='hermes:repair-session'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM hermes_sync_messages "
            "WHERE source_session_id='repair-session'"
        ).fetchone()[0] == 2


def test_sync_adopts_legacy_session_with_message_already_inactive(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _create_hermes_source(source, ("legacy-inactive",))
    with sqlite3.connect(source) as connection:
        connection.execute(
            "UPDATE messages SET active=0, compacted=0 "
            "WHERE session_id='legacy-inactive' AND role='user'"
        )
    with sqlite3.connect(target) as connection:
        import_hermes_history._ensure_schema(connection)
        connection.execute(
            "INSERT INTO sessions "
            "(id, title, started_at, message_count, personality) "
            "VALUES ('legacy-inactive', 'Legacy', 1, 2, 'hermes-cli')"
        )
        connection.executemany(
            "INSERT INTO messages "
            "(session_id, role, content, tool_name, timestamp, msg_index) "
            "VALUES ('legacy-inactive', ?, ?, '', 1, ?)",
            [("user", "question 1", 1), ("assistant", "answer 1", 2)],
        )

    import_hermes_history.sync(source_db=str(source), target_db=str(target))

    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT role, msg_index FROM messages "
            "WHERE session_id='legacy-inactive' ORDER BY msg_index"
        ).fetchall() == [("assistant", 1), ("user", 2)]
        assert connection.execute(
            "SELECT COUNT(*) FROM hermes_sync_messages "
            "WHERE source_session_id='legacy-inactive'"
        ).fetchone()[0] == 2


def test_sync_adopts_legacy_inactive_prefix_and_edited_active_message(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _create_hermes_source(source, ("legacy-inactive-edited",))
    with sqlite3.connect(source) as connection:
        connection.execute(
            "UPDATE messages SET active=0, compacted=0 "
            "WHERE session_id='legacy-inactive-edited' AND role='user'"
        )
        connection.execute(
            "UPDATE messages SET content='edited answer' "
            "WHERE session_id='legacy-inactive-edited' AND role='assistant'"
        )
    with sqlite3.connect(target) as connection:
        import_hermes_history._ensure_schema(connection)
        connection.execute(
            "INSERT INTO sessions "
            "(id, title, started_at, message_count, personality) "
            "VALUES ('legacy-inactive-edited', 'Legacy', 1, 2, 'hermes-cli')"
        )
        connection.executemany(
            "INSERT INTO messages "
            "(session_id, role, content, tool_name, timestamp, msg_index) "
            "VALUES ('legacy-inactive-edited', ?, ?, '', 1, ?)",
            [("user", "question 1", 1), ("assistant", "old answer", 2)],
        )

    import_hermes_history.sync(source_db=str(source), target_db=str(target))

    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT role, content, msg_index FROM messages "
            "WHERE session_id='legacy-inactive-edited' ORDER BY msg_index"
        ).fetchall() == [
            ("assistant", "edited answer", 1),
            ("user", "question 1", 2),
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM hermes_sync_messages "
            "WHERE source_session_id='legacy-inactive-edited'"
        ).fetchone()[0] == 2
