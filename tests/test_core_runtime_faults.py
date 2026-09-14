import asyncio
import multiprocessing
import os
import threading

from agent.core.msg import ContentBlock, Msg
from agent.runtime.event_stream import RuntimeEventStream
from agent.runtime.react import ReActAgent
from agent.runtime.task_store import TaskStore
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def run(awaitable):
    return asyncio.run(awaitable)


async def collect(stream):
    return [event async for event in stream]


def _interrupt_event_publish(event_path, task_path, phase):
    stream = RuntimeEventStream(event_path)
    store = TaskStore(task_path)
    task = store.start_run(
        "interrupted-request",
        "perform unsafe write",
        session_id="test-session",
        model="test-model",
    )
    store.claim_tool(
        task["id"],
        "write_file",
        {"path": "result.txt", "content": "unsafe"},
        "write",
        invocation_id="unsafe-call",
    )

    def fault_hook(observed_phase):
        if observed_phase == phase:
            os._exit(73)

    stream._fault_hook = fault_hook
    stream.publish(
        {"type": "task_status", "task": {"id": task["id"], "status": "cancelled"}}
    )


class _OneToolCallLLM:
    class _Config:
        model = "test-model"
        capabilities = frozenset()

    config = _Config()

    def __init__(self, *, name: str, arguments: str):
        self.name = name
        self.arguments = arguments

    async def chat_stream(self, messages, tools):
        del messages, tools
        yield {
            "type": "tool_calls",
            "calls": [{
                "id": "call-1",
                "name": self.name,
                "arguments": self.arguments,
            }],
            "content": "",
            "reasoning_content": "",
            "finish_reason": "tool_calls",
            "usage": None,
        }


def test_invalid_tool_arguments_never_reach_approval_or_execution():
    async def scenario():
        approvals = []
        executions = []

        async def approve(request):
            approvals.append(request)
            return "once"

        async def write_path(path: str):
            executions.append(path)
            return path

        registry = ToolRegistry()
        registry.register(ToolDef(
            name="write_path",
            description="write one path",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            fn=write_path,
            risk="write",
            approval="always",
        ))
        registry.set_approval_handler(approve)
        agent = ReActAgent(
            "agent",
            _OneToolCallLLM(name="write_path", arguments='{"path":'),
            registry,
            max_iterations=1,
        )

        events = await collect(agent.reply_stream(
            Msg(content=[ContentBlock.text("write it")])
        ))

        assert next(event for event in events if event.get("code") == "invalid_arguments")
        assert approvals == []
        assert executions == []
        assert not any(message.get("tool_calls") for message in agent.context.messages)

    run(scenario())


def test_cancellation_during_running_tool_reaches_tool_and_keeps_step_nonterminal(tmp_path):
    async def scenario():
        started = asyncio.Event()
        cancellations = []
        executions = []

        async def wait_forever():
            executions.append("started")
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellations.append("cancelled")
                raise

        store = TaskStore(tmp_path / "tasks.db")
        task = store.start_run(
            "cancel-request",
            "run the cancellable tool",
            session_id="test-session",
            model="test-model",
        )
        registry = ToolRegistry()
        registry.register(ToolDef(
            name="wait_forever",
            description="wait until cancelled",
            parameters={"type": "object", "properties": {}},
            fn=wait_forever,
        ))
        agent = ReActAgent(
            "agent",
            _OneToolCallLLM(name="wait_forever", arguments="{}"),
            registry,
            max_iterations=1,
            task_store=store,
        )

        events = []

        async def consume():
            async for event in agent.reply_stream(Msg(
                content=[ContentBlock.text("start")],
                metadata={"task_id": task["id"]},
            )):
                events.append(event)

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=2)
        consumer.cancel()
        await asyncio.wait_for(consumer, timeout=2)

        persisted = store.get_task(task["id"])
        assert persisted is not None
        assert executions == ["started"]
        assert cancellations == ["cancelled"]
        tool_step = next(step for step in persisted["steps"] if step["kind"] == "tool")
        assert tool_step["status"] == "unknown"
        assert tool_step["status"] != "completed"
        assert sum(event.get("code") == "cancelled" for event in events) == 1
        assert sum(event.get("type") == "done" for event in events) == 1

    run(scenario())


def test_duplicate_side_effect_claims_are_exclusive_across_store_instances(tmp_path):
    path = tmp_path / "tasks.db"
    owner = TaskStore(path)
    task = owner.start_run(
        "duplicate-request",
        "write one file",
        session_id="test-session",
        model="test-model",
    )
    stores = [TaskStore(path), TaskStore(path)]
    barrier = threading.Barrier(2)
    actions = []
    errors = []

    def claim(store):
        try:
            barrier.wait(timeout=10)
            result = store.claim_tool(
                task["id"],
                "write_file",
                {"path": "a.txt", "content": "x"},
                "write",
                invocation_id="call-1",
            )
            actions.append(result.action)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=claim, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(actions) == ["execute", "uncertain"]


def test_real_interrupt_before_event_commit_rolls_back_and_keeps_write_uncertain(
    tmp_path,
):
    event_path = tmp_path / "events.db"
    task_path = tmp_path / "tasks.db"
    process = multiprocessing.get_context("spawn").Process(
        target=_interrupt_event_publish,
        args=(event_path, task_path, "before_commit"),
    )
    process.start()
    process.join(5)

    assert process.exitcode == 73
    assert RuntimeEventStream(event_path).replay(0) == []
    restarted = TaskStore(task_path)
    assert restarted.recover_interrupted() == 1
    task = restarted.list_tasks(limit=1)[0]
    claim = restarted.claim_tool(
        task["id"],
        "write_file",
        {"path": "result.txt", "content": "unsafe"},
        "write",
        invocation_id="unsafe-call",
    )
    assert claim.action == "uncertain"


def test_real_interrupt_after_event_commit_replays_terminal_cursor_once(tmp_path):
    event_path = tmp_path / "events.db"
    task_path = tmp_path / "tasks.db"
    process = multiprocessing.get_context("spawn").Process(
        target=_interrupt_event_publish,
        args=(event_path, task_path, "after_commit"),
    )
    process.start()
    process.join(5)

    assert process.exitcode == 73
    replayed = RuntimeEventStream(event_path).replay(0)
    assert len(replayed) == 1
    assert replayed[0]["cursor"] == 1
    assert replayed[0]["type"] == "task_status"
    assert replayed[0]["task"]["status"] == "cancelled"
    assert RuntimeEventStream(event_path).replay(1) == []
    restarted = TaskStore(task_path)
    assert restarted.recover_interrupted() == 1
    task = restarted.list_tasks(limit=1)[0]
    claim = restarted.claim_tool(
        task["id"],
        "write_file",
        {"path": "result.txt", "content": "unsafe"},
        "write",
        invocation_id="unsafe-call",
    )
    assert claim.action == "uncertain"
