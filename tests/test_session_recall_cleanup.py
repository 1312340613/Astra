from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from pathlib import Path

import pytest

import scripts.maintenance.cleanup_session_recall_test_leak as cleanup_module
from scripts.maintenance.cleanup_session_recall_test_leak import (
    CheckpointBusyError,
    CleanupError,
    SnapshotExpectation,
    apply_cleanup,
    backup_database,
    inspect_database,
    main,
    run_checkpoint,
)
from session_recall import SessionRecall

SMALL_EXPECTATION = SnapshotExpectation(
    target_session_count=2,
    target_message_count=3,
)
PRODUCTION_MANIFEST = (
    Path(__file__).parents[1]
    / "scripts"
    / "maintenance"
    / "session_recall_test_leak_ids.json"
)


def test_production_manifest_is_sorted_unique_and_canonical():
    ids = json.loads(PRODUCTION_MANIFEST.read_text(encoding="utf-8"))

    assert len(ids) == 201
    assert len(set(ids)) == 201
    assert ids == sorted(ids)
    assert all(
        re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{6}", item)
        for item in ids
    )


@pytest.fixture
def cleanup_db(tmp_path: Path) -> tuple[Path, list[str], str]:
    database_path = tmp_path / "sessions.db"
    recall = SessionRecall(database_path)
    recall.init_db()
    first_key = "backend_question_" + "1" * 32
    second_key = "backend_approval_cancel_" + "2" * 32
    first_target = recall.get_or_create_session(first_key, title=first_key)
    second_target = recall.get_or_create_session(second_key, title=second_key)
    preserved_id = recall.create_session("preserved")
    recall.log_message(first_target, "user", "first target message")
    recall.log_message(first_target, "assistant", "second target message")
    recall.log_message(second_target, "user", "third target message")
    recall.log_message(preserved_id, "assistant", "preserved message")
    recall.close()
    return database_path, [first_target, second_target], preserved_id


def database_counts(database_path: Path) -> tuple[int, int, int]:
    with sqlite3.connect(database_path) as connection:
        return tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("sessions", "messages", "messages_fts")
        )


def session_ids(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        return {
            str(row[0])
            for row in connection.execute("SELECT id FROM sessions")
        }


def test_dry_run_reports_targets_without_mutation(cleanup_db):
    database_path, target_ids, _preserved_id = cleanup_db
    before = database_counts(database_path)

    with sqlite3.connect(database_path) as connection:
        result = inspect_database(connection, target_ids, SMALL_EXPECTATION)

    assert result.target_session_count == 2
    assert result.target_message_count == 3
    assert database_counts(database_path) == before


def test_unrelated_session_growth_does_not_invalidate_manifest(cleanup_db):
    database_path, target_ids, _preserved_id = cleanup_db
    recall = SessionRecall(database_path)
    recall.init_db()
    recall.create_session("new legitimate session")
    recall.close()

    with sqlite3.connect(database_path) as connection:
        result = inspect_database(connection, target_ids, SMALL_EXPECTATION)

    assert result.session_count == 4
    assert result.target_session_count == 2


def test_inspection_leaves_no_transaction_open(cleanup_db):
    database_path, target_ids, _preserved_id = cleanup_db

    with sqlite3.connect(database_path) as connection:
        inspect_database(connection, target_ids, SMALL_EXPECTATION)
        assert connection.in_transaction is False


def test_apply_creates_verified_backup_and_deletes_only_manifest_rows(
    cleanup_db, tmp_path
):
    database_path, target_ids, preserved_id = cleanup_db
    backup_path = tmp_path / "backup.db"

    result = apply_cleanup(database_path, backup_path, target_ids, SMALL_EXPECTATION)

    assert result.deleted_sessions == 2
    assert result.deleted_messages == 3
    assert database_counts(backup_path) == (3, 4, 4)
    assert database_counts(database_path) == (1, 1, 1)
    assert session_ids(database_path) == {preserved_id}


def test_snapshot_mismatch_aborts_before_backup_or_mutation(cleanup_db, tmp_path):
    database_path, target_ids, _preserved_id = cleanup_db
    backup_path = tmp_path / "backup.db"
    wrong = dataclasses.replace(SMALL_EXPECTATION, target_message_count=4)
    before = database_counts(database_path)

    with pytest.raises(CleanupError, match="snapshot"):
        apply_cleanup(database_path, backup_path, target_ids, wrong)

    assert not backup_path.exists()
    assert database_counts(database_path) == before


def test_backup_failure_removes_destination_created_before_validation(
    cleanup_db, tmp_path, monkeypatch
):
    database_path, target_ids, _preserved_id = cleanup_db
    backup_path = tmp_path / "partial-backup.db"
    real_connect = cleanup_module.sqlite3.connect
    opened_connections = []

    class FailingBackupConnection:
        def __init__(self, connection):
            self._connection = connection

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, *args):
            try:
                return self._connection.__exit__(*args)
            finally:
                self._connection.close()

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def backup(self, destination):
            raise sqlite3.DatabaseError("forced backup failure")

    def fail_after_destination_exists(path, *args, **kwargs):
        connection = real_connect(path, *args, **kwargs)
        opened_connections.append(connection)
        if str(path).startswith(database_path.resolve().as_uri()):
            return FailingBackupConnection(connection)
        return connection

    monkeypatch.setattr(cleanup_module.sqlite3, "connect", fail_after_destination_exists)

    with pytest.raises(sqlite3.DatabaseError, match="forced backup failure"):
        backup_database(database_path, backup_path, target_ids, SMALL_EXPECTATION)

    assert not backup_path.exists()
    assert len(opened_connections) == 2
    for connection in opened_connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_duplicate_manifest_id_is_rejected(cleanup_db, tmp_path):
    database_path, target_ids, _preserved_id = cleanup_db

    with pytest.raises(CleanupError, match="unique"):
        apply_cleanup(
            database_path,
            tmp_path / "backup.db",
            [target_ids[0], target_ids[0]],
            SMALL_EXPECTATION,
        )


def test_unknown_manifest_id_is_rejected(cleanup_db, tmp_path):
    database_path, target_ids, _preserved_id = cleanup_db

    with pytest.raises(CleanupError, match="missing"):
        apply_cleanup(
            database_path,
            tmp_path / "backup.db",
            [target_ids[0], "20990101_000000_ffffff"],
            SMALL_EXPECTATION,
        )


def test_manifest_id_with_unreviewed_session_identity_is_rejected(cleanup_db):
    database_path, target_ids, _preserved_id = cleanup_db
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE sessions SET title = ? WHERE id = ?",
            ("renamed real session", target_ids[0]),
        )

    with sqlite3.connect(database_path) as connection, pytest.raises(
        CleanupError, match="identity"
    ):
        inspect_database(connection, target_ids, SMALL_EXPECTATION)


def test_checkpoint_busy_is_reported_as_committed_incomplete_maintenance(monkeypatch):
    monkeypatch.setattr(cleanup_module, "_checkpoint_tuple", lambda path: (1, 4, 0))

    with pytest.raises(CheckpointBusyError) as raised:
        run_checkpoint(Path("already-committed.db"))

    assert raised.value.cleanup_committed is True
    assert raised.value.checkpoint == (1, 4, 0)


def test_cli_dry_run_rejects_missing_database_without_creating_it(tmp_path):
    missing_database = tmp_path / "missing.db"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(["one", "two"]), encoding="utf-8")

    exit_code = main(["--database", str(missing_database), "--manifest", str(manifest_path)])

    assert exit_code != 0
    assert not missing_database.exists()


def test_cli_dry_run_does_not_mutate_existing_fixture(cleanup_db, tmp_path, monkeypatch):
    database_path, target_ids, _preserved_id = cleanup_db
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(target_ids), encoding="utf-8")
    before = database_counts(database_path)
    monkeypatch.setattr(cleanup_module, "PRODUCTION_EXPECTATION", SMALL_EXPECTATION)
    opened_modes = []
    real_open = cleanup_module._open_existing_database

    def record_open(database, *, read_only=False):
        opened_modes.append(read_only)
        return real_open(database, read_only=read_only)

    monkeypatch.setattr(cleanup_module, "_open_existing_database", record_open)
    monkeypatch.setattr(
        cleanup_module,
        "_verify_fts_integrity",
        lambda connection: pytest.fail("dry run attempted write-style FTS integrity check"),
    )

    exit_code = main(["--database", str(database_path), "--manifest", str(manifest_path)])

    assert exit_code == 0
    assert opened_modes == [True]
    assert database_counts(database_path) == before


def test_inspection_rejects_stale_external_fts(cleanup_db):
    database_path, target_ids, preserved_id = cleanup_db
    with sqlite3.connect(database_path) as connection:
        message_id, content = connection.execute(
            "SELECT id, content FROM messages WHERE session_id = ?", (preserved_id,)
        ).fetchone()
        connection.execute(
            "INSERT INTO messages_fts(messages_fts, rowid, content) "
            "VALUES ('delete', ?, ?)",
            (message_id, content),
        )
        connection.execute(
            "INSERT INTO messages_fts(rowid, content) VALUES (?, ?)",
            (message_id, "stale external index content"),
        )

    with sqlite3.connect(database_path) as connection, pytest.raises(
        CleanupError, match="FTS integrity"
    ) as raised:
        inspect_database(connection, target_ids, SMALL_EXPECTATION)

    assert "database disk image is malformed" in str(raised.value)


def test_apply_binds_backup_to_the_locked_deletion_snapshot(cleanup_db, tmp_path, monkeypatch):
    database_path, target_ids, preserved_id = cleanup_db
    backup_path = tmp_path / "backup.db"
    original_backup = cleanup_module.backup_database

    def update_preserved_after_backup(*args, **kwargs):
        original_backup(*args, **kwargs)
        with sqlite3.connect(database_path, timeout=0) as writer:
            writer.execute("UPDATE sessions SET title = ? WHERE id = ?", ("preserved-after", preserved_id))

    monkeypatch.setattr(cleanup_module, "backup_database", update_preserved_after_backup)

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        apply_cleanup(database_path, backup_path, target_ids, SMALL_EXPECTATION)

    with sqlite3.connect(database_path) as live:
        assert live.execute("SELECT title FROM sessions WHERE id = ?", (preserved_id,)).fetchone()[0] == "preserved"


def test_cli_apply_rejects_unmanifested_backend_rows(cleanup_db, tmp_path, monkeypatch):
    database_path, target_ids, _preserved_id = cleanup_db
    manifest_path = tmp_path / "manifest.json"
    backup_path = tmp_path / "backup.db"
    manifest_path.write_text(json.dumps(target_ids), encoding="utf-8")
    recall = SessionRecall(database_path)
    recall.init_db()
    extra_key = "backend_question_" + "3" * 32
    recall.get_or_create_session(extra_key, title=extra_key)
    recall.close()
    monkeypatch.setattr(cleanup_module, "PRODUCTION_EXPECTATION", SMALL_EXPECTATION)

    exit_code = main([
        "--database", str(database_path), "--manifest", str(manifest_path),
        "--backup", str(backup_path), "--apply",
    ])

    assert exit_code != 0


def test_cli_apply_rejects_post_commit_concurrent_write(cleanup_db, tmp_path, monkeypatch):
    database_path, target_ids, preserved_id = cleanup_db
    manifest_path = tmp_path / "manifest.json"
    backup_path = tmp_path / "backup.db"
    manifest_path.write_text(json.dumps(target_ids), encoding="utf-8")
    monkeypatch.setattr(cleanup_module, "PRODUCTION_EXPECTATION", SMALL_EXPECTATION)
    original_checkpoint = cleanup_module.run_checkpoint

    def checkpoint_then_write(path):
        result = original_checkpoint(path)
        with sqlite3.connect(database_path) as writer:
            writer.execute("UPDATE sessions SET title = ? WHERE id = ?", ("post-checkpoint", preserved_id))
        return result

    monkeypatch.setattr(cleanup_module, "run_checkpoint", checkpoint_then_write)

    exit_code = main([
        "--database", str(database_path), "--manifest", str(manifest_path),
        "--backup", str(backup_path), "--apply",
    ])

    assert exit_code != 0


def test_cli_apply_rejects_post_commit_stale_external_fts(
    cleanup_db, tmp_path, monkeypatch
):
    database_path, target_ids, preserved_id = cleanup_db
    manifest_path = tmp_path / "manifest.json"
    backup_path = tmp_path / "backup.db"
    manifest_path.write_text(json.dumps(target_ids), encoding="utf-8")
    monkeypatch.setattr(cleanup_module, "PRODUCTION_EXPECTATION", SMALL_EXPECTATION)
    original_checkpoint = cleanup_module.run_checkpoint

    def checkpoint_then_corrupt_fts(path):
        result = original_checkpoint(path)
        with sqlite3.connect(database_path) as writer:
            message_id, content = writer.execute(
                "SELECT id, content FROM messages WHERE session_id = ?", (preserved_id,)
            ).fetchone()
            writer.execute(
                "INSERT INTO messages_fts(messages_fts, rowid, content) "
                "VALUES ('delete', ?, ?)",
                (message_id, content),
            )
            writer.execute(
                "INSERT INTO messages_fts(rowid, content) VALUES (?, ?)",
                (message_id, "stale post-commit index content"),
            )
        return result

    monkeypatch.setattr(cleanup_module, "run_checkpoint", checkpoint_then_corrupt_fts)

    exit_code = main([
        "--database", str(database_path), "--manifest", str(manifest_path),
        "--backup", str(backup_path), "--apply",
    ])

    assert exit_code == 3


def test_cli_reports_nonbusy_checkpoint_failure_as_committed_incomplete(
    cleanup_db, tmp_path, monkeypatch, capsys
):
    database_path, target_ids, _preserved_id = cleanup_db
    manifest_path = tmp_path / "manifest.json"
    backup_path = tmp_path / "backup.db"
    manifest_path.write_text(json.dumps(target_ids), encoding="utf-8")
    monkeypatch.setattr(cleanup_module, "PRODUCTION_EXPECTATION", SMALL_EXPECTATION)
    monkeypatch.setattr(
        cleanup_module, "run_checkpoint", lambda path: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O"))
    )

    exit_code = main([
        "--database", str(database_path), "--manifest", str(manifest_path),
        "--backup", str(backup_path), "--apply",
    ])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "cleanup_committed=true" in captured.err
    assert str(backup_path) in captured.err
