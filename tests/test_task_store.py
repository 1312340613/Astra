import asyncio
import sqlite3
from pathlib import Path

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.runtime.react import ReActAgent
from agent.runtime.context import AgentContext
from agent.runtime.task_store import TaskStore, format_task_detail, format_task_list
from agent.runtime.tools.registry import ToolDef, ToolRegistry


class _FakeLLM:
    class _Config:
        model = "test-model"
        capabilities = frozenset({"reasoning"})

    config = _Config()


def _run(store: TaskStore, request_id: str = "req-1") -> dict:
    return store.start_run(request_id, "write the report", session_id="default", model="test-model")


def test_task_store_records_lists_and_formats(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    run = _run(store)
    step = store.start_step(run["id"], "llm:0:1", "llm", name="test-model", input_value={"iteration": 1})
    store.finish_step(step["id"], output={"has_content": True})
    store.checkpoint(run["id"], {"phase": "after_llm", "iteration": 1})
    store.finish_run(run["id"], "completed")

    task = store.get_task(run["id"])
    assert task is not None
    assert task["status"] == "completed"
    assert task["checkpoint"] == {"phase": "after_llm", "iteration": 1}
    assert task["steps"][0]["status"] == "completed"
    assert run["id"] in format_task_list(store.list_tasks())
    assert "after_llm" in format_task_detail(task)


def test_task_store_goal_history_rounds(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    assert store.get_goal_history("s1") is None

    goal = store.set_goal("s1", "make pytest pass")
    owner, history = store.get_goal_history("s1")
    assert owner["id"] == goal["id"]
    assert history == []

    store.record_goal_round(goal["id"], {"met": False, "evidence": "red", "next_step": "implement"})
    _, history = store.get_goal_history("s1")
    assert len(history) == 1
    assert history[0]["round"] == 1
    assert history[0]["met"] is False
    assert history[0]["evidence"] == "red"
    assert history[0]["next_step"] == "implement"

    store.record_goal_round(goal["id"], {"met": True, "evidence": "green", "next_step": ""})
    owner, history = store.get_goal_history("s1")
    assert len(history) == 2
    assert history[-1]["met"] is True
    assert history[-1]["round"] == 2
    # A completed goal still exposes its recorded rounds.
    assert owner["status"] == "completed"
    assert owner["round"] == 2


def test_new_task_database_has_no_case_schema(tmp_path: Path):
    path = tmp_path / "tasks.db"
    TaskStore(path)

    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {row[1] for row in db.execute("PRAGMA table_info(task_runs)")}

    assert "cases" not in tables
    assert "case_sessions" not in tables
    assert "case_id" not in columns


def test_legacy_case_column_is_not_exposed_or_reused(tmp_path: Path):
    path = tmp_path / "tasks.db"
    store = TaskStore(path)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE task_runs ADD COLUMN case_id TEXT NOT NULL DEFAULT ''")

    run = store.start_run("req-legacy", "完成得很好", session_id="s1")

    assert "case_id" not in run
    with sqlite3.connect(path) as db:
        stored = db.execute("SELECT case_id FROM task_runs WHERE id=?", (run["id"],)).fetchone()[0]
    assert stored == ""


def test_recover_marks_running_steps_unknown_and_blocks_replay(tmp_path: Path):
    path = tmp_path / "tasks.db"
    store = TaskStore(path)
    run = _run(store)
    first = store.claim_tool(run["id"], "write_file", {"path": "a.txt", "content": "x"}, "write")
    assert first.action == "execute"

    restarted = TaskStore(path)
    assert restarted.recover_interrupted() == 1
    task = restarted.get_task(run["id"])
    assert task is not None
    assert task["status"] == "interrupted"
    assert task["steps"][0]["status"] == "unknown"
    second = restarted.claim_tool(run["id"], "write_file", {"path": "a.txt", "content": "x"}, "write")
    assert second.action == "uncertain"


def test_completed_tool_result_is_reused_across_store_instances(tmp_path: Path):
    path = tmp_path / "tasks.db"
    store = TaskStore(path)
    run = _run(store)
    claim = store.claim_tool(run["id"], "read_file", {"path": "a.txt"}, "read")
    event = {"type": "tool_result", "name": "read_file", "output": "hello", "error": "", "tool_output": "hello"}
    store.finish_step(claim.step_id, output=event)

    restarted = TaskStore(path)
    cached = restarted.claim_tool(run["id"], "read_file", {"path": "a.txt"}, "read")
    assert cached.action == "cached"
    assert cached.output == event


def test_explicit_safe_replay_reclaims_only_an_unknown_step(tmp_path: Path):
    path = tmp_path / "tasks.db"
    store = TaskStore(path)
    run = _run(store)
    first = store.claim_tool(run["id"], "read_file", {"path": "a.txt"}, "read", replay="safe")
    assert first.action == "execute"

    # A live running claim is never replayed concurrently, even when marked safe.
    assert store.claim_tool(
        run["id"], "read_file", {"path": "a.txt"}, "read", replay="safe"
    ).action == "uncertain"

    restarted = TaskStore(path)
    assert restarted.recover_interrupted() == 1
    replayed = restarted.claim_tool(
        run["id"], "read_file", {"path": "a.txt"}, "read", replay="safe"
    )
    assert replayed.action == "execute"
    assert replayed.step_id == first.step_id
    step = restarted.get_task(run["id"])["steps"][0]
    assert step["status"] == "running"
    assert step["attempt"] == 2


def test_default_never_replay_keeps_unknown_side_effect_blocked(tmp_path: Path):
    path = tmp_path / "tasks.db"
    store = TaskStore(path)
    run = _run(store)
    first = store.claim_tool(run["id"], "write_file", {"path": "a.txt"}, "write")
    assert first.action == "execute"
    assert TaskStore(path).recover_interrupted() == 1
    assert TaskStore(path).claim_tool(
        run["id"], "write_file", {"path": "a.txt"}, "write"
    ).action == "uncertain"


def test_recovery_replays_safe_read_but_keeps_unsafe_write_uncertain(tmp_path: Path):
    path = tmp_path / "tasks.db"
    store = TaskStore(path)
    run = _run(store)
    read = store.claim_tool(
        run["id"], "read_file", {"path": "source.txt"}, "read", replay="safe"
    )
    write = store.claim_tool(
        run["id"], "write_file", {"path": "result.txt", "content": "x"}, "write"
    )

    restarted = TaskStore(path)
    assert restarted.recover_interrupted() == 1

    recovered_read = restarted.claim_tool(
        run["id"], "read_file", {"path": "source.txt"}, "read", replay="safe"
    )
    recovered_write = restarted.claim_tool(
        run["id"], "write_file", {"path": "result.txt", "content": "x"}, "write"
    )
    steps = {step["id"]: step for step in restarted.get_task(run["id"])["steps"]}

    assert recovered_read.action == "execute"
    assert recovered_read.step_id == read.step_id
    assert steps[read.step_id]["attempt"] == 2
    assert recovered_write.action == "uncertain"
    assert recovered_write.step_id == write.step_id
    assert steps[write.step_id]["status"] == "unknown"


def test_tool_artifact_remains_in_step_output(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    run = store.start_run("req-artifact", "生成申请材料报告", session_id="s1")
    step = store.start_step(run["id"], "tool:report", "tool", name="write_file")

    store.finish_step(step["id"], output={"artifact_path": "reports/application.md"})

    stored = store.get_task(run["id"])
    assert stored is not None
    assert stored["steps"][0]["output"] == {"artifact_path": "reports/application.md"}


def test_react_uses_persistent_tool_result_without_reexecution(tmp_path: Path):
    calls = 0

    async def tool(value: str) -> str:
        nonlocal calls
        calls += 1
        return value.upper()

    registry = ToolRegistry()
    registry.register(ToolDef(
        name="uppercase",
        description="uppercase",
        parameters={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
        fn=tool,
        idempotent=True,
    ))
    store = TaskStore(tmp_path / "tasks.db")
    run = _run(store)
    agent = ReActAgent("agent", _FakeLLM(), registry, task_store=store)  # type: ignore[arg-type]
    call = {"id": "call-1", "name": "uppercase", "arguments": '{"value":"hello"}'}

    first = asyncio.run(agent._execute_tool_call(call, task_id=run["id"]))
    second = asyncio.run(agent._execute_tool_call({**call, "id": "call-2"}, task_id=run["id"]))
    assert first["output"] == "HELLO"
    assert second["output"] == "HELLO"
    assert second["persistent_cache"] is True
    assert calls == 1


def test_react_tracks_identical_non_idempotent_calls_as_distinct_invocations(tmp_path: Path):
    calls = 0

    async def submit(prompt: str) -> str:
        nonlocal calls
        calls += 1
        return f"job-{calls}:{prompt}"

    registry = ToolRegistry()
    registry.register(ToolDef(
        name="random_draw",
        description="submit a randomized image",
        parameters={"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
        fn=submit,
        idempotent=False,
        repeat_guard=False,
    ))
    store = TaskStore(tmp_path / "tasks.db")
    run = _run(store)
    agent = ReActAgent("agent", _FakeLLM(), registry, task_store=store)  # type: ignore[arg-type]
    call = {"id": "draw-1", "name": "random_draw", "arguments": '{"prompt":"same scene"}'}

    first = asyncio.run(agent._execute_tool_call(call, task_id=run["id"]))
    second = asyncio.run(agent._execute_tool_call({**call, "id": "draw-2"}, task_id=run["id"]))
    replay = asyncio.run(agent._execute_tool_call(call, task_id=run["id"]))

    assert first["output"] == "job-1:same scene"
    assert second["output"] == "job-2:same scene"
    assert replay["persistent_cache"] is True
    assert calls == 2


@pytest.mark.parametrize(
    ("risk", "idempotent"),
    [
        ("write", False),
        ("read", False),
        ("network", True),
    ],
)
def test_react_blocks_unsafe_tool_when_durable_tool_claim_fails(
    risk: str,
    idempotent: bool,
):
    calls = 0

    async def write_value(value: str) -> str:
        nonlocal calls
        calls += 1
        return value

    class BrokenTaskStore:
        def claim_tool(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

    registry = ToolRegistry()
    registry.register(ToolDef(
        name="write_value",
        description="write a value",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        fn=write_value,
        risk=risk,
        idempotent=idempotent,
    ))
    agent = ReActAgent(
        "agent",
        _FakeLLM(),
        registry,
        task_store=BrokenTaskStore(),  # type: ignore[arg-type]
    )

    result = asyncio.run(agent._execute_tool_call(
        {"id": "write-1", "name": "write_value", "arguments": '{"value":"x"}'},
        task_id="task-1",
    ))

    assert calls == 0
    assert result["error_type"] == "tool_claim_unavailable"
    assert result["code"] == "tool_claim_unavailable"
    assert result["recovery_blocked"] is True
    assert result["retryable"] is False
    assert "was not executed" in result["error"]


def test_react_allows_explicitly_idempotent_read_when_durable_claim_fails():
    calls = 0

    async def read_value(value: str) -> str:
        nonlocal calls
        calls += 1
        return value.upper()

    class BrokenTaskStore:
        def claim_tool(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

    registry = ToolRegistry()
    registry.register(ToolDef(
        name="read_value",
        description="read a value",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        fn=read_value,
        risk="read",
        idempotent=True,
    ))
    agent = ReActAgent(
        "agent",
        _FakeLLM(),
        registry,
        task_store=BrokenTaskStore(),  # type: ignore[arg-type]
    )

    result = asyncio.run(agent._execute_tool_call(
        {"id": "read-1", "name": "read_value", "arguments": '{"value":"hello"}'},
        task_id="task-1",
    ))

    assert calls == 1
    assert result["output"] == "HELLO"
    assert not result["error"]


def test_resume_only_allows_terminal_recoverable_states(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    run = _run(store)
    with pytest.raises(ValueError, match="only interrupted"):
        store.prepare_resume(run["id"])
    store.finish_run(run["id"], "failed", "network")
    resumed = store.prepare_resume(run["id"])
    assert resumed["status"] == "running"
    assert resumed["resume_count"] == 1


def test_event_keys_are_idempotent(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    run = _run(store)
    first = store.append_event(run["id"], "same-key", "notice", {"value": 1})
    second = store.append_event(run["id"], "same-key", "notice", {"value": 2})
    assert first == second


def test_checkpoint_preserves_complete_tool_pair_for_next_llm_and_reload(tmp_path: Path):
    class ToolThenAnswerLLM:
        def __init__(self):
            self.calls = 0
            self.second_prompt = None
            self.config = _FakeLLM._Config()

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{"id": "search-1", "name": "lookup", "arguments": '{"query":"AI news"}'}],
                    "content": "",
                    "reasoning_content": "I should search once.",
                    "usage": None,
                }
            else:
                self.second_prompt = messages
                yield {"type": "done", "content": "Here is the answer.", "usage": None}

    async def lookup(query: str) -> str:
        return f"result for {query}"

    registry = ToolRegistry()
    registry.register(ToolDef("lookup", "lookup", {"type": "object"}, lookup))
    store = TaskStore(tmp_path / "tasks.db")
    run = _run(store)
    llm = ToolThenAnswerLLM()
    agent = ReActAgent("agent", llm, registry, task_store=store)  # type: ignore[arg-type]
    session = tmp_path / "session.json"
    agent.context.set_session(str(session))
    msg = Msg(
        content=[ContentBlock.text("find news")],
        metadata={"task_id": run["id"]},
    )
    asyncio.run(agent.reply(msg))

    assert llm.second_prompt is not None
    assistant = next(item for item in llm.second_prompt if item.get("tool_calls"))
    tool_result = next(item for item in llm.second_prompt if item.get("role") == "tool")
    assert assistant["tool_calls"][0]["id"] == "search-1"
    assert assistant["reasoning_content"] == "I should search once."
    assert tool_result["tool_call_id"] == "search-1"
    assert "result for AI news" in tool_result["content"]

    reloaded = AgentContext()
    reloaded.set_session(str(session))
    assert reloaded.load() is True
    assert any(item.get("tool_calls") for item in reloaded.messages)
    assert any(item.get("role") == "tool" and item.get("tool_call_id") == "search-1" for item in reloaded.messages)
    task = store.get_task(run["id"])
    assert task is not None
    assert task["checkpoint"]["phase"] == "turn_complete"
    assert task["checkpoint"]["final_summary"] == "Here is the answer."


def test_per_turn_tool_budget_stops_semantic_query_churn():
    class QueryChurnLLM:
        def __init__(self):
            self.calls = 0
            self.config = _FakeLLM._Config()

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if not any(item.get("function", {}).get("name") == "search_web" for item in tools):
                yield {"type": "chunk", "content": "Synthesized from existing results."}
                yield {"type": "done", "content": "Synthesized from existing results.", "usage": None}
                return
            yield {
                "type": "tool_calls",
                "calls": [{
                    "id": f"call-{self.calls}",
                    "name": "search_web",
                    "arguments": '{"query":"AI news variation %d"}' % self.calls,
                }],
                "content": "",
                "reasoning_content": "",
                "usage": None,
            }

    executed = 0

    async def search_web(query: str) -> str:
        nonlocal executed
        executed += 1
        return f"results for {query}"

    registry = ToolRegistry()
    registry.register(ToolDef(
        "search_web",
        "search",
        {"type": "object"},
        search_web,
        max_calls_per_turn=2,
    ))
    agent = ReActAgent("agent", QueryChurnLLM(), registry, max_iterations=10)  # type: ignore[arg-type]
    events = asyncio.run(_collect(agent.reply_stream(Msg(content=[ContentBlock.text("AI news")]))))
    assert executed == 2
    assert any(event.get("type") == "chunk" and "Synthesized" in event.get("content", "") for event in events)
    # The schema stays byte-stable for prefix-cache reuse. The execution layer
    # rejects the third call and final synthesis uses the two successful
    # results already present in history.
    assert any("tool budget exhausted" in event.get("message", "") for event in events)


async def _collect(stream):
    return [event async for event in stream]


def test_reasoning_content_is_replayed_on_the_next_user_turn():
    class TwoTurnLLM:
        def __init__(self):
            self.calls = 0
            self.config = _FakeLLM._Config()

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                yield {"type": "reasoning", "content": "private reasoning"}
                yield {
                    "type": "done",
                    "content": "hello",
                    "reasoning_content": "private reasoning",
                    "usage": None,
                }
            else:
                assistant = next(item for item in messages if item.get("role") == "assistant")
                assert assistant["reasoning_content"] == "private reasoning"
                yield {"type": "done", "content": "second answer", "usage": None}

    agent = ReActAgent("agent", TwoTurnLLM(), ToolRegistry(), max_iterations=1)  # type: ignore[arg-type]
    asyncio.run(agent.reply(Msg(content=[ContentBlock.text("hello")])))
    response = asyncio.run(agent.reply(Msg(content=[ContentBlock.text("latest news")])))
    assert response is not None
    assert response.get_text() == "second answer"


def test_concurrent_claim_grants_execute_to_exactly_one_instance(tmp_path: Path):
    """两个独立实例（模拟跨进程并发）同时 claim 同一工具时，
    只有一个实例获得 execute，另一个得到 uncertain。

    旧实现把“查重”和“创建 running step”拆在两个事务里，两个执行者
    都可能看到“尚不存在”并各自返回 execute，导致有副作用的工具被
    重复触发。修复后 claim 在单个 BEGIN IMMEDIATE 事务内完成。
    """
    import threading
    import time
    from unittest import mock

    path = tmp_path / "tasks.db"
    store = TaskStore(path)
    run = _run(store)

    # 两个实例各有自己的 RLock，互不保护——真实竞争 SQLite 锁，
    # 等价于两个进程同时 claim。
    first = TaskStore(path)
    second = TaskStore(path)
    barrier = threading.Barrier(2)
    actions: list[str] = []

    # 人为扩大“查重 → 创建”竞争窗口：旧实现 claim_tool 通过 start_step
    # 创建 step，注入延迟后两个实例都能在对方插入前通过查重，从而双双
    # 拿到 execute。新实现把创建内联进单个 BEGIN IMMEDIATE 事务，不再
    # 调用 start_step，因此该延迟只影响旧实现——保证测试能捕获旧 bug。
    original_start_step = TaskStore.start_step

    def slow_start_step(instance: TaskStore, *args, **kwargs):
        time.sleep(0.3)
        return original_start_step(instance, *args, **kwargs)

    def claim(instance: TaskStore) -> None:
        barrier.wait()
        result = instance.claim_tool(
            run["id"], "write_file", {"path": "a.txt", "content": "x"}, "write"
        )
        actions.append(result.action)

    with mock.patch.object(TaskStore, "start_step", slow_start_step):
        threads = [
            threading.Thread(target=claim, args=(first,)),
            threading.Thread(target=claim, args=(second,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

    assert sorted(actions) == ["execute", "uncertain"], actions
    task = store.get_task(run["id"])
    assert task is not None
    assert len(task["steps"]) == 1


def test_repeat_claim_is_uncertain_until_finished(tmp_path: Path):
    """同一实例串行重复 claim 未完成的工具，不重复授权 execute。"""
    store = TaskStore(tmp_path / "tasks.db")
    run = _run(store)

    first = store.claim_tool(run["id"], "write_file", {"path": "a.txt", "content": "x"}, "write")
    assert first.action == "execute"

    second = store.claim_tool(run["id"], "write_file", {"path": "a.txt", "content": "x"}, "write")
    assert second.action == "uncertain"
    assert second.step_id == first.step_id


def test_claim_waits_for_contended_write_lock_instead_of_sqlite_busy(tmp_path: Path):
    """另一连接持写锁时，claim 等待（busy_timeout）而非抛 SQLITE_BUSY。"""
    import threading
    import time

    path = tmp_path / "tasks.db"
    store = TaskStore(path)
    run = _run(store)

    holder = sqlite3.connect(str(path), timeout=10)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE task_runs SET error='hold-lock' WHERE id=?", (run["id"],))

    actions: list[str] = []

    def claim() -> None:
        result = store.claim_tool(run["id"], "read_file", {"path": "a.txt"}, "read")
        actions.append(result.action)

    thread = threading.Thread(target=claim)
    thread.start()
    time.sleep(0.3)  # claim 线程已进入 BEGIN IMMEDIATE 等待
    holder.commit()  # 释放写锁
    holder.close()
    thread.join(timeout=10)

    assert actions == ["execute"], actions


def test_claim_fails_explicitly_after_configured_sqlite_lock_timeout(tmp_path: Path):
    """A bounded internal timeout reports contention without changing task state."""
    import time

    path = tmp_path / "tasks.db"
    store = TaskStore(path, busy_timeout_ms=50)
    run = _run(store)
    before = store.get_task(run["id"])

    holder = sqlite3.connect(str(path), timeout=10)
    holder.execute("BEGIN IMMEDIATE")
    try:
        claim = None
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            claim = store.claim_tool(run["id"], "read_file", {"path": "a.txt"}, "read")
        elapsed = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()

    after = store.get_task(run["id"])
    assert claim is None
    assert elapsed < 1.0
    assert before is not None
    assert after is not None
    assert after["status"] == before["status"] == "running"
    assert after["finished_at"] == before["finished_at"]
    assert after["steps"] == before["steps"] == []
