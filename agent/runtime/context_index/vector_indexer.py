"""Build/refresh the Context Index vector DB from activity summaries (stage 2, M2).

Encoding contract: summary content is truncated to SUMMARY_ENCODE_CHARS before
embedding — identical to the stage-1 recall evaluation, so measured quality
transfers 1:1 to production. Rebuild is idempotent and incremental by content
hash (only new/changed summaries are re-encoded).
"""
from __future__ import annotations

from agent.runtime.paths import state_path

import argparse
import hashlib
import sqlite3
import time
from pathlib import Path

from . import embedder as _embedder
from .vector_index import VectorStore, default_vectors_db_path

SUMMARY_ENCODE_CHARS = 900  # 与阶段 1 召回评测口径一致（embed_recall_eval.py）
_TRUNCATION_MARK = "\n…[truncated]"


def _iter_summaries(activity_db: Path):
    connection = sqlite3.connect(f"file:{activity_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield from connection.execute(
            "SELECT summary_id, content, content_hash FROM activity_summaries "
            "WHERE TRIM(content) != '' ORDER BY summary_id"
        ).fetchall()
    finally:
        connection.close()


def rebuild(activity_db: Path, vectors_db: Path, *, force: bool) -> int:
    embedder = _embedder.get_embedder()
    if embedder is None:
        raise SystemExit(
            f"embedding backend unavailable ({_embedder.ENV_SWITCH} off or load failed)"
        )
    store = VectorStore(vectors_db)
    try:
        existing = {} if force else store.hashes()
        todo = [
            row
            for row in _iter_summaries(activity_db)
            if row["summary_id"] not in existing
            or existing[row["summary_id"]] != row["content_hash"]
        ]
        if not todo:
            return 0
        batch, batch_rows = [], []
        encoded_count = 0

        def flush() -> None:
            nonlocal batch, batch_rows, encoded_count
            vectors = embedder.encode(batch)
            if vectors is None:
                raise SystemExit("encode failed mid-build; store left consistent (upsert is atomic per row)")
            for row, vector in zip(batch_rows, vectors):
                store.upsert(row["summary_id"], row["content_hash"], list(vector))
            encoded_count += len(batch_rows)
            batch, batch_rows = [], []

        for row in todo:
            head = row["content"][:SUMMARY_ENCODE_CHARS]
            if len(row["content"]) > SUMMARY_ENCODE_CHARS:
                head += _TRUNCATION_MARK
            batch.append(f"title: {row['summary_id']}\n{head}")
            batch_rows.append(row)
            if len(batch) >= 16:
                flush()
        if batch:
            flush()
        return encoded_count
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    from agent.cli.environment import load_project_env

    load_project_env(Path(__file__).resolve().parents[3])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--activity-db",
        type=Path,
        default=state_path("activity-history.sqlite3"),
    )
    parser.add_argument("--vectors-db", type=Path, default=None)
    parser.add_argument("--rebuild", action="store_true", help="re-encode everything (ignore hashes)")
    args = parser.parse_args(argv)
    if not args.activity_db.is_file():
        parser.error("No activity history database; import activity summaries before building vectors")
    vectors_db = args.vectors_db or default_vectors_db_path()
    vectors_db.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    count = rebuild(args.activity_db, vectors_db, force=args.rebuild)
    print(
        f"encoded {count} summaries -> {vectors_db} in {time.perf_counter() - t0:.1f}s "
        f"(sha1:{hashlib.sha1(str(vectors_db).encode()).hexdigest()[:8]})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
