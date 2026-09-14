"""M2 tests: vector_indexer incremental build + shared encoding contract."""
import sqlite3

import pytest

from agent.runtime.context_index import embedder as embedder_module
from agent.runtime.context_index import vector_indexer as ix


def _mini_activity_db(tmp_path):
    path = tmp_path / "activity.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE activity_summaries (
               summary_id TEXT PRIMARY KEY,
               content TEXT NOT NULL,
               content_hash TEXT NOT NULL
           )"""
    )
    connection.execute(
        "INSERT INTO activity_summaries VALUES ('s1', 'first summary', 'h1')"
    )
    connection.execute(
        "INSERT INTO activity_summaries VALUES ('s2', ?, 'h2')", ("x" * 1200,)
    )
    connection.commit()
    connection.close()
    return path


class RecordingEmbedder:
    def __init__(self):
        self.seen_texts: list[str] = []

    def encode(self, texts):
        self.seen_texts.extend(texts)
        return [(1.0, 0.0) for _ in texts]


@pytest.fixture()
def fake_embedder(monkeypatch):
    embedder = RecordingEmbedder()
    monkeypatch.setattr(embedder_module, "get_embedder", lambda: embedder)
    return embedder


def test_rebuild_is_incremental_by_content_hash(tmp_path, fake_embedder) -> None:
    activity_db = _mini_activity_db(tmp_path)
    vectors_db = tmp_path / "vectors.db"

    assert ix.rebuild(activity_db, vectors_db, force=False) == 2
    assert fake_embedder.seen_texts  # 首轮全量
    fake_embedder.seen_texts.clear()

    assert ix.rebuild(activity_db, vectors_db, force=False) == 0
    assert fake_embedder.seen_texts == []  # hash 未变→零重编码

    connection = sqlite3.connect(activity_db)
    connection.execute(
        "UPDATE activity_summaries SET content='edited', content_hash='h2b' WHERE summary_id='s2'"
    )
    connection.commit()
    connection.close()
    assert ix.rebuild(activity_db, vectors_db, force=False) == 1  # 只重编变更行


def test_encoding_contract_truncation_and_title_prefix(tmp_path, fake_embedder) -> None:
    activity_db = _mini_activity_db(tmp_path)
    ix.rebuild(activity_db, tmp_path / "v.db", force=True)
    short, long = fake_embedder.seen_texts[0], fake_embedder.seen_texts[1]
    assert short == "title: s1\nfirst summary"  # 与阶段 1 评测口径一致的前缀
    assert long.startswith("title: s2\n")
    assert long.endswith(ix._TRUNCATION_MARK)
    assert len(long) < 1200  # 900 字符截断生效
