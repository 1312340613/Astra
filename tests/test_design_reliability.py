"""Counterexamples from the reliability audit, with isolated execution/storage."""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.runtime.capabilities import build_runtime_capabilities
from agent.runtime.context_index.lexical import LexicalQuery
from agent.runtime.context_index.query import plan_query
from agent.runtime.context_index.record_source import _revision
from agent.runtime.context_index.semantic_reader import SemanticReader
from agent.runtime.context_index.workspace import WorkspaceIdentity
from agent.runtime.goal_verifier import build_transcript, parse_verdict, verdict_is_usable
from agent.runtime.memory import MemoryStore
from agent.runtime.memory_consolidation import MemoryConsolidator
from agent.runtime.memory_provider import BuiltinMemoryProvider
from agent.runtime.memory_retainer import MemoryRetainer, ToolEvidence
from agent.runtime.react import ReActAgent
from agent.runtime.task_store import TaskStore
from agent.runtime.tools.code import register_code_tools
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.sandbox.local import LocalSandbox


class OfflineLLM:
    class config:
        model = "offline-reliability-fixture"
        capabilities = frozenset()

    async def chat_stream(self, messages, tools, **kwargs):
        yield {"type": "done", "content": "已阅读。", "usage": None}


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HINDSIGHT_ENABLED", "0")
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_EMBEDDING", "off")
    monkeypatch.setenv("MEMORY_AUTO_RETAIN", "0")


def memory_store(root):
    return MemoryStore(path=root / "memory.db", core_dir=root / "core")


@pytest.mark.parametrize("enabled", ["0", "1"])
@pytest.mark.parametrize("text", [
    "请翻译这句台词：我喜欢详细回答。",
    "不要记住：我喜欢详细回答",
    "把终端截图的标题改成：测试结果",
    "我喜欢详细回答",
])
def test_conversation_never_triggers_syntactic_memory_writes(tmp_path, monkeypatch, text, enabled):
    monkeypatch.setenv("MEMORY_AUTO_RETAIN", enabled)
    store = memory_store(tmp_path)
    old = store.add_record(kind="preference", content="用户偏好：我喜欢用深色主题查看终端日志")
    retainer = MemoryRetainer(BuiltinMemoryProvider(store), store, mode="evolving")
    agent = ReActAgent("audit", OfflineLLM(), ToolRegistry(), memory_store=store,
                       memory_retainer=retainer, timing_log_enabled=False, query_profile_enabled=False)
    agent.context.set_session(str(tmp_path / "s1.jsonl"))

    async def run():
        return [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text(text)]))]

    events = asyncio.run(run())
    assert not [e for e in events if e.get("type") == "memory_retention"]
    assert store.record_store.get(old.record_id).status == "active"
    assert [r.record_id for r in store.recall_records("", limit=50)] == [old.record_id]


def test_retention_off_matches_runtime_diagnostic(tmp_path):
    store = memory_store(tmp_path)
    agent = ReActAgent("audit", OfflineLLM(), ToolRegistry(), memory_store=store)
    assert "disabled" in build_runtime_capabilities(agent).get("memory.auto_retain").detail
    outcome = asyncio.run(agent.memory_retainer.retain_turn(
        "", session_id="s1", message_id="m1",
        tool_evidence=[ToolEvidence("probe", "observed UTC timezone", "t1")],
    ))
    assert outcome.retained_ids == ()
    assert store.recall_records("", limit=50) == []


@pytest.mark.parametrize("texts", [
    ["Astra 在执行生产数据库迁移前，必须先取得用户确认并保存本次操作的审计记录。",
     "Astra 在执行生产数据库迁移前，不必先取得用户确认并保存本次操作的审计记录。"],
    ["项目 A 的生产环境必须启用数据库自动备份，并且保留最近三十天的恢复记录。",
     "项目 B 的生产环境必须启用数据库自动备份，并且保留最近三十天的恢复记录。"],
])
def test_similar_observations_are_not_semantic_duplicates(tmp_path, texts):
    store = memory_store(tmp_path)
    records = [store.add_record(kind="observation", content=text) for text in texts]
    assert MemoryConsolidator(store.record_store).merge_duplicate_observations() == 0
    assert all(store.record_store.get(r.record_id).status == "active" for r in records)


def test_old_temporary_episode_is_not_promoted_to_permanent_fact(tmp_path):
    store = memory_store(tmp_path)
    now = datetime.now(timezone.utc)
    episode = store.add_record(kind="episode", content="用户近期状态：我正在准备课程展示，我在下周参加答辩",
                               valid_until=(now + timedelta(days=1)).isoformat())
    with store.record_store._connection() as db:
        db.execute("UPDATE memory_records SET created_at=? WHERE id=?",
                   ((now - timedelta(days=8)).isoformat(), episode.record_id))
    assert MemoryConsolidator(store.record_store).run_all()["extracted_observations"] == 0
    assert store.recall_records("", kinds=("user_fact", "preference")) == []
    assert store.record_store.get(episode.record_id).valid_until == episode.valid_until


def test_real_execution_does_not_masquerade_as_coding_verification(tmp_path):
    async def run():
        store = TaskStore(tmp_path / "tasks.db")
        task = store.start_run("audit", "修复示例 Python 文件并验证", session_id="s1")
        registry = ToolRegistry()
        sandbox = LocalSandbox(timeout=3, workdir=str(tmp_path))
        register_code_tools(registry, sandbox, task_store=store)

        async def edit_probe(path: str):
            (tmp_path / path).write_text("def broken(:\n", encoding="utf-8")
            return "written"

        registry.register(ToolDef(name="edit_probe", description="temporary file writer",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            fn=edit_probe, risk="write", group="files"))
        agent = ReActAgent("audit", OfflineLLM(), registry, task_store=store,
                           timing_log_enabled=False, query_profile_enabled=False)

        async def call(name, **args):
            return await agent._execute_tool_call({"id": name, "name": name,
                "arguments": json.dumps(args)}, task_id=task["id"])

        try:
            await call("edit_probe", path="broken.py")
            # Exercise a real native shell without requiring WSL on Windows.
            pwd = await call("execute_shell", command="cd" if sys.platform == "win32" else "pwd",
                             environment="windows" if sys.platform == "win32" else "posix",
                             foreground_yield_ms=0)
            assert pwd["execution"]["exit_code"] == 0
            assert agent._verification_required is True
            contract = store.get_task(task["id"])["verification"]
            assert contract["status"] != "passed"
            assert not contract["checks"][-1].get("passed")
            failure = await call("execute_python", code="assert False, 'audit check failed'", foreground_yield_ms=0)
            assert failure["execution"]["exit_code"] == 1
            assert failure["execution"]["status"] == "completed"
            assert agent._verification_required is True
            contract = store.get_task(task["id"])["verification"]
            assert contract["checks"][-1]["exit_code"] == 1
            assert not contract["checks"][-1].get("passed")
            assert contract["status"] != "passed"
            assert store.get_task(task["id"])["status"] != "done_verified"
        finally:
            await sandbox.close()

    asyncio.run(run())


def test_goal_verifier_keeps_command_and_failure_tail():
    prefix = "collected 150 items\n" + "tests/test_example.py . PASSED\n" * 80
    command = "import pytest; raise SystemExit(pytest.main(['-q']))"

    def transcript(tail):
        return build_transcript([
            {"role": "user", "content": "Run all tests and finish only when they all pass"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "test-call", "type": "function",
                "function": {"name": "execute_python", "arguments": json.dumps({"code": command})}}]},
            {"role": "tool", "tool_call_id": "test-call", "content": prefix + tail},
        ])

    success = transcript("150 passed in 5.0s")
    failure = transcript("149 passed, 1 failed in 5.0s\n[exit code: 1]")
    assert success != failure
    assert command in failure
    assert "1 failed" in failure
    assert "exit code: 1" in failure


@pytest.mark.parametrize("evidence", [None, "", "  "])
def test_blank_evidence_cannot_complete_a_goal(tmp_path, evidence):
    store = TaskStore(tmp_path / "tasks.db")
    goal = store.set_goal("audit", "All tests pass")
    verdict = parse_verdict(json.dumps({"met": True, "evidence": evidence}))
    assert not verdict_is_usable(verdict)
    assert store.record_goal_round(goal["id"], verdict)["status"] != "completed"


def test_mixed_language_query_does_not_drop_semantic_bug_match(tmp_path):
    store = memory_store(tmp_path)
    record = store.add_record(kind="observation", content="昨天已修复登录页面的报错，原因是会话过期后没有清理旧令牌。")
    with store.record_store._connection() as db:
        row = db.execute("SELECT * FROM memory_records WHERE id=?", (record.record_id,)).fetchone()
    plan = plan_query("查一下之前修复的 bug", now=datetime.now(timezone.utc) + timedelta(seconds=1))
    # Controlled vector hit: tests the production filter, not embedding accuracy.
    items = SemanticReader._records(store.path, [(record.record_id, 0.99)],
        {record.record_id: _revision(row)}, plan, WorkspaceIdentity("audit", str(tmp_path), "audit"),
        "s1", frozenset(), LexicalQuery.from_text(plan.query))
    assert len(items) == 1


def test_goal_store_rejects_direct_unusable_verdict_without_spending_round(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    goal = store.set_goal("audit", "All tests pass")
    for verdict in ({"met": True}, {"met": "false", "evidence": "text"},
                    {"met": True, "evidence": {"guess": True}},
                    {"met": True, "evidence": "x", "error": "provider failed"}):
        updated = store.record_goal_round(goal["id"], verdict)
        assert updated["status"] == "active" and updated["round"] == 0


@pytest.mark.parametrize("limit", [0, 1, 15, 100, 2000])
def test_verifier_transcript_obeys_global_budget_even_for_one_large_message(limit):
    transcript = build_transcript([{"role": "tool", "content": "x" * 20000 + "exit 1"}], max_chars=limit)
    assert len(transcript) <= limit
