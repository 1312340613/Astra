"""Gold-labelled replay of synthetic memory cases through Astra's real broker.

Run without --semantic for the lexical baseline. --semantic explicitly uses the
configured embedding backend; model preparation is excluded from warm latency.
No model judges, private archives, or chat-completion requests are involved.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

from agent.runtime.session_recall import SessionRecall
from agent.runtime.activity_store import _redact_text
from agent.runtime.context_index import embedder
from agent.runtime.context_index.activity_source import ActivityRecommendationReader
from agent.runtime.context_index.broker import ContextIndexBroker
from agent.runtime.context_index.semantic_index import ENCODE_CHARS, rebuild_source
from agent.runtime.context_index.semantic_reader import SemanticReader, minimum_similarity
from agent.runtime.context_index.session_source import SessionRecommendationSource, _identity
from agent.runtime.context_index.workspace import WorkspaceIdentity
from agent.runtime.memory import MemoryStore
from .quality_metrics import QualityObservation, quality_metrics

FIXTURE = Path(__file__).with_name("data") / "quality-v1.json"
NOW = datetime(2026, 9, 9, 10, tzinfo=UTC)
KINDS = ("lexical", "paraphrase", "negative", "hard_negative")


class CachedEncoder:
    """Cache actual backend outputs; no labels participate in encoding."""
    def __init__(self, backend, texts):
        self.vectors = {}
        unique = list(dict.fromkeys(texts))
        for offset in range(0, len(unique), 4):
            batch = unique[offset:offset + 4]
            vectors = backend.encode(batch)
            if vectors is None or len(vectors) != len(batch):
                raise RuntimeError("Embedding preparation failed")
            self.vectors.update(zip(batch, vectors))

    def encode(self, texts):
        return [self.vectors[text] for text in texts]


def load_fixture(path: Path = FIXTURE) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or not value.get("scenarios"):
        raise ValueError("Unsupported quality fixture")
    ids = [row["id"] for row in value["scenarios"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate scenario ids")
    return value


def _archives(root: Path, scenario: dict, source: str) -> tuple[Path, Path, dict[str, str]]:
    sessions = root / "sessions.db"
    memory = root / "memory.db"
    recall = SessionRecall(sessions)
    recall.init_db()
    store = MemoryStore(memory, core_dir=root / "core")
    documents = [("target", scenario["content"]), *[
        (f"distractor-{i}", text) for i, text in enumerate(scenario["distractors"])
    ]]
    identities = {}
    try:
        for document_id, content in documents:
            if source == "session":
                session = recall.create_session("历史会话", workspace_key="quality")
                message_id = recall.log_message(session, "user", content)
                identities[_identity(session, message_id)] = document_id
            else:
                record = store.add_record(kind="observation", content=content)
                identities[record.record_id] = document_id
        with closing(sqlite3.connect(sessions)) as db, db:
            db.execute("UPDATE messages SET timestamp=?", (NOW.timestamp() - 60,))
        with closing(sqlite3.connect(memory)) as db, db:
            for column in ("created_at", "valid_from", "last_confirmed_at"):
                db.execute(f"UPDATE memory_records SET {column}=?", ("2026-09-09T09:00:00+00:00",))
    finally:
        recall.close()
    return sessions, memory, identities


def replay(scenarios: list[dict], *, backend=None, query_backend=None) -> list[QualityObservation]:
    observations = []
    original_instance = embedder._instance
    embedder._instance = query_backend or backend
    try:
        with tempfile.TemporaryDirectory(prefix="astra-memory-quality-") as temporary:
            for scenario in scenarios:
                for source in ("session", "memory"):
                    root = Path(temporary) / scenario["id"] / source
                    root.mkdir(parents=True)
                    sessions, memory, identities = _archives(root, scenario, source)
                    vectors = root / "vectors.db"
                    semantic = None
                    if backend is not None:
                        rebuild_source(sessions if source == "session" else memory, vectors, source, backend=backend)
                        semantic = SemanticReader(sessions, vectors)
                    broker = ContextIndexBroker(
                        "session", 900, SessionRecommendationSource(sessions),
                        ActivityRecommendationReader(root / "absent.db"), semantic_reader=semantic,
                    )
                    selected_by_slot = {}
                    selector = broker._select_rows

                    def observe(*args, _selector=selector, _slots=selected_by_slot, _ids=identities, **kwargs):
                        selected = _selector(*args, **kwargs)
                        _slots.clear()
                        _slots.update((item.slot, _ids.get(item.identity, "unknown")) for item in selected)
                        return selected

                    broker._select_rows = observe
                    for kind in KINDS:
                        case_id = f"{scenario['id']}/{source}/{kind}"
                        pack = asyncio.run(broker.build(
                            scenario[kind], case_id, "current", WorkspaceIdentity("quality", "", "quality"),
                            NOW, frozenset(), memory_path=memory if source == "memory" else None,
                        ))
                        trace = broker.last_trace
                        observations.append(QualityObservation(
                            case_id, scenario["split"], kind, frozenset() if "negative" in kind else frozenset({"target"}),
                            tuple(selected_by_slot[row.slot] for row in pack.rows),
                            trace.latency_ms if trace else 0.0, trace.rendered_tokens if trace else 0,
                            dict(trace.source_status) if trace else {}, dict(trace.semantic_status) if trace else {},
                        ))
    finally:
        embedder._instance = original_instance
    return observations


def report(rows: list[QualityObservation]) -> dict:
    return {"overall": quality_metrics(rows),
            "by_split": {split: quality_metrics([row for row in rows if row.split == split])
                         for split in ("development", "holdout")},
            "by_kind": {kind: quality_metrics([row for row in rows if row.kind == kind])
                        for kind in KINDS},
            "cases": [{**asdict(row), "expected": sorted(row.expected)} for row in rows]}


def main(argv: list[str] | None = None) -> int:
    from agent.cli.environment import load_project_env
    load_project_env(Path(__file__).resolve().parents[3])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--semantic", action="store_true", help="explicitly encode with the configured backend")
    parser.add_argument("--split", choices=("all", "development", "holdout"), default="all")
    parser.add_argument("--live-query-encoding", action="store_true", help="include real warm query encoding in measured latency")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    fixture = load_fixture(args.fixture)
    if args.split != "all":
        fixture["scenarios"] = [row for row in fixture["scenarios"] if row["split"] == args.split]
    result = {
        "schema_version": 1, "fixture_sha256": hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
        "independent_scenarios": len(fixture["scenarios"]), "provenance": fixture["provenance"],
        "lexical": report(replay(fixture["scenarios"])),
    }
    if args.semantic:
        backend = embedder.get_embedder()
        if backend is None:
            parser.error("Embedding backend unavailable; lexical results do not establish semantic quality")
        texts = []
        for scenario in fixture["scenarios"]:
            texts.extend(_redact_text(text)[:ENCODE_CHARS] for text in (scenario["content"], *scenario["distractors"]))
            texts.extend(scenario[kind] for kind in KINDS)
        cached = CachedEncoder(backend, texts)
        result["embedding"] = {"identity": embedder.embedding_identity(), "minimum_similarity": minimum_similarity(),
                               "query_encoding": "live warm backend" if args.live_query_encoding else
                               "precomputed actual backend outputs; latency excludes model encoding"}
        result["hybrid"] = report(replay(fixture["scenarios"], backend=cached,
                                       query_backend=backend if args.live_query_encoding else None))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value["overall"] for key, value in result.items() if key in {"lexical", "hybrid"}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
