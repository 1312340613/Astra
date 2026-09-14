#!/usr/bin/env python3
"""
Sync Hermes state.db history into Astra sessions.db.

Safe to run repeatedly. Sessions and messages are reconciled by stable Hermes
identifiers, so appended or edited messages are synchronized without creating
duplicates. Messages no longer active in Hermes are retained in Astra as
history and placed after the current Hermes transcript.

Usage:
    python3 scripts/import_hermes_history.py            # incremental sync
    python3 scripts/import_hermes_history.py --full     # ignore last-sync marker, scan all
    python3 scripts/import_hermes_history.py --dry-run  # show what would be imported
    python3 scripts/import_hermes_history.py --source-db PATH --target-db PATH

Paths may also be set with HERMES_DB and ASTRA_SESSIONS_DB. Command-line
arguments take precedence over environment variables.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_HERMES_DB = Path.home() / ".hermes" / "state.db"
_DEFAULT_SESSIONS_DB = PROJECT_ROOT / ".astra" / "sessions.db"
HERMES_DB = _DEFAULT_HERMES_DB
SESSIONS_DB = _DEFAULT_SESSIONS_DB


def resolve_database_paths(source_arg: str = "", target_arg: str = "") -> tuple[Path, Path]:
    """Resolve CLI, environment, then portable default database paths."""
    source_value = source_arg or os.environ.get("HERMES_DB", "")
    target_value = target_arg or os.environ.get("ASTRA_SESSIONS_DB", "")
    source_default = Path.home() / ".hermes" / "state.db" if HERMES_DB == _DEFAULT_HERMES_DB else HERMES_DB
    target_default = PROJECT_ROOT / ".astra" / "sessions.db" if SESSIONS_DB == _DEFAULT_SESSIONS_DB else SESSIONS_DB
    source = Path(source_value).expanduser() if source_value else source_default
    target = Path(target_value).expanduser() if target_value else target_default
    return source, target

# Sources to skip (non-interactive internal sessions)
SKIP_SOURCES = {"subagent", "tool", "cron", "delegation"}

PERSONALITY_MAP = {
    "tui": "hermes-tui",
    "cli": "hermes-cli",
    "weixin": "weixin",
    "discord": "discord",
    "acp": "acp",
    "telegram": "telegram",
}


def _ensure_schema(dst: sqlite3.Connection) -> None:
    fts_exists = _table_exists(dst, "messages_fts")
    dst.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT DEFAULT '',
            started_at REAL NOT NULL,
            ended_at REAL,
            message_count INTEGER DEFAULT 0,
            personality TEXT DEFAULT '',
            source_session_key TEXT
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user','assistant','tool','system')),
            content TEXT DEFAULT '',
            tool_name TEXT DEFAULT '',
            timestamp REAL NOT NULL,
            msg_index INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session
            ON messages(session_id, msg_index);

        CREATE TABLE IF NOT EXISTS sync_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS hermes_sync_messages (
            source_session_id TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            target_message_id INTEGER NOT NULL UNIQUE,
            PRIMARY KEY (source_session_id, source_message_id)
        );
    """)
    session_columns = {
        str(row[1]) for row in dst.execute("PRAGMA table_info(sessions)")
    }
    if "source_session_key" not in session_columns:
        dst.execute("ALTER TABLE sessions ADD COLUMN source_session_key TEXT")
    dst.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_source_key "
        "ON sessions(source_session_key) WHERE source_session_key IS NOT NULL"
    )
    fts_stale = False
    if _table_exists(dst, "recall_meta"):
        row = dst.execute(
            "SELECT value FROM recall_meta WHERE key = 'fts_stale'"
        ).fetchone()
        fts_stale = row is not None and str(row[0]) == "1"

    if not fts_exists:
        dst.execute(
            "CREATE VIRTUAL TABLE messages_fts USING fts5("
            "content, content=messages, content_rowid=id, tokenize='trigram')"
        )

    if not fts_stale:
        dst.executescript("""
            CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content)
                VALUES('delete', old.id, old.content);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content)
                VALUES('delete', old.id, old.content);
                INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
            END;
        """)
        if not fts_exists:
            dst.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    dst.commit()


def _get_last_sync(dst: sqlite3.Connection) -> float:
    if not _table_exists(dst, "sync_meta"):
        return 0.0
    row = dst.execute(
        "SELECT value FROM sync_meta WHERE key = 'hermes_last_sync'"
    ).fetchone()
    return float(row[0]) if row else 0.0


def _set_last_sync(dst: sqlite3.Connection, ts: float) -> None:
    dst.execute(
        "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('hermes_last_sync', ?)",
        (str(ts),),
    )


def _table_exists(database: sqlite3.Connection, table: str) -> bool:
    row = database.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _validate_hermes_source(database: sqlite3.Connection) -> None:
    try:
        database.execute(
            "SELECT id, title, source, started_at, ended_at, message_count "
            "FROM sessions LIMIT 0"
        )
        if database.execute("SELECT 1 FROM sessions LIMIT 1").fetchone() is not None:
            database.execute(
                "SELECT id, session_id, role, content, tool_name, timestamp, active, compacted "
                "FROM messages LIMIT 0"
            )
    except sqlite3.Error as exc:
        raise sqlite3.OperationalError(
            f"Hermes source database has an incompatible schema: {exc}"
        ) from exc


def _read_database_generation(path: Path) -> tuple[bytes, bytes | None]:
    main_database = path.read_bytes()
    wal_path = Path(f"{path}-wal")
    try:
        wal_database = wal_path.read_bytes() if wal_path.is_file() else None
    except FileNotFoundError:
        wal_database = None
    return main_database, wal_database


def _read_stable_database_files(
    path: Path,
    *,
    attempts: int = 5,
) -> tuple[bytes, bytes | None]:
    previous = _read_database_generation(path)
    for _ in range(attempts):
        current = _read_database_generation(path)
        if current == previous:
            return current
        previous = current
    raise RuntimeError(
        "Astra sessions database changed continuously during dry-run; retry when writes are quieter"
    )


def _open_dry_run_snapshot(
    path: Path,
) -> tuple[sqlite3.Connection | None, tempfile.TemporaryDirectory[str] | None]:
    if not path.is_file():
        return None, None
    main_database, wal_database = _read_stable_database_files(path)
    snapshot_dir = tempfile.TemporaryDirectory(prefix="astra-hermes-dry-run-")
    snapshot_path = Path(snapshot_dir.name) / path.name
    snapshot_path.write_bytes(main_database)
    if wal_database is not None:
        Path(f"{snapshot_path}-wal").write_bytes(wal_database)
    database = sqlite3.connect(snapshot_path)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA query_only=ON")
    return database, snapshot_dir


def _session_exists(database: sqlite3.Connection | None, session_id: str) -> bool:
    if database is None or not _table_exists(database, "sessions"):
        return False
    return database.execute(
        "SELECT 1 FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone() is not None


def _table_columns(database: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(database, table):
        return set()
    return {str(row[1]) for row in database.execute(f'PRAGMA table_info("{table}")')}


def _source_session_key(session_id: str) -> str:
    return f"hermes:{session_id}"


def _target_session(
    database: sqlite3.Connection | None,
    source_session_id: str,
    personality: str,
) -> tuple[str, bool, bool]:
    """Return target id, whether it exists, and whether it is a legacy import."""
    preferred = _source_session_key(source_session_id)
    if database is None or not _table_exists(database, "sessions"):
        return preferred, False, False

    columns = _table_columns(database, "sessions")
    if "source_session_key" in columns:
        row = database.execute(
            "SELECT id FROM sessions WHERE source_session_key = ?",
            (preferred,),
        ).fetchone()
        if row is not None:
            return str(row[0]), True, False

    legacy = database.execute(
        "SELECT id, personality FROM sessions WHERE id = ?",
        (source_session_id,),
    ).fetchone()
    if legacy is not None and str(legacy[1] or "") == personality:
        return source_session_id, True, True

    candidate = preferred
    suffix = 2
    while _session_exists(database, candidate):
        candidate = f"{preferred}:{suffix}"
        suffix += 1
    return candidate, False, False


def _normalized_source_message(message: sqlite3.Row, index: int) -> dict[str, object]:
    role = str(message["role"] or "")
    if role == "session_meta":
        role = "system"
    if role not in {"user", "assistant", "tool", "system"}:
        role = "system"
    return {
        "source_message_id": str(message["id"]),
        "role": role,
        "content": str(message["content"] or ""),
        "tool_name": str(message["tool_name"] or ""),
        "timestamp": float(message["timestamp"] or 0.0),
        "msg_index": index,
    }


def _message_signature(message: sqlite3.Row | dict[str, object]) -> tuple[object, ...]:
    return (
        str(message["role"] or ""),
        str(message["content"] or ""),
        str(message["tool_name"] or ""),
        float(message["timestamp"] or 0.0),
    )


def _database_totals(database: sqlite3.Connection | None) -> tuple[int, int]:
    if database is None:
        return 0, 0
    session_count = (
        database.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        if _table_exists(database, "sessions")
        else 0
    )
    message_count = (
        database.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if _table_exists(database, "messages")
        else 0
    )
    return session_count, message_count


def sync(
    full: bool = False,
    dry_run: bool = False,
    *,
    source_db: str = "",
    target_db: str = "",
) -> None:
    src_path, dst_path = resolve_database_paths(source_db, target_db)

    if not src_path.exists():
        print(f"ERROR: Hermes state.db not found at {src_path}")
        sys.exit(1)

    print(f"Source: {src_path} ({src_path.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"Target: {dst_path}")

    src = sqlite3.connect(str(src_path))
    src.row_factory = sqlite3.Row
    _validate_hermes_source(src)
    snapshot_dir = None
    if dry_run:
        dst, snapshot_dir = _open_dry_run_snapshot(dst_path)
    else:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(str(dst_path))
        dst.row_factory = sqlite3.Row
        dst.execute("PRAGMA journal_mode=WAL")
        dst.execute("PRAGMA synchronous=NORMAL")
        dst.execute("PRAGMA foreign_keys=OFF")
        dst.execute("PRAGMA busy_timeout=10000")
        _ensure_schema(dst)
        dst.execute("BEGIN IMMEDIATE")

    # ── Determine scan mode ──────────────────────────────────
    last_sync = 0.0 if full or dst is None else _get_last_sync(dst)
    sync_start = time.time()

    if full:
        print("Full reconciliation: scanning all sessions and messages")
    elif last_sync > 0:
        print(
            "Incremental message sync: scanning all sessions for deltas "
            f"(last run {time.strftime('%Y-%m-%d %H:%M', time.localtime(last_sync))})"
        )
    else:
        print("Initial message sync: scanning all sessions")

    # ── Fetch candidate sessions ─────────────────────────────
    placeholders = ",".join("?" for _ in SKIP_SOURCES)
    src_sessions = src.execute(
        f"SELECT id, title, source, started_at, ended_at, message_count "
        f"FROM sessions WHERE source NOT IN ({placeholders}) "
        f"ORDER BY started_at ASC",
        tuple(SKIP_SOURCES),
    ).fetchall()

    if not src_sessions:
        print("Nothing new to sync.")
        src.close()
        if dst is not None:
            dst.close()
        if snapshot_dir is not None:
            snapshot_dir.cleanup()
        return

    print(f"Found {len(src_sessions)} candidate session(s)")

    # ── Import ───────────────────────────────────────────────
    imported_sessions = 0
    imported_msgs = 0
    updated_msgs = 0
    existing_sessions = 0

    for s_row in src_sessions:
        sid = s_row["id"]
        title = s_row["title"] or f"[{s_row['source']}] {sid[:20]}"
        source = s_row["source"]
        started = s_row["started_at"] or time.time()
        ended = s_row["ended_at"]
        personality = PERSONALITY_MAP.get(source, source)
        target_sid, session_exists, legacy_session = _target_session(dst, sid, personality)
        if session_exists:
            existing_sessions += 1

        all_msgs = src.execute(
            "SELECT id, session_id, role, content, tool_name, timestamp, active, compacted "
            "FROM messages WHERE session_id = ? "
            "ORDER BY id ASC",
            (sid,),
        ).fetchall()
        msgs = [
            message
            for message in all_msgs
            if int(message["active"] or 0) == 1
            or int(message["compacted"] or 0) == 1
        ]

        source_messages = [
            _normalized_source_message(message, index)
            for index, message in enumerate(msgs, 1)
        ]
        all_source_messages = [
            _normalized_source_message(message, index)
            for index, message in enumerate(all_msgs, 1)
        ]
        all_source_positions = {
            str(message["source_message_id"]): index
            for index, message in enumerate(all_source_messages)
        }

        existing_rows: list[sqlite3.Row] = []
        mappings: dict[str, int] = {}
        if session_exists and dst is not None:
            existing_rows = dst.execute(
                "SELECT id, role, content, tool_name, timestamp, msg_index "
                "FROM messages WHERE session_id = ? ORDER BY msg_index, id",
                (target_sid,),
            ).fetchall()
            if _table_exists(dst, "hermes_sync_messages"):
                mappings = {
                    str(row[0]): int(row[1])
                    for row in dst.execute(
                        "SELECT source_message_id, target_message_id "
                        "FROM hermes_sync_messages WHERE source_session_id = ?",
                        (sid,),
                    ).fetchall()
                }

        existing_by_id = {int(row["id"]): row for row in existing_rows}
        broken_mapping_ids = [
            source_message_id
            for source_message_id, target_message_id in mappings.items()
            if target_message_id not in existing_by_id
        ]
        for source_message_id in broken_mapping_ids:
            if not dry_run:
                assert dst is not None
                dst.execute(
                    "DELETE FROM hermes_sync_messages "
                    "WHERE source_session_id = ? AND source_message_id = ?",
                    (sid, source_message_id),
                )
            del mappings[source_message_id]

        claimed_target_ids = set(mappings.values())
        planned_new = 0
        planned_updates = 0

        # Old importer versions had no per-message mapping. Before reconciling
        # the live transcript, adopt every exact source/target pair—including
        # source messages that have since become inactive—so active filtering
        # cannot shift positional matching.
        if legacy_session and existing_rows:
            for source_message in all_source_messages:
                source_message_id = str(source_message["source_message_id"])
                if source_message_id in mappings:
                    continue
                candidate = next(
                    (
                        row
                        for row in existing_rows
                        if int(row["id"]) not in claimed_target_ids
                        and _message_signature(row) == _message_signature(source_message)
                    ),
                    None,
                )
                if candidate is None:
                    continue
                target_message_id = int(candidate["id"])
                mappings[source_message_id] = target_message_id
                claimed_target_ids.add(target_message_id)
                if not dry_run:
                    assert dst is not None
                    dst.execute(
                        "INSERT INTO hermes_sync_messages "
                        "(source_session_id, source_message_id, target_message_id) "
                        "VALUES (?, ?, ?)",
                        (sid, source_message_id, target_message_id),
                    )

        if not session_exists:
            imported_sessions += 1
            if not dry_run:
                assert dst is not None
                dst.execute(
                    "INSERT INTO sessions "
                    "(id, title, started_at, ended_at, message_count, personality, source_session_key) "
                    "VALUES (?, ?, ?, ?, 0, ?, ?)",
                    (target_sid, title, started, ended, personality, _source_session_key(sid)),
                )
                session_exists = True
        elif legacy_session and not dry_run:
            assert dst is not None
            dst.execute(
                "UPDATE sessions SET source_session_key = ? WHERE id = ?",
                (_source_session_key(sid), target_sid),
            )

        for source_message in source_messages:
            source_message_id = str(source_message["source_message_id"])
            target_message_id = mappings.get(source_message_id)
            target_message = existing_by_id.get(target_message_id) if target_message_id else None

            if target_message is None and existing_rows:
                position = (
                    all_source_positions[source_message_id]
                    if legacy_session
                    else int(source_message["msg_index"]) - 1
                )
                candidate = existing_rows[position] if position < len(existing_rows) else None
                if legacy_session and (
                    candidate is None
                    or int(candidate["id"]) in claimed_target_ids
                    or str(candidate["role"] or "") != str(source_message["role"])
                ):
                    candidate = next(
                        (
                            row
                            for row in existing_rows
                            if int(row["id"]) not in claimed_target_ids
                            and str(row["role"] or "") == str(source_message["role"])
                        ),
                        None,
                    )
                if candidate is not None and int(candidate["id"]) not in claimed_target_ids:
                    exact_match = _message_signature(candidate) == _message_signature(source_message)
                    compatible_legacy = (
                        legacy_session
                        and str(candidate["role"] or "") == str(source_message["role"])
                    )
                    if not exact_match and not compatible_legacy:
                        raise RuntimeError(
                            f"Existing target session {target_sid} does not match Hermes "
                            f"session {sid} at message {source_message['msg_index']}"
                        )
                    target_message = candidate
                    target_message_id = int(candidate["id"])
                    claimed_target_ids.add(target_message_id)
                    if not dry_run:
                        assert dst is not None
                        dst.execute(
                            "INSERT INTO hermes_sync_messages "
                            "(source_session_id, source_message_id, target_message_id) "
                            "VALUES (?, ?, ?)",
                            (sid, source_message_id, target_message_id),
                        )

            if target_message is None:
                planned_new += 1
                if not dry_run:
                    assert dst is not None
                    cursor = dst.execute(
                        "INSERT INTO messages "
                        "(session_id, role, content, tool_name, timestamp, msg_index) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            target_sid,
                            source_message["role"],
                            source_message["content"],
                            source_message["tool_name"],
                            source_message["timestamp"],
                            source_message["msg_index"],
                        ),
                    )
                    dst.execute(
                        "INSERT INTO hermes_sync_messages "
                        "(source_session_id, source_message_id, target_message_id) "
                        "VALUES (?, ?, ?)",
                        (sid, source_message_id, int(cursor.lastrowid)),
                    )
                continue

            desired = (
                source_message["role"],
                source_message["content"],
                source_message["tool_name"],
                source_message["timestamp"],
                source_message["msg_index"],
            )
            current = (
                target_message["role"],
                target_message["content"],
                target_message["tool_name"],
                target_message["timestamp"],
                target_message["msg_index"],
            )
            if current != desired:
                planned_updates += 1
                if not dry_run:
                    assert dst is not None and target_message_id is not None
                    dst.execute(
                        "UPDATE messages SET role = ?, content = ?, tool_name = ?, "
                        "timestamp = ?, msg_index = ? WHERE id = ?",
                        (*desired, target_message_id),
                    )

        # Preserve messages that Hermes no longer marks active/compacted. They may
        # be useful Astra history, but must not occupy indexes in the live source
        # transcript or cause duplicate msg_index values.
        active_source_ids = {
            str(message["source_message_id"]) for message in source_messages
        }
        retained_target_ids = [
            target_message_id
            for source_message_id, target_message_id in mappings.items()
            if source_message_id not in active_source_ids
            and target_message_id in existing_by_id
        ]
        retained_target_ids.sort(
            key=lambda message_id: (
                int(existing_by_id[message_id]["msg_index"]),
                message_id,
            )
        )
        for offset, target_message_id in enumerate(retained_target_ids, 1):
            retained_index = len(source_messages) + offset
            if int(existing_by_id[target_message_id]["msg_index"]) == retained_index:
                continue
            planned_updates += 1
            if not dry_run:
                assert dst is not None
                dst.execute(
                    "UPDATE messages SET msg_index = ? WHERE id = ?",
                    (retained_index, target_message_id),
                )

        imported_msgs += planned_new
        updated_msgs += planned_updates

        if not dry_run:
            assert dst is not None
            target_count = dst.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                (target_sid,),
            ).fetchone()[0]
            dst.execute(
                "UPDATE sessions SET title = ?, started_at = ?, ended_at = ?, "
                "message_count = ?, personality = ? WHERE id = ?",
                (title, started, ended, target_count, personality, target_sid),
            )

        if dry_run and (not session_exists or planned_new or planned_updates):
            action = "import" if not session_exists else "update"
            print(
                f"  [dry-run] would {action}: {sid[:16]}… ({title[:40]}) "
                f"+{planned_new} ~{planned_updates}"
            )

        if imported_sessions and imported_sessions % 100 == 0:
            print(f"  Progress: {imported_sessions} sessions, {imported_msgs} msgs")

    if not dry_run:
        assert dst is not None
        _set_last_sync(dst, sync_start)
        dst.commit()

    # ── Summary ──────────────────────────────────────────────
    final_sessions, final_msgs = _database_totals(dst)

    print(f"\n{'='*50}")
    if dry_run:
        print("Dry run — nothing written.")
    print(f"  New sessions: {imported_sessions} (skipped {existing_sessions} existing)")
    print(f"  New messages: {imported_msgs}")
    print(f"  Updated messages: {updated_msgs}")
    print(f"  DB total: {final_sessions} sessions, {final_msgs} msgs")

    src.close()
    if dst is not None:
        dst.close()
    if snapshot_dir is not None:
        snapshot_dir.cleanup()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Hermes history into Astra sessions.db")
    parser.add_argument("--full", action="store_true", help="Ignore last-sync marker, scan all sessions")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be imported without writing")
    parser.add_argument(
        "--source-db",
        default="",
        help="Hermes state.db path (default: HERMES_DB or ~/.hermes/state.db)",
    )
    parser.add_argument(
        "--target-db",
        default="",
        help="Astra sessions.db path (default: ASTRA_SESSIONS_DB or repository .astra/sessions.db)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sync(
        full=args.full,
        dry_run=args.dry_run,
        source_db=args.source_db,
        target_db=args.target_db,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
