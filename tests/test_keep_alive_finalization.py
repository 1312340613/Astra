"""Parent completion must join an episode, not a retained member's lifetime."""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.runtime.agent_team import AgentTeamRuntime, AgentTeamStore
from agent.runtime.react import ReActAgent
from agent.runtime.task_store import TaskStore
from agent.runtime.tools import delegate
from agent.runtime.tools.delegate import DelegateMailbox, DelegateMailboxStore, register_delegate_tools
from agent.runtime.tools.processes import ProcessManager
from agent.runtime.tools.registry import ToolRegistry


class Member:
    def __init__(self):
        self.requests = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def chat(self, messages, **kwargs):
        self.requests.append(deepcopy(messages))
        self.started.set()
        await self.release.wait()
        return {"content": f"verified episode {len(self.requests)}", "tool_calls": []}


async def call(registry, task, tool, **args):
    result = await registry.execute(tool, args, task_id=task["id"])
    assert not result["error"], result
    return json.loads(result["output"])


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


def setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = ProcessManager(artifact_dir=tmp_path / "processes")
    monkeypatch.setattr(delegate, "_sub_processes", manager)
    tasks = TaskStore(tmp_path / "tasks.db")
    registry = ToolRegistry()
    member = Member()
    mailbox = register_delegate_tools(
        registry, llm_getter=lambda: member, task_store=tasks, session_id_getter=lambda: "retained-session",
    )
    return manager, tasks, registry, member, mailbox


class Parent:
    def __init__(self, tool_calls):
        self.config = SimpleNamespace(model="stub-parent")
        self.tool_calls = list(tool_calls)
        self.prompts = []

    def estimate_tokens(self, messages):
        return len(str(messages)) // 4

    async def chat_stream(self, messages, tools):
        self.prompts.append(deepcopy(messages))
        if self.tool_calls:
            name, args = self.tool_calls.pop(0)
            yield {"type": "tool_calls", "calls": [{
                "id": f"parent-call-{len(self.prompts)}", "name": name, "arguments": json.dumps(args),
            }], "content": "", "reasoning_content": "", "usage": None}
        else:
            yield {"type": "done", "content": "Parent work finished.", "usage": None}


@pytest.mark.parametrize("already_idle", [False, True])
def test_parent_finishes_and_next_turn_adopts_the_same_idle_member(tmp_path, monkeypatch, already_idle):
    async def scenario():
        manager, tasks, registry, member, mailbox = setup(tmp_path, monkeypatch)
        old = tasks.start_run("old", "first turn", session_id="retained-session")
        team = await call(registry, old, "team", action="create", name="retained", goal="two turns")
        handle = await call(registry, old, "team_spawn", team_id=team["id"], name="member",
                            goal="first assignment", keep_alive=True, timeout=30)
        process = manager.get(handle["process"]["process_id"])
        member_id = handle["agent"]["id"]
        team_store = AgentTeamStore(tasks.path)
        await member.started.wait()
        if already_idle:
            member.release.set()
            await until(lambda: team_store.get_agent(member_id)["status"] == "idle")

        async def run_parent(task, llm, *, release_on_join=False):
            agent = ReActAgent("parent", llm, registry, max_iterations=5)
            agent.delegate_mailbox = mailbox
            agent.task_store = tasks
            events = []
            async for event in agent.reply_stream(Msg(
                content=[ContentBlock.text("complete this turn")],
                metadata={"task_id": task["id"], "session_id": "retained-session"},
            )):
                events.append(event)
                if release_on_join and event.get("stage") == "joining":
                    member.release.set()
            assert any(e.get("type") == "done" for e in events)
            assert not [e for e in events if e.get("type") == "error"], events[-5:]
            # Backend lifecycle finishes the durable owner after ReAct emits
            # done; never fake that transition while the root is still joining.
            tasks.finish_run(task["id"], "completed")
            agent._close_turn_change_store()
            return events

        try:
            first_llm = Parent([])
            first_events = await asyncio.wait_for(run_parent(old, first_llm, release_on_join=True), 3)
            assert "verified episode 1" in str(first_llm.prompts)
            assert not mailbox.has_running(old["id"])
            assert mailbox.drain(old["id"]) == []
            assert not process.task.done()
            if not already_idle:
                assert any(e.get("stage") == "joining" for e in first_events)
            assert tasks.get_task(old["id"])["status"] == "completed"

            new = tasks.start_run("new", "second turn", session_id="retained-session")
            member.release.clear()
            second_llm = Parent([
                ("team", {"action": "resume", "team_id": team["id"]}),
                ("team_send", {"team_id": team["id"], "to": "member",
                               "message": "second assignment", "kind": "task_assignment"}),
            ])
            await asyncio.wait_for(run_parent(new, second_llm, release_on_join=True), 3)
            assert "verified episode 2" in str(second_llm.prompts)
            assert len(member.requests) == 2
            assert "verified episode 1" in str(member.requests[1])
            assert "second assignment" in str(member.requests[1])
            assert not process.task.done()
            assert team_store.get_agent(member_id)["process_id"] == process.process_id
            assert team_store.get_team(team["id"])["owner_task_id"] == new["id"]
            await mailbox.cancel_task(old["id"])
            assert not process.task.done()
            assert mailbox.drain(new["id"]) == []
            await mailbox.cancel_task(new["id"])
            assert manager.status(process) == "cancelled"
        finally:
            await mailbox.cancel_all()

    asyncio.run(scenario())


@pytest.mark.parametrize("stop", ["shutdown", "expiry", "cancel"])
def test_idle_poll_and_lifetime_notifications(tmp_path, monkeypatch, stop):
    async def scenario():
        manager, tasks, registry, member, mailbox = setup(tmp_path, monkeypatch)
        if stop == "expiry":
            monkeypatch.setattr(delegate, "_team_idle_timeout_seconds", lambda: 0.2)
        owner = tasks.start_run("owner", "first", session_id="retained-session")
        team = await call(registry, owner, "team", action="create", name="poll", goal="report")
        handle = await call(registry, owner, "team_spawn", team_id=team["id"], name="member",
                            goal="work", keep_alive=True, timeout=30)
        process = manager.get(handle["process"]["process_id"])
        member.release.set()
        try:
            polled = await asyncio.wait_for(call(registry, owner, "delegate_poll",
                                                 process_id=process.process_id, wait_ms=30000), 3)
            assert polled["status"] == "running"  # Process lifetime is retained.
            assert polled["worker"]["status"] == "idle"
            assert polled["episode_result"]["result"] == "verified episode 1"
            assert mailbox.drain(owner["id"]) == []
            restored = DelegateMailbox(manager, DelegateMailboxStore(tasks.path))
            assert restored.drain(owner["id"]) == []
            if stop == "shutdown":
                await call(registry, owner, "team_send", team_id=team["id"], to="member",
                           message="stop", kind="shutdown_request")
            if stop == "cancel":
                await mailbox.cancel_running(owner["id"])
            else:
                await asyncio.wait_for(process.task, 3)
            await asyncio.sleep(0)
            notifications = mailbox.drain(owner["id"])
            if stop == "cancel":
                assert len(notifications) == 1 and "Status: cancelled" in notifications[0]
            else:
                assert notifications == []
            assert mailbox.drain(owner["id"]) == []
            assert len(member.requests) == 1
        finally:
            await mailbox.cancel_all()

    asyncio.run(scenario())


def test_queued_message_counts_as_work_before_idle_member_consumes_it(tmp_path, monkeypatch):
    async def scenario():
        manager, tasks, registry, member, mailbox = setup(tmp_path, monkeypatch)
        owner = tasks.start_run("owner", "first", session_id="retained-session")
        team = await call(registry, owner, "team", action="create", name="queued", goal="report")
        handle = await call(registry, owner, "team_spawn", team_id=team["id"], name="member",
                            goal="work", keep_alive=True, timeout=30)
        process = manager.get(handle["process"]["process_id"])
        member.release.set()
        try:
            await mailbox.wait_and_drain(owner["id"], timeout=2)
            assert not mailbox.has_running(owner["id"])
            # Synchronous enqueue prevents the consumer from running before
            # the assertion, exercising the queued-but-still-idle boundary.
            AgentTeamStore(tasks.path).send_message(
                team["id"], team["lead_agent_id"], handle["agent"]["id"],
                body="next", kind="task_assignment",
            )
            assert mailbox.has_running(owner["id"])
            assert mailbox.idle_report(owner["id"], process.process_id) is None
            member.release.clear()
            await mailbox.cancel_task(owner["id"])
            assert manager.status(process) == "cancelled"
        finally:
            await mailbox.cancel_all()

    asyncio.run(scenario())


def test_failure_after_delivered_episode_is_still_reported(tmp_path, monkeypatch):
    async def scenario():
        manager, tasks, registry, member, mailbox = setup(tmp_path, monkeypatch)
        fail = asyncio.Event()
        receive = AgentTeamRuntime.receive_deliveries_async

        async def receive_or_fail(runtime, agent_id, *, after_seq):
            if runtime.store.get_agent(agent_id)["status"] == "idle":
                await fail.wait()
                raise RuntimeError("idle mailbox unavailable")
            return await receive(runtime, agent_id, after_seq=after_seq)

        monkeypatch.setattr(AgentTeamRuntime, "receive_deliveries_async", receive_or_fail)
        owner = tasks.start_run("owner", "first", session_id="retained-session")
        team = await call(registry, owner, "team", action="create", name="failure", goal="report")
        handle = await call(registry, owner, "team_spawn", team_id=team["id"], name="member",
                            goal="work", keep_alive=True, timeout=30)
        process = manager.get(handle["process"]["process_id"])
        member.release.set()
        try:
            reports = await mailbox.wait_and_drain(owner["id"], timeout=2)
            assert len(reports) == 1 and "verified episode 1" in reports[0]
            fail.set()
            await asyncio.wait_for(process.task, 3)
            await asyncio.sleep(0)
            errors = mailbox.drain(owner["id"])
            assert len(errors) == 1 and "Status: failed" in errors[0]
            assert "idle mailbox unavailable" in errors[0]
            assert mailbox.drain(owner["id"]) == []
        finally:
            await mailbox.cancel_all()

    asyncio.run(scenario())
