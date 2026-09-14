#!/usr/bin/env python3
"""Merge persistent user state from ``.agent_system`` into ``.astra``.

The migration is intentionally idempotent:

* existing ``.astra`` configuration values win;
* database rows are inserted only when their stable key is absent;
* the large Session Recall database is copied with SQLite's backup API.

Ephemeral test runs, process files, caches, and handoff fixtures are not migrated.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEGACY_DIR = PROJECT_ROOT / ".agent_system"
ASTRA_DIR = PROJECT_ROOT / ".astra"


def _deep_merge(legacy: Any, current: Any) -> Any:
    """Merge mappings recursively while preserving current values."""
    if not isinstance(legacy, dict) or not isinstance(current, dict):
        return current
    result = dict(legacy)
    for key, value in current.items():
        if key in result:
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return payload if isinstance(payload, dict) else {}


def _merge_json(name: str) -> str:
    source = LEGACY_DIR / name
    target = ASTRA_DIR / name
    if not source.is_file():
        return f"{name}: no legacy file"
    merged = _deep_merge(_load_json(source), _load_json(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return f"{name}: merged"


def _merge_models() -> str:
    source = LEGACY_DIR / "models.yaml"
    target = ASTRA_DIR / "models.yaml"
    if not source.is_file():
        return "models.yaml: no legacy file"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return "models.yaml: copied"

    legacy = yaml.safe_load(source.read_text(encoding="utf-8-sig")) or {}
    current = yaml.safe_load(target.read_text(encoding="utf-8-sig")) or {}
    merged = _deep_merge(legacy, current)
    target.write_text(
        yaml.safe_dump(merged, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return "models.yaml: merged"


def _table_exists(conn: sqlite3.Connection, table: str, schema: str = "main") -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [
        str(row[1])
        for row in conn.execute(f'PRAGMA {schema}.table_info("{table}")')
    ]


def _merge_table(
    conn: sqlite3.Connection,
    table: str,
    *,
    omit: Iterable[str] = (),
    where: str = "",
) -> int:
    if not _table_exists(conn, table) or not _table_exists(conn, table, "legacy"):
        return 0
    omitted = set(omit)
    target_columns = _columns(conn, table)
    source_columns = set(_columns(conn, table, "legacy"))
    columns = [
        column
        for column in target_columns
        if column in source_columns and column not in omitted
    ]
    if not columns:
        return 0
    quoted = ", ".join(f'"{column}"' for column in columns)
    before = conn.total_changes
    conn.execute(
        f'INSERT OR IGNORE INTO "{table}" ({quoted}) '
        f'SELECT {quoted} FROM legacy."{table}"'
        + (f" WHERE {where}" if where else "")
    )
    return conn.total_changes - before


def _merge_database(
    name: str,
    tables: Iterable[tuple[str, tuple[str, ...], str]],
    *,
    drop_after_copy: Iterable[str] = (),
) -> str:
    source = LEGACY_DIR / name
    target = ASTRA_DIR / name
    if not source.is_file():
        return f"{name}: no legacy database"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        os.close(handle)
        temporary = Path(temporary_name)
        sidecars = [
            temporary.with_name(temporary.name + suffix)
            for suffix in ("-wal", "-shm", "-journal")
        ]
        try:
            with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(temporary)) as dst:
                src.backup(dst)
                journal_mode = dst.execute("PRAGMA journal_mode=DELETE").fetchone()
                if journal_mode is None or str(journal_mode[0]).lower() != "delete":
                    raise RuntimeError("Could not make copied SQLite database self-contained")
                dropped = []
                for table in drop_after_copy:
                    if _table_exists(dst, table):
                        dst.execute(f'DROP TABLE "{table}"')
                        dropped.append(table)
                dst.commit()
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
            for sidecar in sidecars:
                sidecar.unlink(missing_ok=True)
        suffix = f"; dropped {', '.join(dropped)}" if dropped else ""
        return f"{name}: copied with SQLite backup{suffix}"

    conn = sqlite3.connect(target, timeout=30)
    inserted: dict[str, int] = {}
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("ATTACH DATABASE ? AS legacy", (str(source),))
        conn.execute("BEGIN IMMEDIATE")
        for table, omit, where in tables:
            inserted[table] = _merge_table(
                conn,
                table,
                omit=omit,
                where=where,
            )
        conn.commit()
        conn.execute("DETACH DATABASE legacy")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    detail = ", ".join(f"{table} +{count}" for table, count in inserted.items())
    return f"{name}: merged ({detail})"


def _merge_session_recall() -> str:
    source = LEGACY_DIR / "sessions.db"
    target = ASTRA_DIR / "sessions.db"
    if not source.is_file():
        return "sessions.db: no legacy database"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
        return "sessions.db: copied with SQLite backup"

    conn = sqlite3.connect(target, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("ATTACH DATABASE ? AS legacy", (str(source),))
        conn.execute("BEGIN IMMEDIATE")
        sessions = _merge_table(conn, "sessions")
        before = conn.total_changes
        conn.execute(
            """
            INSERT INTO messages (
                session_id, role, content, tool_name, timestamp, msg_index
            )
            SELECT
                old.session_id, old.role, old.content, old.tool_name,
                old.timestamp, old.msg_index
            FROM legacy.messages AS old
            WHERE NOT EXISTS (
                SELECT 1
                FROM messages AS current
                WHERE current.session_id = old.session_id
                  AND current.msg_index = old.msg_index
            )
            """
        )
        messages = conn.total_changes - before
        conn.commit()
        if _table_exists(conn, "messages_fts"):
            conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            conn.commit()
        conn.execute("DETACH DATABASE legacy")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return f"sessions.db: merged (sessions +{sessions}, messages +{messages})"


def migrate() -> list[str]:
    ASTRA_DIR.mkdir(parents=True, exist_ok=True)
    results = [
        _merge_models(),
        _merge_json("settings.json"),
        _merge_json("filesystem.json"),
        _merge_json("tui-settings.json"),
        _merge_json("mcp.json"),
        _merge_database(
            "memory.db",
            (
                (
                    "memory_records",
                    (),
                    "source_session_id IS NULL OR source_session_id <> 'legacy-markdown'",
                ),
                ("working_memories", (), ""),
            ),
        ),
        _merge_database(
            "learning.db",
            (
                ("learning_proposals", (), ""),
                ("learning_settings", (), ""),
            ),
        ),
        _merge_database(
            "tasks.db",
            (
                ("task_runs", (), ""),
                ("steps", (), ""),
                ("task_events", ("id",), ""),
            ),
            drop_after_copy=("case_sessions", "cases"),
        ),
        _merge_database(
            "browser.db",
            (
                ("browser_sessions", (), ""),
                ("browser_tabs", (), ""),
            ),
        ),
        _merge_session_recall(),
    ]
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    if not LEGACY_DIR.is_dir():
        raise SystemExit(f"Legacy directory not found: {LEGACY_DIR}")
    for result in migrate():
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
