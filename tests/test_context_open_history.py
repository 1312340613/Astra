from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.runtime.context_open_history import without_context_open_placeholders
from agent.runtime.react import ReActAgent
from agent.runtime.tools.context_index import register_context_index_tools
from agent.runtime.tools.registry import ToolRegistry


HANDLE = "ctx:s:beef"
EVIDENCE = "private expanded evidence"


class Broker:
    def __init__(self):
        self.opens = []
        self.inspections = 0

    def inspect(self):
        self.inspections += 1
        return json.dumps({"handles": [HANDLE]})

    def open(self, handles, window):
        self.opens.append((handles, window))
        return EVIDENCE


def make_agent(llm=None, *, max_iterations=8):
    registry = ToolRegistry()
    broker = Broker()
    register_context_index_tools(registry, broker)  # type: ignore[arg-type]
    agent = ReActAgent(
        "agent", llm or object(), registry, max_iterations=max_iterations,
        progressive_tools=False, timing_log_enabled=False,
    )
    return agent, broker


def history_call(call_id, name, arguments):
    return {
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def tool_event(call_id, name, arguments):
    return {
        "type": "tool_calls",
        "calls": [{"id": call_id, "name": name, "arguments": json.dumps(arguments)}],
        "content": "", "finish_reason": "stop", "usage": None,
    }


def collect(agent):
    async def run():
        return [event async for event in agent.reply_stream(
            Msg(content=[ContentBlock.text("open the useful memory")]),
        )]
    return asyncio.run(run())


def test_open_arguments_remain_literal_in_history_and_raw_audit(tmp_path):
    agent, _broker = make_agent()
    agent.context.set_session(str(tmp_path / "session.json"))
    arguments = json.dumps({"handles": [HANDLE, "ctx:m:1234"], "window": 2})
    message = agent._assistant_message("opening", [{
        "id": "open-1", "name": "context_open", "arguments": arguments,
    }])
    agent.context.add_assistant_raw(message)

    assert message["tool_calls"][0]["function"]["arguments"] == arguments
    audit_path = tmp_path / ".artifacts" / "session.raw-tool-calls.jsonl"
    audit = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert audit["argument_persistence"] == "durable"
    assert audit["arguments"] == arguments
    assert "[redacted" not in repr(agent.context.messages)


@pytest.mark.parametrize("old_args", [
    {"handles": ["[redacted 10 chars]", "[redacted 19 chars]"]},
    {"handles": "[redacted 10 chars]"},
    {"request_local_placeholder": True, "argument_keys": ["handles"]},
])
@pytest.mark.parametrize("content", [None, "previous prose", [{"type": "text", "text": "previous prose"}]])
def test_legacy_projection_removes_only_bad_call_result_pairs(old_args, content):
    messages = [
        {"role": "user", "content": "the report mentions [redacted 10 chars]"},
        {"role": "assistant", "content": content, "reasoning_content": "prior reasoning", "tool_calls": [
            history_call("old-open", "context_open", old_args),
            history_call("sibling", "computer_act", {"actions": [{"text": "[redacted 32 chars]"}]}),
            history_call("valid-open", "context_open", {"handles": [HANDLE]}),
        ]},
        {"role": "tool", "tool_call_id": "old-open", "content": "old result"},
        {"role": "tool", "tool_call_id": "sibling", "content": "sibling result"},
        {"role": "tool", "tool_call_id": "valid-open", "content": "valid result"},
    ]
    original = copy.deepcopy(messages)

    projected = without_context_open_placeholders(messages)

    assert messages == original
    assert projected[0] == messages[0]
    assistant = projected[1]
    assert assistant["reasoning_content"] == "prior reasoning"
    assert assistant["tool_calls"] == messages[1]["tool_calls"][1:]
    assert "context_inspect" in str(assistant["content"])
    if content is not None:
        assert "previous prose" in str(assistant["content"])
    assert projected[2:] == messages[3:]


def test_legacy_projection_handles_a_call_without_siblings_and_is_idempotent():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            history_call("old-open", "context_open", {"handles": ["[redacted 10 chars]"]}),
        ]},
        {"role": "tool", "tool_call_id": "old-open", "content": "old result"},
    ]
    projected = without_context_open_placeholders(messages)
    assert len(projected) == 1
    assert "tool_calls" not in projected[0]
    assert "context_inspect" in projected[0]["content"]
    assert without_context_open_placeholders(projected) == projected


@pytest.mark.parametrize("arguments", [
    {"handles": ["[redacted 10 chars]"]},
    {"handles": [HANDLE, " [redacted 19 chars] "]},
    {"request_local_placeholder": True, "argument_keys": ["handles"]},
])
def test_model_placeholder_rejected_before_execution_with_current_handle_hint(arguments):
    agent, broker = make_agent()
    calls, failure = agent._validated_tool_calls(
        tool_event("bad-open", "context_open", arguments)["calls"], "stop", None,
    )
    assert calls is None
    assert failure is not None and failure.code == "invalid_arguments"
    assert failure.retryable
    assert "context_inspect" in failure.recovery_hint
    assert "10-character" in failure.recovery_hint
    assert "old-turn" in failure.recovery_hint
    assert broker.opens == []


def test_history_recovery_then_two_opens_replays_literal_handles_and_expires_evidence(tmp_path):
    class RecoveryLLM:
        config = SimpleNamespace(model="test", capabilities=frozenset({"assistant_prefill"}))

        def __init__(self):
            self.prompts = []

        async def chat_stream(self, messages, tools, **_kwargs):
            self.prompts.append(copy.deepcopy(messages))
            for message in messages:
                for call in message.get("tool_calls", []):
                    if call["function"]["name"] == "context_open":
                        assert "[redacted" not in call["function"]["arguments"]
            step = len(self.prompts)
            if step == 1:
                yield tool_event("bad", "context_open", {"handles": ["[redacted 10 chars]"]})
            elif step == 2:
                assert tools  # Invalid arguments must not enter assistant-prefill continuation.
                assert messages[-1]["role"] == "user"
                hint = messages[-1]["content"]
                assert "context_inspect" in hint
                assert "truncated" not in hint and "whole-file" not in hint
                yield tool_event("inspect", "context_inspect", {})
            elif step == 3:
                result = next(m for m in messages if m.get("tool_call_id") == "inspect")
                payload, _end = json.JSONDecoder().raw_decode(result["content"], result["content"].index("{"))
                handles = payload["handles"]
                yield tool_event("open-1", "context_open", {"handles": handles})
            elif step == 4:
                result = next(m for m in messages if m.get("tool_call_id") == "open-1")
                assert EVIDENCE in result["content"]
                call = next(c for m in messages for c in m.get("tool_calls", []) if c["id"] == "open-1")
                # Reproduce the real failure mode: copy the saved arguments.
                yield tool_event("open-2", "context_open", json.loads(call["function"]["arguments"]))
            else:
                assert step == 5
                results = {m.get("tool_call_id"): m.get("content") for m in messages if m.get("role") == "tool"}
                assert EVIDENCE not in results["open-1"]
                assert EVIDENCE in results["open-2"]
                yield {"type": "done", "content": "opened twice", "usage": None}

    llm = RecoveryLLM()
    agent, broker = make_agent(llm)
    agent.context.set_session(str(tmp_path / "session.json"))
    old = [
        {"role": "user", "content": "previous turn"},
        {"role": "assistant", "content": "", "tool_calls": [
            history_call("old-open", "context_open", {"handles": ["[redacted 10 chars]"]}),
        ]},
        {"role": "tool", "tool_call_id": "old-open", "content": "old redacted result"},
    ]
    agent.context.messages.extend(copy.deepcopy(old))

    events = collect(agent)

    assert broker.inspections == 1
    assert broker.opens == [([HANDLE], 2), ([HANDLE], 2)]
    assert len(llm.prompts) == 5
    assert any(e.get("code") == "invalid_arguments" for e in events)
    assert not any(e.get("code") == "tool_call_truncated" for e in events)
    assert not any("OUTPUT LIMIT" in e.get("message", "") for e in events)
    assert agent.context.messages[:3] == old
    assert EVIDENCE not in repr(agent.context.messages)
    assert agent._fresh_tool_context == {}
    audit = (tmp_path / ".artifacts" / "session.raw-tool-calls.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in audit.splitlines()]
    opens = [record for record in records if record["name"] == "context_open"]
    assert len(opens) == 2
    assert all(json.loads(record["arguments"]) == {"handles": [HANDLE]} for record in opens)


def test_placeholder_recovery_stops_after_two_retries():
    class StuckLLM:
        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools, **_kwargs):
            if not tools:
                yield {"type": "done", "content": "unable to open", "usage": None}
                return
            self.calls += 1
            yield tool_event(f"bad-{self.calls}", "context_open", {"handles": ["[redacted 10 chars]"]})

    llm = StuckLLM()
    agent, broker = make_agent(llm)
    events = collect(agent)
    assert llm.calls == 3
    assert broker.opens == []
    assert any(e.get("stage") == "stopped" for e in events)
