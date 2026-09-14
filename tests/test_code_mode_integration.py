"""Integration: run_code inside the real ReAct loop dispatches sub-calls."""

import asyncio

from agent.core.msg import ContentBlock, Msg
from agent.runtime.code_mode import register_run_code_tool
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def run(coro):
    return asyncio.run(coro)


def test_run_code_dispatches_through_real_react_loop():
    """The model calls run_code; its program calls echo via the real path."""

    class RunCodeThenDoneLLM:
        def __init__(self):
            self.calls = []

        async def chat_stream(self, messages, tools):
            self.calls.append((messages, tools))
            if len(self.calls) == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "call-run-1",
                        "name": "run_code",
                        "arguments": '{"code": "return await tools.echo(message=\'hi\')", "description": "echo hi"}',
                    }],
                    "content": "",
                    "reasoning_content": "",
                    "usage": None,
                }
            else:
                yield {"type": "done", "content": "done", "usage": None}

    registry = ToolRegistry()
    registry.register(ToolDef(
        "echo",
        "echo",
        {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        lambda message="": "echo-result", group="core",
    ))
    llm = RunCodeThenDoneLLM()
    agent = ReActAgent(
        "agent",
        llm,  # type: ignore[arg-type]
        registry,
        system_prompt="system",
        max_iterations=3,
        code_mode="code",
    )
    register_run_code_tool(registry, agent_getter=lambda: agent)

    run(agent.reply(Msg(content=[ContentBlock.text("run it")], id="turn-1")))

    assert len(llm.calls) == 2
    # run_code was in the first turn's available tools
    first_tools = {t["function"]["name"] for t in llm.calls[0][1]}
    assert "run_code" in first_tools
    # The echo sub-call result reached the second turn's history.
    second_messages = llm.calls[1][0]
    joined = "\n".join(str(m.get("content", "")) for m in second_messages)
    assert "echo-result" in joined
