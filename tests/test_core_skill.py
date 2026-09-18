"""Runtime contracts for voluntary core reads; scripted replies are not model evals."""

import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.runtime.code_mode import register_run_code_tool
from agent.runtime.context import AgentContext
from agent.runtime.core_rules import (
    AGENT_BASE_PROMPT, AGENT_CORE_PROMPT, CORE_SKILL_GUIDE, CORE_SKILL_NAME,
    CORE_SKILL_VERSION, core_identity_prompt, core_skill_content,
)
from agent.runtime.llm import LLMConfig
from agent.runtime.prompts import get_prompt_profile, prompt_profiles
from agent.runtime.react import ReActAgent
from agent.runtime.skills import SkillStore
from agent.runtime.task_store import TaskStore
from agent.runtime.token_estimator import estimate_messages_tokens
from agent.runtime.tool_execution import ExecutionResult
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.runtime.tools.skills import register_skill_tools


DONE = {"type": "done", "content": "完成。", "usage": None}


class CaptureLLM:
    def __init__(self, replies=(), *, supported=True):
        self.config = LLMConfig(model="deepseek-flash")
        if not supported:
            self.config.base_url = "https://custom.example/v1"
            self.config.capabilities = frozenset()
        self.requests = []
        self.replies = iter(replies)

    estimate_tokens = staticmethod(estimate_messages_tokens)

    async def chat_stream(self, messages, tools, **kwargs):
        self.requests.append(copy.deepcopy({"messages": messages, "tools": tools}))
        yield next(self.replies, DONE)


def make_agent(tmp_path, replies=(), *, supported=True, **kwargs):
    store = SkillStore(tmp_path / "skills")
    registry = ToolRegistry()
    register_skill_tools(registry, store)
    registry.register(ToolDef("inspect_project", "Inspect the project", {"type": "object", "properties": {}},
                              lambda: "project verified", risk="read"))
    agent = ReActAgent(
        "core-skill-test", CaptureLLM(replies, supported=supported), registry, skill_store=store,
        system_prompt=kwargs.pop("system_prompt", get_prompt_profile("lyra").system_prompt()),
        timing_log_enabled=False, query_profile_enabled=False, max_iterations=5, **kwargs,
    )
    return agent


async def say(agent, text):
    return await agent.reply(Msg(content=[ContentBlock.text(text)]))


def call(name, args=None, *, call_id="read-core"):
    return {"type": "tool_calls", "content": "", "usage": None, "calls": [
        {"id": call_id, "name": name, "arguments": json.dumps(args or {})},
    ]}


def assert_prefix(first, following):
    assert first["tools"] == following["tools"]
    assert following["messages"][:len(first["messages"])] == first["messages"]


def test_public_persona_and_original_core_match_in_both_modes(monkeypatch):
    baseline = json.loads((Path(__file__).parent / "fixtures/core_prompt_migration.json").read_text())
    assert hashlib.sha256(AGENT_CORE_PROMPT.encode()).hexdigest() == baseline["core_sha256"]
    monkeypatch.setenv("ASTRA_CORE_RULES_MODE", "full")
    full = {name: p.system_prompt() for name, p in prompt_profiles().items()}
    assert {name: hashlib.sha256(prompt.encode()).hexdigest() for name, prompt in full.items()} == baseline["profile_sha256"]
    monkeypatch.setenv("ASTRA_CORE_RULES_MODE", "skill")
    for name, profile in prompt_profiles().items():
        assert profile.system_prompt() == full[name].replace(AGENT_CORE_PROMPT, core_identity_prompt(), 1)
    assert all(line in AGENT_CORE_PROMPT.splitlines() for line in AGENT_BASE_PROMPT.splitlines())
    assert "工作原则：" not in core_identity_prompt()
    assert "基本原则：" in core_identity_prompt()
    assert CORE_SKILL_GUIDE in core_identity_prompt()


def test_core_precedes_project_skills_and_cannot_be_shadowed_or_modified(tmp_path):
    store = SkillStore(tmp_path)
    store.create("aaa", "---\nname: aaa\ndescription: project skill\n---\nLocal procedure")
    shadow = tmp_path / CORE_SKILL_NAME
    shadow.mkdir()
    (shadow / "SKILL.md").write_text("---\nname: astra-core\ndescription: fake\n---\nShadow")
    assert [s["name"] for s in store.list()] == [CORE_SKILL_NAME, "aaa"]
    assert store.catalog_prompt().index("- astra-core:") < store.catalog_prompt().index("- aaa:")
    assert AGENT_CORE_PROMPT not in store.catalog_prompt()
    assert store.view("ASTRA-CORE") == core_skill_content()
    assert store.view(CORE_SKILL_NAME, "astra.md") == core_skill_content()
    assert CORE_SKILL_VERSION in store.view(CORE_SKILL_NAME)
    mutations = [
        lambda: store.create(CORE_SKILL_NAME, "content"),
        lambda: store.patch(CORE_SKILL_NAME, "Lyra", "Changed"),
        lambda: store.write_file(CORE_SKILL_NAME, "references/x.md", "content"),
        lambda: store.restore_file(CORE_SKILL_NAME, "SKILL.md", None),
    ]
    for mutation in mutations:
        with pytest.raises(ValueError, match="read-only built-in"):
            mutation()
    for path in ("../astra.md", "/etc/passwd", "references/x.md"):
        with pytest.raises(ValueError, match="only exposes"):
            store.view(CORE_SKILL_NAME, path)


@pytest.mark.parametrize("supported", [True, False])
def test_model_requested_read_is_only_appended_once_and_preserves_prefix(tmp_path, supported):
    async def scenario():
        agent = make_agent(tmp_path, [DONE, DONE, call("skill_view", {"name": CORE_SKILL_NAME}),
                                     call("inspect_project", call_id="inspect"), DONE, DONE], supported=supported)
        for text in ("你好", "忙了一整天，想跟你吐槽两句又不知道从哪里开始", "检查这个项目", "谢谢，聊点别的"):
            await say(agent, text)
        requests = agent.llm.requests
        assert len(requests) == 6
        for earlier, later in zip(requests, requests[1:]):
            assert_prefix(earlier, later)
        assert all("工作原则：" not in str(r["messages"]) for r in requests[:3])
        for request in requests[3:]:
            assert sum(AGENT_CORE_PROMPT in str(m.get("content", "")) for m in request["messages"]) == 1
            assert [m["role"] for m in request["messages"] if AGENT_CORE_PROMPT in str(m.get("content", ""))] == ["tool"]
            assert sum(m["role"] == "system" for m in request["messages"]) == 1
        assert [c["id"] for m in agent.context.messages for c in m.get("tool_calls", [])] == ["read-core", "inspect"]

    asyncio.run(scenario())


def test_omitted_core_read_is_not_hidden_by_runtime_injection_or_replanning(tmp_path):
    agent = make_agent(tmp_path, [call("inspect_project", call_id="direct"), DONE])
    asyncio.run(say(agent, "检查项目"))
    assert len(agent.llm.requests) == 2
    assert "project verified" in str(agent.context.messages)
    assert AGENT_CORE_PROMPT not in str(agent.llm.requests)


def test_source_changes_keep_evidence_without_repeated_workflow_prompts(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_WORKDIR", str(tmp_path))
    store = TaskStore(tmp_path / "tasks.db")
    task = store.start_run("test", "update source", session_id="workflow-test")
    agent = make_agent(tmp_path, [
        call("edit_probe", {"path": "main.py"}, call_id="edit"),
        call("check_project", call_id="check"),
        DONE,
    ], task_store=store)

    def edit_probe(path):
        (tmp_path / path).write_text("value = 1\n", encoding="utf-8")
        return "updated"

    agent.tools.register(ToolDef(
        "edit_probe", "Update test source",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        edit_probe, risk="write",
    ))
    agent.tools.register(ToolDef(
        "check_project", "Return a scripted check receipt", {"type": "object", "properties": {}},
        lambda: ExecutionResult("scripted check passed", {"exit_code": 0}), risk="execute",
    ))
    asyncio.run(agent.reply(Msg(
        content=[ContentBlock.text("修改文件并完成相关检查")], metadata={"task_id": task["id"]},
    )))

    requests = agent.llm.requests
    assert len(requests) == 3
    for earlier, later in zip(requests, requests[1:]):
        assert_prefix(earlier, later)
    assert all(sum(m["role"] == "system" for m in r["messages"]) == 1 for r in requests)
    assert (tmp_path / "main.py").read_text(encoding="utf-8") == "value = 1\n"
    contract = store.get_task(task["id"])["verification"]
    assert contract["mutated_paths"] == ["main.py"]
    assert len(contract["checks"]) == 1
    assert contract["checks"][0]["execution_status"] == "completed"
    assert contract["checks"][0]["exit_code"] == 0
    assert contract["status"] == "unverified"  # A receipt alone does not certify the change.


def test_code_mode_can_read_core_through_real_program_transport(tmp_path):
    code = 'result = await tools.skill_view(name="astra-core")\nprint(result)'
    agent = make_agent(tmp_path, [call("run_code", {"code": code, "description": "Read core rules"}), DONE], code_mode="code")
    register_run_code_tool(agent.tools, lambda: agent)
    asyncio.run(say(agent, "检查这个项目"))
    assert len(agent.llm.requests) == 2
    body = str(agent.llm.requests[-1]["messages"])
    assert "工作原则：" in body and "Skill 纪律：" in body
    assert "Active skill contract: astra-core" not in body
    assert CORE_SKILL_NAME in agent._active_skill_names


def test_restore_keeps_loaded_tool_result_and_same_wire_prefix(tmp_path):
    async def scenario():
        path = str(tmp_path / "session.json")
        agent = make_agent(tmp_path, [call("skill_view", {"name": CORE_SKILL_NAME}), DONE])
        agent.context.set_session(path)
        await say(agent, "检查项目")
        agent.context.save()
        previous = agent.llm.requests[-1]
        for _ in range(2):
            restored = make_agent(tmp_path)
            restored.context.set_session(path)
            assert restored.context.load()
            await say(restored, "随便聊聊")
            assert_prefix(previous, restored.llm.requests[-1])
            restored.context.save()
            previous = restored.llm.requests[-1]
            assert "work_prompt" not in restored.context._session_store.load()

    asyncio.run(scenario())


def test_old_core_projection_is_retired_once(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_CORE_RULES_MODE", "full")
    old_prompt = get_prompt_profile("lyra").system_prompt()
    monkeypatch.setenv("ASTRA_CORE_RULES_MODE", "skill")
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"system_prompt": old_prompt,
                               "work_prompt": {"version": 1, "mode": "chat"},
                               "system_prompt_projection": {"version": 1, "head": "obsolete"},
                               "messages": [{"role": "user", "content": "你好"}]}))
    context = AgentContext()
    context.set_session(str(path))
    # Real sessions retain an older snapshot after later header-only saves.
    context._session_store.snapshot_path.write_text(path.read_text())
    context._session_store.header_path.write_text(path.read_text())
    assert context.load()
    assert context.system_projection.state == {}
    assert "工作原则：" not in context.system_prompt
    context.system_projection.state = {"new": "projection"}
    context.save()
    assert "work_prompt" in context._session_store.snapshot_path.read_text()
    # The old legacy file/snapshot still exists. The new header wins.
    restored = AgentContext()
    restored.set_session(str(path))
    assert restored.load()
    assert restored.system_projection.state == {"new": "projection"}


def test_compacted_summary_cannot_reinject_core_but_model_can_reread(tmp_path):
    async def scenario():
        agent = make_agent(tmp_path, [call("skill_view", {"name": CORE_SKILL_NAME}), DONE])
        await say(agent, "检查项目")
        agent.context.messages = [{"role": "user", "content": "[Summary] Previously read astra-core; continue work."}]
        # Simulate body removal by compaction, retaining the active-skill name.
        agent.context._rebuild_token_cache()
        agent.llm.replies = iter([call("skill_view", {"name": CORE_SKILL_NAME}, call_id="reread"), DONE])
        await say(agent, "继续")
        before, after = agent.llm.requests[-2:]
        assert AGENT_CORE_PROMPT not in str(before["messages"])
        assert sum(AGENT_CORE_PROMPT in str(m.get("content", "")) for m in after["messages"]) == 1
        assert "project verified" not in str(after["messages"])

    asyncio.run(scenario())


def test_minimal_and_custom_prompts_stay_custom(tmp_path):
    async def scenario():
        for minimal in (True, False):
            agent = make_agent(tmp_path, system_prompt="custom instructions", minimal_mode=minimal)
            await say(agent, "检查项目")
            head = agent.llm.requests[0]["messages"][0]["content"]
            assert head.startswith("custom instructions")
            assert CORE_SKILL_GUIDE not in head and AGENT_CORE_PROMPT not in head
            if minimal:
                assert "available-skills" not in head

    asyncio.run(scenario())


def test_retired_flag_does_not_change_new_prompt(monkeypatch):
    monkeypatch.setenv("ASTRA_CORE_RULES_MODE", "skill")
    prompt = core_identity_prompt()
    for value in ("0", "1"):
        monkeypatch.setenv("ASTRA_LAZY_WORK_RULES", value)
        assert core_identity_prompt() == prompt


def test_date_anchor_survives_midnight_and_restore_without_changing_user_text(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from agent.runtime import time_utils

    now = [datetime(2026, 9, 10, 23, 59, tzinfo=timezone.utc)]
    monkeypatch.setattr(time_utils, "current_datetime", lambda: now[0])
    context = AgentContext(system_prompt=get_prompt_profile("lyra").system_prompt())
    path = str(tmp_path / "dated.json")
    context.set_session(path)
    context.add_user("今天有点累")
    initial = context.get_prompt()[1]
    assert "Local date: 2026-09-10" in initial["content"]
    context.save()
    now[0] = datetime(2026, 9, 11, 0, 1, tzinfo=timezone.utc)
    restored = AgentContext()
    restored.set_session(path)
    assert restored.load()
    restored.add_user("今天好多了")
    assert restored.get_prompt()[1] == initial
    assert "Local date: 2026-09-11" in restored.get_prompt()[-1]["content"]
    assert restored.messages[0]["content"] == "今天有点累"
    assert "[SYSTEM-SUPPLIED DATE ANCHOR]" not in json.dumps(restored.messages)
    assert "relative_date" not in restored.get_prompt()[1]


def test_profiler_fingerprints_projected_wire_request(monkeypatch, tmp_path):
    from agent.runtime.query_profiler import QueryProfiler

    captured = []
    original = QueryProfiler.set_request

    def record(self, messages, tools, active_skills):
        captured.append(copy.deepcopy({"messages": messages, "tools": tools}))
        return original(self, messages, tools, active_skills)

    monkeypatch.setattr(QueryProfiler, "set_request", record)
    agent = make_agent(tmp_path)
    asyncio.run(say(agent, "你好"))
    assert captured[-1] == agent.llm.requests[0]
    assert AGENT_BASE_PROMPT in captured[-1]["messages"][0]["content"]
