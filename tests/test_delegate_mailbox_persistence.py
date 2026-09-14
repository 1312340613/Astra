import asyncio
from types import SimpleNamespace

from agent.runtime.tools.delegate import (
    DelegateConcurrencyGate,
    DelegateMailbox,
    DelegateMailboxStore,
)


class FakeManager:
    def __init__(self):
        self.processes = {}

    def get(self, process_id):
        try:
            return self.processes[process_id]
        except KeyError as exc:
            raise ValueError("unknown process") from exc

    @staticmethod
    def status(process):
        return "running" if process.task is not None and not process.task.done() else "completed"


def test_delegate_completion_survives_mailbox_recreation_and_delivers_once(tmp_path):
    async def scenario():
        manager = FakeManager()
        store = DelegateMailboxStore(tmp_path / "tasks.db")
        mailbox = DelegateMailbox(manager, store)

        async def finish():
            return {"worker_status": "completed", "result": "durable evidence"}

        process = SimpleNamespace(
            task_id="owner-1",
            process_id="process-1",
            label="inspect durable state",
            metadata={"session_transcript_path": "transcript.jsonl"},
            task=asyncio.create_task(finish()),
        )
        manager.processes[process.process_id] = process
        mailbox.track(process, session_id="session-1")
        await process.task
        await asyncio.sleep(0)

        restarted = DelegateMailbox(manager, DelegateMailboxStore(tmp_path / "tasks.db"))
        delivered = restarted.drain_for("new-owner", "session-1")
        assert len(delivered) == 1
        assert "durable evidence" in delivered[0]
        assert "transcript.jsonl" in delivered[0]

        restarted_again = DelegateMailbox(manager, DelegateMailboxStore(tmp_path / "tasks.db"))
        assert restarted_again.drain_for("new-owner", "session-1") == []

    asyncio.run(scenario())


def test_delegate_concurrency_gate_preserves_capacity_for_another_owner():
    async def scenario():
        gate = DelegateConcurrencyGate(global_limit=3, owner_limit=2)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2
        entered = []
        release = asyncio.Event()

        async def run(owner, label):
            await gate.acquire(owner, deadline)
            entered.append(label)
            await release.wait()
            gate.release(owner)

        first = asyncio.create_task(run("owner-a", "a1"))
        second = asyncio.create_task(run("owner-a", "a2"))
        blocked = asyncio.create_task(run("owner-a", "a3"))
        await asyncio.sleep(0.02)
        other = asyncio.create_task(run("owner-b", "b1"))
        await asyncio.sleep(0.02)

        assert set(entered) == {"a1", "a2", "b1"}
        assert not blocked.done()
        release.set()
        await asyncio.gather(first, second, blocked, other)
        assert entered[-1] == "a3"

    asyncio.run(scenario())
