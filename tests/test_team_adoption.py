import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent.runtime.agent_team import AgentTeamStore
from agent.runtime.task_store import TaskStore
from agent.runtime.tools import delegate
from agent.runtime.tools.delegate import DelegateMailbox, DelegateMailboxStore, register_delegate_tools
from agent.runtime.tools.processes import ProcessManager
from agent.runtime.tools.registry import ToolRegistry


async def call(registry, task, tool, **args):
    result = await registry.execute(tool, args, task_id=task["id"])
    assert not result["error"], result["error"]
    return json.loads(result["output"])


class PausedMember:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.team_id = ""

    async def chat(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            await self.release.wait()
            return {"content": "", "tool_calls": [{
                "id": "member-message",
                "name": "team_send", "arguments": json.dumps({
                    "team_id": self.team_id, "to": "lead", "message": "still connected after adoption",
                }),
            }]}
        return {"content": "finished retained work", "tool_calls": []}


async def wait_status(store, agent_id, status):
    async with asyncio.timeout(3):
        while store.get_agent(agent_id)["status"] != status:
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("keep_alive", [False, True])
def test_new_turn_adopts_live_member_and_old_cleanup_cannot_cancel_it(tmp_path, monkeypatch, keep_alive):
    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        tasks = TaskStore(tmp_path / "tasks.db")
        old = tasks.start_run("old", "old turn", session_id="session")
        llm = PausedMember()
        registry = ToolRegistry()
        mailbox = register_delegate_tools(
            registry, llm_getter=lambda: llm, task_store=tasks, session_id_getter=lambda: "session",
        )
        team = await call(registry, old, "team", action="create", name="retained", goal="continue")
        llm.team_id = team["id"]
        handle = await call(registry, old, "team_spawn",
                          team_id=team["id"], name="member", goal="work", keep_alive=keep_alive, timeout=30)
        process_id = handle["process"]["process_id"]
        process = manager.get(process_id)
        agent_id = handle["agent"]["id"]
        await llm.started.wait()
        team_store = AgentTeamStore(tasks.path)
        job = team_store.create_task(team["id"], title="ongoing")
        lease = team_store.claim_task(team["id"], job["id"], agent_id, lease_seconds=300)
        # Exercise idle adoption too; the other variant remains in an LLM call.
        if keep_alive:
            llm.release.set()
            await wait_status(team_store, agent_id, "idle")
        tasks.finish_run(old["id"], "completed")
        new = tasks.start_run("new", "continue", session_id="session")
        adopted = await call(registry, new, "team", action="resume", team_id=team["id"])
        assert adopted["owner_task_id"] == new["id"]
        assert process.task_id == old["id"]  # immutable provenance
        assert team_store.get_task(job["id"])["lease_until"] == lease["lease_until"]
        assert team_store.get_task(job["id"])["version"] == lease["version"]
        listed = await call(registry, new, "delegate_list")
        assert process_id in {item["process_id"] for item in listed}
        assert await call(registry, old, "delegate_list") == []
        denied = await registry.execute("delegate_cancel", {"process_id": process_id}, task_id=old["id"])
        assert "another task" in denied["error"]
        await mailbox.cancel_task(old["id"])
        assert manager.status(process) == "running"
        await call(registry, new, "delegate_read", process_id=process_id)
        llm.release.set()
        if keep_alive:
            await call(registry, new, "team_send", team_id=team["id"], to="member",
                       message="continue with next assignment", kind="task_assignment")
            async with asyncio.timeout(3):
                while llm.calls < 3:
                    await asyncio.sleep(0.01)
            await wait_status(team_store, agent_id, "idle")
            stopped = await call(registry, new, "team", action="stop", team_id=team["id"])
            assert stopped["status"] == "stopped"
            assert manager.status(process) == "cancelled"
        else:
            await manager.wait(process, 3000)
            await asyncio.sleep(0)
            inbox = await call(registry, new, "team_inbox", team_id=team["id"])
            assert inbox["messages"][0]["message"] == "still connected after adoption"
            assert mailbox.drain_for(old["id"], "session") == []
            results = mailbox.drain(new["id"])
            assert len(results) == 1 and "finished retained work" in results[0]
            assert mailbox.drain(new["id"]) == []
            restored = DelegateMailbox(manager, DelegateMailboxStore(tasks.path))
            assert restored.drain(new["id"]) == []
            rows = DelegateMailboxStore(tasks.path).rows()
            assert len(rows) == 1 and rows[0]["owner_task_id"] == new["id"]
            assert rows[0]["delivered"] is True
    asyncio.run(scenario())


def test_concurrent_adoption_has_one_winner_and_cannot_cross_sessions(tmp_path):
    tasks = TaskStore(tmp_path / "tasks.db")
    old = tasks.start_run("old", "old", session_id="session")
    teams = AgentTeamStore(tasks.path)
    team = teams.create_team(old["id"], session_id="session", name="race", goal="continue")
    tasks.finish_run(old["id"], "completed")
    contenders = [tasks.start_run(str(i), "new", session_id="session") for i in range(2)]

    def claim(run):
        try:
            return AgentTeamStore(tasks.path).resume_team(
                team["id"], new_owner_task_id=run["id"], session_id="session",
                expected_owner_task_id=old["id"],
            )
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, contenders))
    assert sum(isinstance(result, dict) for result in outcomes) == 1
    with pytest.raises(ValueError, match="another session"):
        teams.resume_team(team["id"], new_owner_task_id="foreign", session_id="foreign")


def test_cancelling_adoption_wait_settles_owner_before_old_cleanup(tmp_path, monkeypatch):
    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        tasks = TaskStore(tmp_path / "tasks.db")
        old = tasks.start_run("old", "old", session_id="session")
        registry = ToolRegistry()
        llm = PausedMember()
        mailbox = register_delegate_tools(registry, llm_getter=lambda: llm, task_store=tasks,
                                          session_id_getter=lambda: "session")
        team = await call(registry, old, "team", action="create", name="cancel-race", goal="continue")
        llm.team_id = team["id"]
        handle = await call(registry, old, "team_spawn", team_id=team["id"], name="member", goal="work", timeout=30)
        process = manager.get(handle["process"]["process_id"])
        await llm.started.wait()
        tasks.finish_run(old["id"], "completed")
        new = tasks.start_run("new", "continue", session_id="session")
        entered, release = threading.Event(), threading.Event()
        original_resume = AgentTeamStore.resume_team

        def paused_resume(self, *args, **kwargs):
            entered.set()
            assert release.wait(3)
            return original_resume(self, *args, **kwargs)

        monkeypatch.setattr(AgentTeamStore, "resume_team", paused_resume)
        adopting = asyncio.create_task(call(registry, new, "team", action="resume", team_id=team["id"]))
        assert await asyncio.to_thread(entered.wait, 2)
        cleanup = asyncio.create_task(mailbox.cancel_task(old["id"]))
        adopting.cancel()
        await asyncio.sleep(0)
        assert not cleanup.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await adopting
        await cleanup
        assert AgentTeamStore(tasks.path).get_team(team["id"])["owner_task_id"] == new["id"]
        assert manager.status(process) == "running"
        llm.release.set()
        await manager.wait(process, 3000)
        await asyncio.sleep(0)
        assert len(mailbox.drain(new["id"])) == 1
        assert mailbox.drain(new["id"]) == []
    asyncio.run(scenario())


def test_restart_marks_missing_member_interrupted_without_replaying(tmp_path, monkeypatch):
    async def scenario():
        tasks = TaskStore(tmp_path / "tasks.db")
        old = tasks.start_run("old", "old", session_id="session")
        teams = AgentTeamStore(tasks.path)
        team = teams.create_team(old["id"], session_id="session", name="recover", goal="recover")
        helper = teams.register_agent(team["id"], name="lost", role="worker", mode="worker",
                                      parent_agent_id=team["lead_agent_id"])
        teams.bind_agent(helper["id"], "no-live-handle")
        tasks.finish_run(old["id"], "interrupted")
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        registry = ToolRegistry()
        def never_start():
            raise AssertionError("adoption must not replay work")
        register_delegate_tools(registry, llm_getter=never_start, task_store=tasks,
                                session_id_getter=lambda: "session")
        new = tasks.start_run("new", "continue", session_id="session")
        resumed = await call(registry, new, "team", action="resume", team_id=team["id"])
        assert next(a for a in resumed["agents"] if a["id"] == helper["id"])["status"] == "interrupted"
        assert manager.list() == []
    asyncio.run(scenario())
