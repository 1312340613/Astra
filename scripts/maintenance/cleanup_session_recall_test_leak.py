"""Guarded cleanup for a known Session Recall test-data leak.

The command defaults to inspection.  It only changes a database when ``--apply``
is supplied, and then retains a verified SQLite backup before deleting the
explicit manifest rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SnapshotExpectation:
    target_session_count: int
    target_message_count: int


PRODUCTION_EXPECTATION = SnapshotExpectation(201, 458)


@dataclass(frozen=True)
class Inspection:
    session_count: int
    message_count: int
    fts_count: int
    target_session_count: int
    target_message_count: int


@dataclass(frozen=True)
class CleanupResult:
    deleted_sessions: int
    deleted_messages: int
    backup_path: Path
    pre_cleanup: Inspection
    post_cleanup: Inspection
    post_fingerprint: str


@dataclass(frozen=True)
class CheckpointResult:
    busy: int
    log_frames: int
    checkpointed: int


class CleanupError(RuntimeError):
    """The database did not meet a prerequisite for safe cleanup."""


class CheckpointBusyError(CleanupError):
    """Cleanup committed, but WAL truncation could not finish."""

    def __init__(self, checkpoint: tuple[int, int, int]):
        super().__init__(f"checkpoint remained busy: {checkpoint}")
        self.cleanup_committed = True
        self.checkpoint = checkpoint


_REVIEWED_SESSION_KEYS = (
    re.compile(r"backend_question_[0-9a-f]{32}"),
    re.compile(r"backend_question_plan_[0-9a-f]{32}"),
    re.compile(r"backend_question_approval_[0-9a-f]{32}"),
    re.compile(r"backend_question_cancel_[0-9a-f]{32}"),
    re.compile(r"backend_approval_[0-9a-f]{32}"),
    re.compile(r"backend_approval_cancel_[0-9a-f]{32}"),
)
_REVIEWED_FIXED_SESSION_KEYS = {
    "backend_goal_resume_test",
    "backend_cancel_test",
}


def _count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _state_fingerprint(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for table, columns in (
        ("sessions", "id, title, started_at, ended_at, message_count, personality, source_session_key, workspace_key, workspace_root"),
        ("messages", "id, session_id, role, content, tool_name, timestamp, msg_index"),
    ):
        digest.update(table.encode())
        for row in connection.execute(f"SELECT {columns} FROM {table} ORDER BY id"):
            digest.update(repr(tuple(row)).encode())
    return digest.hexdigest()


def _open_existing_database(
    database_path: Path, *, read_only: bool = False
) -> sqlite3.Connection:
    path = Path(database_path)
    if not path.is_file():
        raise CleanupError(f"database must be an existing regular file: {path}")
    try:
        mode = "ro" if read_only else "rw"
        return sqlite3.connect(f"{path.resolve().as_uri()}?mode={mode}", uri=True)
    except sqlite3.Error as exc:
        raise CleanupError(f"could not open database: {exc}") from exc


def _manifest_ids(target_ids: Sequence[str]) -> tuple[str, ...]:
    ids = tuple(target_ids)
    if any(not isinstance(target_id, str) or not target_id.strip() for target_id in ids):
        raise CleanupError("manifest ids must be non-empty strings")
    if len(set(ids)) != len(ids):
        raise CleanupError("manifest ids must be unique")
    return ids


def _is_reviewed_session_key(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return value in _REVIEWED_FIXED_SESSION_KEYS or any(
        pattern.fullmatch(value) for pattern in _REVIEWED_SESSION_KEYS
    )


def _verify_fts_integrity(connection: sqlite3.Connection) -> None:
    savepoint_started = False
    try:
        connection.execute("SAVEPOINT cleanup_fts_integrity")
        savepoint_started = True
        connection.execute(
            "INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)"
        )
    except sqlite3.DatabaseError as exc:
        raise CleanupError(f"FTS integrity check failed: {exc}") from exc
    finally:
        if savepoint_started:
            connection.execute("ROLLBACK TO cleanup_fts_integrity")
            connection.execute("RELEASE cleanup_fts_integrity")


def inspect_database(
    connection: sqlite3.Connection,
    target_ids: Sequence[str],
    expectation: SnapshotExpectation,
    *,
    verify_fts_integrity: bool = True,
) -> Inspection:
    """Validate the captured snapshot without modifying canonical rows."""
    ids = _manifest_ids(target_ids)
    if len(ids) != expectation.target_session_count:
        raise CleanupError(
            "snapshot target-session count does not match the manifest: "
            f"expected {expectation.target_session_count}, got {len(ids)}"
        )

    session_count = _count(connection, "sessions")
    message_count = _count(connection, "messages")
    fts_count = _count(connection, "messages_fts")
    if message_count != fts_count:
        raise CleanupError(
            f"snapshot message/FTS count mismatch: messages={message_count}, fts={fts_count}"
        )

    placeholders = ", ".join("?" for _ in ids)
    target_session_count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM sessions WHERE id IN ({placeholders})", ids
        ).fetchone()[0]
    )
    if target_session_count != len(ids):
        raise CleanupError("manifest contains missing session ids")
    target_sessions = connection.execute(
        f"SELECT source_session_key, title FROM sessions WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    if any(
        not _is_reviewed_session_key(source_key) or title != source_key
        for source_key, title in target_sessions
    ):
        raise CleanupError("manifest session identity is not an approved backend test fixture")
    target_message_count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM messages WHERE session_id IN ({placeholders})", ids
        ).fetchone()[0]
    )
    if target_message_count != expectation.target_message_count:
        raise CleanupError(
            "snapshot target-message count mismatch: "
            f"expected {expectation.target_message_count}, got {target_message_count}"
        )
    if verify_fts_integrity:
        _verify_fts_integrity(connection)
    return Inspection(
        session_count=session_count,
        message_count=message_count,
        fts_count=fts_count,
        target_session_count=target_session_count,
        target_message_count=target_message_count,
    )


def backup_database(
    source_path: Path,
    backup_path: Path,
    target_ids: Sequence[str],
    expectation: SnapshotExpectation,
    *,
    verify_source_fts_integrity: bool = True,
) -> None:
    """Create and verify a new backup, deleting only a backup we just found incomplete."""
    source_path = Path(source_path)
    backup_path = Path(backup_path)
    if source_path.resolve() == backup_path.resolve():
        raise CleanupError("backup path must differ from the source database")
    if backup_path.exists():
        raise CleanupError(f"backup path already exists: {backup_path}")

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_owned = False
    backup_validated = False
    try:
        with closing(_open_existing_database(source_path)) as source, source:
            before_backup = inspect_database(
                source,
                target_ids,
                expectation,
                verify_fts_integrity=verify_source_fts_integrity,
            )
            try:
                backup_path.touch(exist_ok=False)
            except FileExistsError as exc:
                raise CleanupError(f"backup path already exists: {backup_path}") from exc
            backup_owned = True
            with closing(sqlite3.connect(backup_path)) as destination, destination:
                source.backup(destination)
        with closing(sqlite3.connect(backup_path)) as backup, backup:
            copied = inspect_database(backup, target_ids, expectation)
        if copied != before_backup:
            raise CleanupError("backup snapshot does not match the inspected source")
        backup_validated = True
    except BaseException:
        if backup_owned and not backup_validated and backup_path.exists():
            backup_path.unlink()
        raise


def _post_cleanup_inspection(connection: sqlite3.Connection) -> Inspection:
    session_count = _count(connection, "sessions")
    message_count = _count(connection, "messages")
    fts_count = _count(connection, "messages_fts")
    remaining_targets = int(
        connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE id IN (SELECT id FROM cleanup_target_ids)"
        ).fetchone()[0]
    )
    remaining_target_messages = int(
        connection.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id IN (SELECT id FROM cleanup_target_ids)"
        ).fetchone()[0]
    )
    if remaining_targets or remaining_target_messages:
        raise CleanupError("target rows remain after deletion")
    if message_count != fts_count:
        raise CleanupError("post-cleanup messages and FTS rows differ")
    _verify_fts_integrity(connection)
    return Inspection(
        session_count=session_count,
        message_count=message_count,
        fts_count=fts_count,
        target_session_count=remaining_targets,
        target_message_count=remaining_target_messages,
    )


def apply_cleanup(
    database_path: Path,
    backup_path: Path,
    target_ids: Sequence[str],
    expectation: SnapshotExpectation,
) -> CleanupResult:
    """Back up and delete exactly the already-validated manifest rows."""
    database_path = Path(database_path)
    backup_path = Path(backup_path)
    ids = _manifest_ids(target_ids)
    connection = _open_existing_database(database_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            pre_cleanup = inspect_database(connection, ids, expectation)
            backup_database(
                database_path,
                backup_path,
                ids,
                expectation,
                verify_source_fts_integrity=False,
            )
            connection.execute("CREATE TEMP TABLE cleanup_target_ids(id TEXT PRIMARY KEY)")
            connection.executemany(
                "INSERT INTO cleanup_target_ids(id) VALUES (?)", ((target_id,) for target_id in ids)
            )
            before_messages = connection.total_changes
            connection.execute(
                "DELETE FROM messages WHERE session_id IN (SELECT id FROM cleanup_target_ids)"
            )
            message_change_delta = connection.total_changes - before_messages
            before_sessions = connection.total_changes
            connection.execute("DELETE FROM sessions WHERE id IN (SELECT id FROM cleanup_target_ids)")
            session_change_delta = connection.total_changes - before_sessions
            post_cleanup = _post_cleanup_inspection(connection)
            deleted_sessions = pre_cleanup.session_count - post_cleanup.session_count
            deleted_messages = pre_cleanup.message_count - post_cleanup.message_count
            if message_change_delta < deleted_messages:
                raise CleanupError("message deletion did not produce the expected change delta")
            if session_change_delta < deleted_sessions:
                raise CleanupError("session deletion did not produce the expected change delta")
            if deleted_sessions != pre_cleanup.target_session_count:
                raise CleanupError("deleted session total did not match the inspected manifest")
            if deleted_messages != pre_cleanup.target_message_count:
                raise CleanupError("deleted message total did not match the inspected manifest")
            if post_cleanup.session_count != pre_cleanup.session_count - deleted_sessions:
                raise CleanupError("post-cleanup session count was not conserved")
            if post_cleanup.message_count != pre_cleanup.message_count - deleted_messages:
                raise CleanupError("post-cleanup message count was not conserved")
            post_fingerprint = _state_fingerprint(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    finally:
        connection.close()
    return CleanupResult(
        deleted_sessions=deleted_sessions,
        deleted_messages=deleted_messages,
        backup_path=backup_path,
        pre_cleanup=pre_cleanup,
        post_cleanup=post_cleanup,
        post_fingerprint=post_fingerprint,
    )


def _checkpoint_tuple(database_path: Path) -> tuple[int, int, int]:
    with closing(_open_existing_database(database_path)) as connection, connection:
        row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return int(row[0]), int(row[1]), int(row[2])


def run_checkpoint(database_path: Path) -> CheckpointResult:
    """Try to truncate the WAL after a committed cleanup."""
    checkpoint = _checkpoint_tuple(database_path)
    result = CheckpointResult(*checkpoint)
    if result.busy:
        raise CheckpointBusyError(checkpoint)
    return result


def _read_manifest(manifest_path: Path) -> list[str]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CleanupError(f"could not read JSON manifest: {exc}") from exc
    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        raise CleanupError("manifest must be a JSON array of session id strings")
    return payload


def _print_inspection(inspection: Inspection, *, dry_run: bool) -> None:
    print(
        " ".join(
            (
                f"dry_run={str(dry_run).lower()}",
                f"sessions={inspection.session_count}",
                f"messages={inspection.message_count}",
                f"fts={inspection.fts_count}",
                f"target_sessions={inspection.target_session_count}",
                f"target_messages={inspection.target_message_count}",
            )
        )
    )


def _verify_persisted_cleanup(
    database_path: Path, target_ids: Sequence[str], result: CleanupResult
) -> None:
    ids = _manifest_ids(target_ids)
    placeholders = ", ".join("?" for _ in ids)
    with closing(_open_existing_database(database_path, read_only=True)) as connection, connection:
        session_count = _count(connection, "sessions")
        message_count = _count(connection, "messages")
        fts_count = _count(connection, "messages_fts")
        if (session_count, message_count) != (
            result.post_cleanup.session_count,
            result.post_cleanup.message_count,
        ):
            raise CleanupError("persisted cleanup counts changed after commit")
        if message_count != fts_count:
            raise CleanupError("persisted messages and FTS rows differ")
        if connection.execute(
            f"SELECT COUNT(*) FROM sessions WHERE id IN ({placeholders})", ids
        ).fetchone()[0]:
            raise CleanupError("persisted manifest sessions remain")
        if connection.execute(
            f"SELECT COUNT(*) FROM messages WHERE session_id IN ({placeholders})", ids
        ).fetchone()[0]:
            raise CleanupError("persisted manifest messages remain")
        if connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE id LIKE 'backend\\_%' ESCAPE '\\' "
            "OR source_session_key LIKE 'backend\\_%' ESCAPE '\\'"
        ).fetchone()[0]:
            raise CleanupError("persisted backend-prefixed session remains")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise CleanupError("persisted SQLite integrity check failed")
        if _state_fingerprint(connection) != result.post_fingerprint:
            raise CleanupError("persisted state changed after commit")
    # FTS5's external-content integrity command is a transactional write-style
    # operation, so run it on a short-lived RW connection after the read-only
    # persisted-state assertions.  The savepoint is rolled back by the helper.
    with closing(_open_existing_database(database_path)) as connection, connection:
        _verify_fts_integrity(connection)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--apply", action="store_true", help="back up and delete the manifest rows")
    args = parser.parse_args(argv)

    try:
        target_ids = _read_manifest(args.manifest)
        if not args.apply:
            with closing(_open_existing_database(args.database, read_only=True)) as connection, connection:
                _print_inspection(
                    inspect_database(
                        connection,
                        target_ids,
                        PRODUCTION_EXPECTATION,
                        verify_fts_integrity=False,
                    ),
                    dry_run=True,
                )
            return 0
        backup_path = args.backup or args.database.with_suffix(args.database.suffix + ".cleanup-backup")
        result = apply_cleanup(args.database, backup_path, target_ids, PRODUCTION_EXPECTATION)
        print(
            f"cleanup_committed=true backup_path={result.backup_path} "
            f"deleted_sessions={result.deleted_sessions} deleted_messages={result.deleted_messages}"
        )
        try:
            checkpoint = run_checkpoint(args.database)
        except CheckpointBusyError as exc:
            print(
                f"cleanup_committed=true backup_path={result.backup_path} "
                f"checkpoint_busy=true checkpoint={exc.checkpoint}",
                file=sys.stderr,
            )
            return 3
        except (CleanupError, OSError, sqlite3.Error) as exc:
            print(
                f"cleanup_committed=true backup_path={result.backup_path} "
                f"checkpoint_incomplete=true error={exc}",
                file=sys.stderr,
            )
            return 3
        try:
            _verify_persisted_cleanup(args.database, target_ids, result)
        except (CleanupError, OSError, sqlite3.Error) as exc:
            print(
                f"cleanup_committed=true backup_path={result.backup_path} "
                f"post_commit_verification_failed=true error={exc}",
                file=sys.stderr,
            )
            return 3
        print(f"checkpoint_busy=false checkpoint={(checkpoint.busy, checkpoint.log_frames, checkpoint.checkpointed)}")
        return 0
    except CleanupError as exc:
        print(f"cleanup_error={exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as exc:
        print(f"cleanup_error=database operation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
