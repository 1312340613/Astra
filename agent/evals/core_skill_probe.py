"""Opt-in live model probe of core-Skill decisions on isolated fixture tools.

Run: python -m agent.evals.core_skill_probe --live --output /path/to/report.json
Uses the configured official DeepSeek account. No browser, desktop or project
file is modified; fixture observations measure routing, not GUI acceptance.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import time
from typing import cast

from agent.core.msg import ContentBlock, Msg
from agent.runtime.code_mode import register_run_code_tool
from agent.runtime.core_rules import AGENT_CORE_PROMPT, CORE_SKILL_NAME
from agent.runtime.llm import LLMClient, LLMConfig
from agent.runtime.prompts import get_prompt_profile
from agent.runtime.react import ReActAgent
from agent.runtime.skills import SkillStore
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.runtime.tools.skills import register_skill_tools


class ObservedLLM:
    def __init__(self, client: LLMClient):
        self.client = client
        self.config = client.config
        self.estimate_tokens = client.estimate_tokens
        self.requests: list[dict] = []

    async def chat_stream(self, messages, tools, **kwargs):
        previous = self.requests[-1] if self.requests else None
        record = {"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools),
                  "first_update_s": None, "duration_s": None, "usage": {}}
        record["same_system"] = previous is None or messages[0] == previous["messages"][0]
        record["same_tools"] = previous is None or tools == previous["tools"]
        record["extends_prefix"] = previous is None or messages[:len(previous["messages"])] == previous["messages"]
        self.requests.append(record)
        started = time.monotonic()
        try:
            async for event in self.client.chat_stream(messages, tools, **kwargs):
                if record["first_update_s"] is None:
                    record["first_update_s"] = round(time.monotonic() - started, 3)
                if event.get("usage"):
                    record["usage"] = event["usage"]
                yield event
        finally:
            record["duration_s"] = round(time.monotonic() - started, 3)


class ProbeAgent(ReActAgent):
    def __init__(self, *args, **kwargs):
        self.calls: list[dict] = []
        super().__init__(*args, **kwargs)

    async def _execute_tool_call(self, tc, task_id=None, progress_callback=None, trace_ctx=None):
        record = {"name": tc["name"], "arguments": json.loads(tc.get("arguments") or "{}"),
                  "request_index": len(cast(ObservedLLM, self.llm).requests) - 1,
                  "error": "Call did not complete"}
        self.calls.append(record)
        result = await super()._execute_tool_call(tc, task_id, progress_callback, trace_ctx)
        record["error"] = result.get("error", "")
        return result


def fixture_agent(root: Path, client: LLMClient, *, code_mode="native") -> ProbeAgent:
    store = SkillStore(root / "skills")
    for name, description in (
        ("fixture-code", "检查 fixture Python 代码，分析缺陷和边界条件"),
        ("fixture-browser", "读取测试浏览器页面和复选框状态"),
        ("fixture-desktop", "读取 Astra Fixture 桌面窗口和控件状态"),
    ):
        store.create(name, f"---\nname: {name}\ndescription: {description}\n---\n"
                     "按当前工具定义取得观察结果，依据返回的字段回答。\n")
    registry = ToolRegistry(artifact_dir=root / "tool-results")
    register_skill_tools(registry, store)

    def read_file(path: str) -> str:
        if path not in {"/fixture/sample.py", "sample.py"}:
            raise ValueError("The probe only exposes /fixture/sample.py")
        return "def sum_positive(values):\n    return sum(x for x in values if x != 0)\n"

    fixtures = (
        ("read_file", "Read the isolated fixture /fixture/sample.py.",
         {"path": {"type": "string"}}, read_file),
        ("browser_snapshot", "Observe the isolated preferences page; includes checked state.", {},
         lambda: json.dumps({"url": "fixture://preferences", "controls": [
             {"ref": "mail", "role": "checkbox", "name": "邮件通知", "checked": False}]})),
        ("computer_apps", "List isolated fixture applications.", {},
         lambda: json.dumps({"apps": [{"app": "Astra Fixture", "bundle_id": "com.astra.fixture"}]})),
        ("computer_get_app_state", "Read the isolated Astra Fixture window; no real desktop access.",
         {"app": {"type": "string"}},
         lambda app: json.dumps({"app": app, "controls": [{"role": "button", "name": "保存", "enabled": True}]})),
    )
    for name, description, properties, fn in fixtures:
        registry.register(ToolDef(name, description,
                                  {"type": "object", "properties": properties, "required": list(properties)},
                                  fn, risk="read", approval="never", idempotent=True))
    agent = ProbeAgent("core-skill-probe", ObservedLLM(client), registry,  # type: ignore[arg-type]
                       skill_store=store, system_prompt=get_prompt_profile("lyra").system_prompt(),
                       max_iterations=6, timing_log_enabled=False, query_profile_enabled=False,
                       turn_timeout_seconds=120, code_mode=code_mode)
    agent.tool_allowlist = {"skills_list", "skill_view", *(item[0] for item in fixtures)}
    if code_mode == "code":
        register_run_code_tool(registry, lambda: agent)
        agent.tool_allowlist.add("run_code")
    # Use fixture project context, not the developer's current workspace rules.
    agent._source_tracking_workdir = lambda: root
    return agent


async def run_probe(output: Path) -> dict:
    from agent.cli.environment import load_project_env
    from agent.cli.model_catalog import configured_model_catalog

    load_project_env(Path.cwd())
    os.environ["ASTRA_CORE_RULES_MODE"] = "skill"
    entry = configured_model_catalog().resolve("deepseek-flash")
    if entry is None or entry.base_url.rstrip("/") not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}:
        raise RuntimeError("Configured official DeepSeek Flash profile required")
    profile = entry.profile
    key = profile.api_key() or os.getenv("LLM_API_KEY", "")
    if not key:
        raise RuntimeError("Configured DeepSeek API key required")
    generation = {**profile.generation_settings(), "max_tokens": 4096}
    config = LLMConfig(provider=profile.provider, model=entry.model_id, api_key=key,
                       base_url=entry.base_url, capabilities=profile.capabilities,
                       overall_timeout=60, idle_timeout=25, max_retries=0, **generation)
    client = LLMClient(config)
    report: dict = {"model": entry.model_id, "reasoning_effort": config.reasoning_effort,
                    "thinking_mode": config.thinking_mode, "fixture_tools_only": True,
                    "results": []}

    async def turn(agent, label, text, expected):
        before_calls, before_requests = len(agent.calls), len(agent.llm.requests)
        started = time.monotonic()
        error = ""
        try:
            answer = await agent.reply(Msg(content=[ContentBlock.text(text)]))
            answer_text = answer.get_text()
        except Exception as exc:
            answer_text, error = "", f"{type(exc).__name__}: {exc}"
        calls = agent.calls[before_calls:]
        requests = agent.llm.requests[before_requests:]
        ordered = [c for c in calls if c["name"] not in {"skills_list", "run_code"}]
        core_reads = [c for c in ordered if c["name"] == "skill_view" and c["arguments"].get("name") == CORE_SKILL_NAME]
        first_is_core = bool(ordered) and ordered[0] in core_reads
        work_calls = [c for c in ordered if c["name"] != "skill_view"]
        # Planning a work call in the same batch as a core read means the model
        # has not seen the rules yet, even if execution happens in that order.
        read_before_plan = bool(core_reads and work_calls) and all(
            c["request_index"] > core_reads[0]["request_index"] for c in ordered[1:]
        ) and min(c["request_index"] for c in work_calls) > max(
            c["request_index"] for c in ordered if c["name"] == "skill_view"
        )
        passed = (not calls if expected == "chat" else
                  first_is_core and len(core_reads) == 1 and read_before_plan if expected == "read" else
                  not core_reads and bool(work_calls))
        passed = bool(passed and not error and not any(c["error"] for c in calls))
        item = {"label": label, "expected": expected, "passed": passed, "calls": calls,
                "duration_s": round(time.monotonic() - started, 3), "error": error,
                "answer": answer_text, "requests": [
                    {**{k: v for k, v in r.items() if k not in {"messages", "tools"}},
                     "core_in_system": AGENT_CORE_PROMPT in str(r["messages"][0]),
                     "core_body_copies": sum(AGENT_CORE_PROMPT in str(m.get("content", "")) for m in r["messages"])}
                    for r in requests]}
        report["results"].append(item)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({k: item[k] for k in ("label", "passed", "duration_s", "calls", "error")}, ensure_ascii=False), flush=True)

    with tempfile.TemporaryDirectory(prefix="astra-core-probe-") as temporary:
        root = Path(temporary)
        agent = fixture_agent(root / "conversation", client)
        await turn(agent, "casual-long", "刚忙完一整天，脑子像塞了一团棉花，想随便跟你待一会儿。", "chat")
        await turn(agent, "casual-story", "刚才路上看到一只胖猫坐在花盆旁边，突然觉得还挺治愈的。", "chat")
        await turn(agent, "chat-to-code", "帮我读一下 /fixture/sample.py，检查 sum_positive 是否会误把负数算进去，说明原因。", "read")
        await turn(agent, "back-to-chat", "明白了，暂时不工作了，陪我闲聊两句。", "chat")
        await turn(agent, "reuse-core", "再读一次 /fixture/sample.py，核对零值的处理。", "reuse")
        agent.context.messages = [{"role": "user", "content": "[Summary] Previously inspected sample.py and read astra-core. Full tool outputs were compacted away."}]
        agent.context._rebuild_token_cache()
        await turn(agent, "after-body-compaction", "继续，重新检查 /fixture/sample.py 的问题。", "read")
        await turn(fixture_agent(root / "browser", client), "browser", "查看测试页面偏好设置里「邮件通知」现在是否选中。", "read")
        await turn(fixture_agent(root / "desktop", client), "desktop", "看看 Astra Fixture 窗口里保存按钮是不是可用的。", "read")
    report["passed"] = all(item["passed"] for item in report["results"])
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Authorize configured DeepSeek API requests")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error("This probe requires --live and makes billed model requests")
    return 0 if asyncio.run(run_probe(args.output))["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
