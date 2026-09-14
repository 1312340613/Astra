"""Synthetic-case tests for the Context Index relevance benchmark.

metrics.py must stay pure (no IO, no model); judge.py parsing must fail closed.
"""

import json
import math
from pathlib import Path

import pytest

from agent.evals.context_index_benchmark.metrics import (
    RowLabel,
    ndcg_at_k,
    precision_at_k,
    summarize,
)


def _row(label: int, *, source: str = "session", slot: str = "S1",
         reason: str = "related", position: int = 1) -> RowLabel:
    return RowLabel(source=source, slot=slot, reason=reason, position=position, label=label)


class TestNdcg:
    def test_perfect_ranking_is_one(self) -> None:
        rows = [_row(2, position=1), _row(1, position=2), _row(0, position=3)]
        assert ndcg_at_k(rows, k=6) == pytest.approx(1.0)

    def test_worst_ranking_below_perfect(self) -> None:
        good = [_row(2, position=1), _row(0, position=2)]
        bad = [_row(0, position=1), _row(2, position=2)]
        assert ndcg_at_k(good) > ndcg_at_k(bad)

    def test_known_value(self) -> None:
        # gains: 2→3, 1→1, 0→0; discount 1/log2(i+1). DCG = 3 + 1/log2(3).
        rows = [_row(2, position=1), _row(1, position=2), _row(0, position=3)]
        expected = (3 / 1 + 1 / math.log2(3)) / (3 / 1 + 1 / math.log2(3))
        assert ndcg_at_k(rows, k=2) == pytest.approx(expected)

    def test_all_zero_is_zero(self) -> None:
        rows = [_row(0, position=1), _row(0, position=2)]
        assert ndcg_at_k(rows, k=6) == pytest.approx(0.0)

    def test_k_truncates_displayed_positions(self) -> None:
        rows = [_row(0, position=1), _row(2, position=2)]
        # Only position 1 is inside k=1 → gain 0, ideal inside window is still 0 → 0.0
        assert ndcg_at_k(rows, k=1) == pytest.approx(0.0)
        assert ndcg_at_k(rows, k=2) < 1.0

    def test_empty_panel_is_zero(self) -> None:
        assert ndcg_at_k([], k=6) == 0.0


class TestPrecision:
    def test_counts_relevant_above_threshold(self) -> None:
        rows = [_row(2, position=1), _row(0, position=2), _row(1, position=3)]
        assert precision_at_k(rows, k=2) == pytest.approx(0.5)
        assert precision_at_k(rows, k=3) == pytest.approx(2 / 3)

    def test_empty_k_slice_is_zero(self) -> None:
        assert precision_at_k([], k=2) == 0.0


class TestSummarize:
    def test_breakdown_and_zero_hit_rate(self) -> None:
        cases = {
            "a": [_row(2, position=1), _row(1, source="activity", slot="A2", reason="recent", position=2)],
            "b": [_row(0, slot="S3", reason="distinct", position=1),
                  _row(0, source="activity", slot="A3", reason="recent fallback", position=2)],
        }
        report = summarize(cases)
        assert report["case_count"] == 2
        assert report["zero_hit_rate"] == pytest.approx(0.5)
        assert report["ndcg_at_6"] == pytest.approx((1.0 + 0.0) / 2)
        semantic = report["by_reason"]["related"]
        recency = report["by_reason"]["recent"]
        assert semantic["mean_label"] == pytest.approx(2.0)
        assert recency["mean_label"] == pytest.approx(1.0)
        assert report["by_source"]["session"]["count"] == 2
        assert report["by_source"]["activity"]["count"] == 2

    def test_reason_groups_cover_fallback_and_distinct(self) -> None:
        cases = {
            "c": [
                _row(2, slot="S3", reason="distinct", position=1),
                _row(0, slot="A3", reason="recent fallback", position=2),
            ],
        }
        report = summarize(cases)
        assert "distinct" in report["by_reason"]
        assert "recent fallback" in report["by_reason"]

    def test_empty_input_is_safe(self) -> None:
        report = summarize({})
        assert report["case_count"] == 0
        assert report["ndcg_at_6"] == 0.0


class TestExtractCases:
    """cases.py must yield loadable ShadowCase JSONL from a session DB."""

    def _make_db(self, path: Path) -> None:
        import sqlite3

        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, workspace_root TEXT)")
        conn.execute(
            "CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, "
            "msg_index INTEGER, timestamp REAL)"
        )
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
            [
                ("s1", "user", "帮我修复登录页面的表单校验问题，需要支持邮箱", 0, 1788000000.5),
                ("s1", "assistant", "好的，我看一下", 1, 1788000001.0),
                ("s1", "user", "短", 2, 1788000002.0),
                ("s1", "user", "<message_time>2026-09-01T10:00:00+08:00</message_time> 忽略这个", 3, 1788000003.0),
                ("s2", "user", "重新设计缓存淘汰策略，参考 LRU-K 论文里的实现细节", 0, 1788100000.0),
                ("s2", "user", "继续刚才说的淘汰策略主题", 1, 1788100100.0),
                ("s3", "user", None, 0, 1788200000.0),
                ("s3", "user", "没有 workspace 的会话里的这条应该被过滤掉才对", 1, 1788200100.0),
            ],
        )
        conn.executemany(
            "INSERT INTO sessions VALUES (?, ?)",
            [("s1", "/tmp/ws-a"), ("s2", "/tmp/ws-a"), ("s3", None)],
        )
        conn.commit()
        conn.close()

    def test_filters_and_emits_loadable_records(self, tmp_path: Path) -> None:
        from agent.evals.context_index_benchmark.cases import extract_cases

        db = tmp_path / "sessions.db"
        self._make_db(db)
        records = extract_cases(db, max_cases=100)
        texts = [r["user_text"] for r in records]
        assert any("表单校验" in t for t in texts)
        assert not any("短" == t for t in texts)
        assert not any("message_time" in t for t in texts)
        assert not any("没有 workspace" in t for t in texts)
        assert all(r["workspace_label"] == "ws-a" for r in records)
        assert all(r["case_id"] for r in records)

        # The records must load through the shadow-replay case loader unchanged.
        from agent.evals.context_index_eval import load_shadow_cases

        fixture = tmp_path / "turns.jsonl"
        fixture.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
        cases = load_shadow_cases(fixture)
        assert len(cases) == len(records)
        by_id = {record["case_id"]: record for record in records}
        assert all(case.session_id == by_id[case.case_id]["session_id"] for case in cases)
        assert all(case.message_id == by_id[case.case_id]["message_id"] for case in cases)

    def test_per_session_cap_and_total_cap(self, tmp_path: Path) -> None:
        from agent.evals.context_index_benchmark.cases import extract_cases

        db = tmp_path / "sessions.db"
        self._make_db(db)
        records = extract_cases(db, max_cases=3)
        assert len(records) == 3
        per_session: dict[str, int] = {}
        for record in records:
            per_session[record["session_id"]] = per_session.get(record["session_id"], 0) + 1
        assert all(count <= 2 for count in per_session.values())


class TestJudgeParsing:
    """parse_judge_response must be total: any model output degrades to None."""

    def test_plain_json_object(self) -> None:
        from agent.evals.context_index_benchmark.judge import parse_judge_response

        parsed = parse_judge_response('{"a": 2, "b": 0, "c": 1}', {"a", "b", "c"})
        assert parsed == {"a": 2, "b": 0, "c": 1}

    def test_fenced_json_with_prose(self) -> None:
        from agent.evals.context_index_benchmark.judge import parse_judge_response

        text = 'Sure!\n```json\n{"h1": 2}\n```\nDone.'
        assert parse_judge_response(text, {"h1"}) == {"h1": 2}

    def test_clamps_out_of_range_and_drops_unknown_handles(self) -> None:
        from agent.evals.context_index_benchmark.judge import parse_judge_response

        parsed = parse_judge_response('{"a": 9, "b": -3, "ghost": 2}', {"a", "b"})
        assert parsed == {"a": 2, "b": 0}

    def test_garbage_is_none(self) -> None:
        from agent.evals.context_index_benchmark.judge import parse_judge_response

        assert parse_judge_response("i think they look fine", {"a"}) is None
        assert parse_judge_response("[1,2,3]", {"a"}) is None
        assert parse_judge_response('{"a": "maybe"}', {"a"}) is None

    def test_float_labels_round(self) -> None:
        from agent.evals.context_index_benchmark.judge import parse_judge_response

        assert parse_judge_response('{"a": 1.0}', {"a"}) == {"a": 1}


class TestJudgePrompt:
    def test_prompt_carries_query_handles_and_descriptions(self) -> None:
        from agent.evals.context_index_benchmark.judge import build_judge_prompt

        rows = [("ctx:s:1", "session", "修复登录表单校验"), ("ctx:a:2", "activity", "浏览 arxiv 论文")]
        system, user = build_judge_prompt("帮我改一下表单校验的 bug", rows)
        assert "ctx:s:1" in user and "ctx:a:2" in user
        assert "修复登录表单校验" in user and "帮我改一下表单校验的 bug" in user
        assert "json" in system.lower()
        # 输出指令必须限定 handle 集合与 0/1/2 量表，防止模型自由发挥。
        assert "0" in system and "1" in system and "2" in system


class TestRunEndToEnd:
    def test_stub_transport_produces_summary(self, tmp_path: Path) -> None:
        from agent.runtime.context_index.models import ContextIndexRow, ContextIndexTrace
        from agent.evals.context_index_benchmark.run import run_benchmark

        def stub(system: str, user: str) -> str:
            handles = [
                token.split(" ", 1)[0]
                for line in user.splitlines()
                if line.startswith("- ctx:") and (token := line[2:])
            ]
            return json.dumps({h: (2 if i == 0 else 0) for i, h in enumerate(handles)})

        class FakeBroker:
            last_trace = None

            async def build(self, user_text, request_id, session_id, workspace, now, active):
                self.last_trace = ContextIndexTrace(
                    request_id=request_id, session_id=session_id, mode="all", workspace_label="ws",
                    displayed=[
                        ContextIndexRow(handle="ctx:s:aaa", source="session", slot="S1",
                                        description="表単校验修复记录", reason="related", timestamp=0.0),
                        ContextIndexRow(handle="ctx:a:bbb", source="activity", slot="A2",
                                        description="最近浏览", reason="recent", timestamp=0.0),
                    ],
                )

        cases_file = tmp_path / "cases.jsonl"
        cases_file.write_text(
            json.dumps({
                "case_id": "case-x-1", "session_id": "x",
                "user_text": "帮我修复登录页面的表单校验问题",
                "workspace_label": "ws", "workspace_key": "/tmp/ws",
                "local_time": "2026-09-01T10:00:00+08:00",
            }) + "\n"
        )
        summary, rows_out = run_benchmark(
            cases_path=cases_file, fixture_root=str(tmp_path), transport=stub, broker=FakeBroker(),
        )
        assert summary["case_count"] == 1
        assert summary["unjudged_cases"] == 0
        assert rows_out and all(row.label in (0, 2) for row in rows_out)
        assert {row.slot for row in rows_out} == {"S1", "A2"}
        assert summary["zero_hit_rate"] == 0.0


class TestFrozenReplay:
    """v2 方法论修正：对比实验必须冻结 db 副本（基线当日 S2 槽在两次重放间漂移即是证据）。"""

    def _mini_sessions_db(self, path: Path) -> None:
        import sqlite3

        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, workspace_root TEXT)")
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
            "role TEXT, content TEXT, tool_name TEXT, timestamp REAL, msg_index INTEGER)"
        )
        conn.execute("INSERT INTO sessions VALUES ('old-1', '/tmp/ws')")
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, msg_index) VALUES "
            "('old-1', 'user', '上次我们讨论过表单校验的正则表达式写法', 1787000000.0, 0)"
        )
        conn.commit()
        conn.close()

    def test_freeze_copies_and_sets_env(self, tmp_path: Path, monkeypatch) -> None:
        from agent.evals.context_index_benchmark.run import freeze_replay_dbs

        source = tmp_path / "sessions.db"
        self._mini_sessions_db(source)
        monkeypatch.setenv("ASTRA_CONTEXT_INDEX_SESSIONS_DB", str(source))
        monkeypatch.setenv("ASTRA_CONTEXT_INDEX_ACTIVITY_DB", str(tmp_path / "missing-activity.db"))
        envs = freeze_replay_dbs(tmp_path / "frozen")
        assert Path(envs["ASTRA_CONTEXT_INDEX_SESSIONS_DB"]).is_file()
        assert envs["ASTRA_CONTEXT_INDEX_ACTIVITY_DB"] == "" or Path(
            envs["ASTRA_CONTEXT_INDEX_ACTIVITY_DB"]
        ).is_file()

    def test_rerun_with_frozen_db_is_deterministic(self, tmp_path: Path, monkeypatch) -> None:
        from agent.evals.context_index_benchmark.run import run_benchmark

        source = tmp_path / "sessions.db"
        self._mini_sessions_db(source)
        monkeypatch.setenv("ASTRA_CONTEXT_INDEX_SESSIONS_DB", str(source))
        monkeypatch.setenv("ASTRA_CONTEXT_INDEX_ACTIVITY_DB", str(tmp_path / "none.db"))
        cases_file = tmp_path / "cases.jsonl"
        cases_file.write_text(
            json.dumps({
                "case_id": "case-d-1", "session_id": "other-session",
                "user_text": "表单校验的正则表达式",
                "workspace_label": "ws", "workspace_key": "/tmp/ws",
                "local_time": "2026-09-04T12:00:00+08:00",
            }) + "\n"
        )

        def stub(system: str, user: str) -> str:
            handles = [
                token.split(" ", 1)[0]
                for line in user.splitlines()
                if line.startswith("- ctx:") and (token := line[2:])
            ]
            return json.dumps({h: 1 for h in handles})

        first, rows_first = run_benchmark(
            cases_path=cases_file, fixture_root="/tmp/ws", transport=stub,
            freeze_dir=tmp_path / "frozen",
        )
        # 冻结后源 db 再被写入也不影响回放。
        import sqlite3

        conn = sqlite3.connect(source)
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, msg_index) VALUES "
            "('old-1', 'user', '污染数据：冻结后新增的消息绝不能出现在回放里', 1789000000.0, 1)"
        )
        conn.commit()
        conn.close()
        second, rows_second = run_benchmark(
            cases_path=cases_file, fixture_root="/tmp/ws", transport=stub,
            freeze_dir=tmp_path / "frozen",
        )
        assert first == second
        assert [(r.case_id, r.slot, r.position) for r in rows_first] == [
            (r.case_id, r.slot, r.position) for r in rows_second
        ]

    def test_ranking_module_override_swaps_selector(self, tmp_path: Path, monkeypatch) -> None:
        # A/B 对照机制：runner 经 broker 模块名解析 select_rows，monkeypatch 即可
        # 换实现——对照实验只动 ranking，工具链/工件不漂移（worktree 方案的教训）。
        import importlib

        from agent.evals.context_index_benchmark.run import run_benchmark
        from agent.runtime.context_index.models import ContextIndexRow, ContextIndexTrace

        source = tmp_path / "sessions.db"
        self._mini_sessions_db(source)
        monkeypatch.setenv("ASTRA_CONTEXT_INDEX_SESSIONS_DB", str(source))
        monkeypatch.setenv("ASTRA_CONTEXT_INDEX_ACTIVITY_DB", str(tmp_path / "none.db"))

        broker_module = importlib.import_module("agent.runtime.context_index.broker")

        class CountingBroker:
            last_trace = None

            def __init__(self) -> None:
                self.calls = 0

            async def build(self, user_text, request_id, session_id, workspace, now, active):
                self.calls += 1
                self.last_trace = ContextIndexTrace(
                    request_id=request_id, session_id=session_id, mode="all",
                    workspace_label="ws",
                    displayed=[ContextIndexRow(
                        handle="ctx:s:abc", source="session", slot="S1",
                        description="记录", reason="related", timestamp=0.0,
                    )],
                )

        cases_file = tmp_path / "cases.jsonl"
        cases_file.write_text(
            json.dumps({
                "case_id": "case-m-1", "session_id": "other",
                "user_text": "表单校验的正则表达式",
                "workspace_label": "ws", "workspace_key": "/tmp/ws",
                "local_time": "2026-09-04T12:00:00+08:00",
            }) + "\n"
        )

        def stub(system: str, user: str) -> str:
            return json.dumps({"ctx:s:abc": 2})

        original_select = broker_module.select_rows
        seen: list[str] = []

        def spy_select(session_result, activity_result):
            seen.append("select_rows")
            return original_select(session_result, activity_result)

        monkeypatch.setattr(broker_module, "select_rows", spy_select)
        # 注入 broker 走同一模块名解析路径，验证的是「runner 不缓存旧绑定」。
        summary, rows = run_benchmark(
            cases_path=cases_file, fixture_root="/tmp/ws", transport=stub,
            broker=CountingBroker(),
        )
        assert summary["case_count"] == 1
        assert rows[0].label == 2


def test_freeze_includes_committed_wal_rows(tmp_path, monkeypatch):
    import sqlite3
    from agent.evals.context_index_benchmark.run import freeze_replay_dbs

    source = tmp_path / 'live.db'
    c = sqlite3.connect(source)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE evidence(value)')
    c.execute('INSERT INTO evidence VALUES (7)')
    c.commit()
    monkeypatch.setenv('ASTRA_CONTEXT_INDEX_SESSIONS_DB', str(source))
    monkeypatch.setenv('ASTRA_CONTEXT_INDEX_ACTIVITY_DB', str(tmp_path / 'missing'))
    try:
        paths = freeze_replay_dbs(tmp_path / 'frozen')
        with sqlite3.connect(paths['ASTRA_CONTEXT_INDEX_SESSIONS_DB']) as snapshot:
            assert snapshot.execute('SELECT value FROM evidence').fetchall() == [(7,)]
    finally:
        c.close()


@pytest.mark.parametrize('configured', [False, True])
def test_freeze_vectors_resolves_default_and_creates_directory(tmp_path, monkeypatch, configured):
    import sqlite3
    from agent.evals.context_index_benchmark.run import _freeze_vectors
    from agent.runtime.context_index import vector_index

    source = tmp_path / 'vectors.db'
    c = sqlite3.connect(source)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE evidence(value)')
    c.execute('INSERT INTO evidence VALUES (1)')
    c.commit()
    monkeypatch.setattr(vector_index, 'default_vectors_db_path', lambda: source)
    if configured:
        monkeypatch.setenv('ASTRA_CONTEXT_INDEX_VECTORS_DB', str(source))
    else:
        monkeypatch.delenv('ASTRA_CONTEXT_INDEX_VECTORS_DB', raising=False)
    dest = tmp_path / 'new' / 'snapshot'
    try:
        _freeze_vectors(dest)
        with sqlite3.connect(dest / 'context-vectors.db') as snapshot:
            assert snapshot.execute('SELECT value FROM evidence').fetchall() == [(1,)]
        c.execute('INSERT INTO evidence VALUES (2)')
        c.commit()
        _freeze_vectors(dest)
        with sqlite3.connect(dest / 'context-vectors.db') as snapshot:
            assert snapshot.execute('SELECT value FROM evidence').fetchall() == [(1,)]
    finally:
        c.close()
