import asyncio
import json

from agent.core.msg import ContentBlock, Msg
from agent.runtime.react import ReActAgent
from agent.runtime.task_resume import budget_resume_candidate, resume_prompt
from agent.runtime.task_store import TaskStore
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def test_budget_candidate_never_resumes_other_or_superseded_tasks(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    task = store.start_run("r1", "original", session_id="s1")
    store.checkpoint(task["id"], {"resume_kind": "iteration_budget"})
    store.finish_run(task["id"], "interrupted")
    assert budget_resume_candidate(store, "s1", "继续吧")["id"] == task["id"]
    assert budget_resume_candidate(store, "s2", "继续") is None
    assert budget_resume_candidate(store, "s1", "继续做另一件事") is None
    new = store.start_run("r2", "new task", session_id="s1")
    store.finish_run(new["id"], "completed")
    assert budget_resume_candidate(store, "s1", "继续") is None


def test_resume_prompt_retains_process_refs_and_unknown_outcomes():
    task = {"id": "t", "input_text": "run notebook", "checkpoint": {
        "phase": "turn_stopped", "iteration": 100, "final_summary": "cell 3 pending"},
        "steps": [{"id": "x", "kind": "tool", "name": "execute_shell", "status": "completed",
                   "output": {"output": json.dumps({"process_id": "p123", "status": "running"})}},
                  {"id": "y", "kind": "tool", "name": "write_file", "status": "running"}]}
    prompt = resume_prompt(task)
    assert "p123" in prompt and "cell 3 pending" in prompt
    assert "not blind replay" in prompt and '"status":"running"' in prompt


def test_iteration_checkpoint_is_durable_before_done_and_resumes_same_run(tmp_path):
    async def scenario():
        class LLM:
            class Config:
                model = "fixture"
                capabilities = frozenset()
            config = Config()
            async def chat_stream(self, messages, tools):
                if tools:
                    yield {"type": "tool_calls", "calls": [
                        {"id": "call1", "name": "probe", "arguments": "{}"}],
                        "content": "", "reasoning_content": ""}
                else:
                    yield {"type": "done", "content": "Probe done; next step pending."}

        store = TaskStore(tmp_path / "tasks.db")
        task = store.start_run("r", "do two steps", session_id="s")
        registry = ToolRegistry()
        calls = []
        registry.register(ToolDef("probe", "probe", {"type": "object", "properties": {}},
                                  lambda: calls.append(1) or '{"process_id":"p1","status":"running"}'))
        agent = ReActAgent("fixture", LLM(), registry, max_iterations=1,
                           system_prompt="system", task_store=store)
        msg = Msg(content=[ContentBlock.text("do two steps")], metadata={"task_id": task["id"]})
        saw_done = False
        async for event in agent._run_react_loop(msg, emit_events=True):
            if event["type"] == "done":
                saved = store.get_task(task["id"])
                assert saved["checkpoint"]["resume_kind"] == "iteration_budget"
                assert saved["checkpoint"]["final_summary"]
                saw_done = True
        assert saw_done and calls == [1]
        store.finish_run(task["id"], "interrupted")
        resumed = store.prepare_resume(task["id"])
        assert resumed["id"] == task["id"] and resumed["resume_count"] == 1
        assert "p1" in resume_prompt(resumed)
        followup = Msg(content=[ContentBlock.text(resume_prompt(resumed))],
                       metadata={"task_id": task["id"]})
        async for _ in agent._run_react_loop(followup, emit_events=True):
            pass
        assert calls == [1]  # Same task journal reuses the completed tool result.
    asyncio.run(scenario())
