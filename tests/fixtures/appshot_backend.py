"""Private line-protocol worker: real admission/React preview/media/provider projection."""

import asyncio
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent.cli.appshot_admission import AppshotAdmission, prepare_appshot_message
from agent.core.msg import APPSHOT_UNTRUSTED_PREFIX
from agent.runtime.llm import LLMClient, LLMConfig, OpenAICompatibleProvider
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolRegistry


async def main():
    root = Path(sys.argv[1])
    session_root = Path(sys.argv[2])
    lock = asyncio.Lock()
    config = LLMConfig(model="gpt-4o", base_url="https://api.openai.com/v1", capabilities={"vision"})
    # Actual OpenAI adapter request builder, with no network client constructed.
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = config
    llm = LLMClient(config, provider=provider)
    agent = ReActAgent(
        "fixture",
        llm,
        ToolRegistry(),
        system_prompt="Fixture system",
        vision_cache_root=session_root / "cache",
        timing_log_enabled=False,
        query_profile_enabled=False,
    )
    agent.context.set_session(str(session_root / "sessions" / "fixture.json"))
    agent.context.max_prompt_tokens = 200000
    state = {"busy": False, "launches": 0}
    events, messages = [], []

    def launch(msg, text):
        messages.append(msg)
        state["launches"] += 1
        lock.release()
        return True

    intake = AppshotAdmission(
        lock=lock,
        busy=lambda: state["busy"],
        context=lambda: agent.context,
        prepare=lambda msg: prepare_appshot_message(agent, msg),
        launch=launch,
        send=events.append,
        runtime_root=root,
    )
    for line in sys.stdin:
        request = json.loads(line)
        op = request["op"]
        if op == "submit":
            state["busy"] = request.get("busy", False)
            agent.context.max_prompt_tokens = request.get("budget", 200000)
            events.clear()
            await intake.submit(request["command"])
            result = {"event": events[-1], "launches": state["launches"]}
        elif op == "status":
            result = {"event": intake.status(request["submission_id"]), "launches": state["launches"]}
        elif op == "provider":
            msg = messages[-1]
            # The real adapter strips private image metadata and projects the fixed AX wrapper.
            kwargs = provider._completion_kwargs([{"role": "user", "content": msg.to_chat_content()}])

            async def fake_completion(*, original=msg, **payload):
                content = payload["messages"][0]["content"]
                images = [p for p in content if p["type"] == "image_url"]
                contexts = [p["text"] for p in content if p["type"] == "text" and APPSHOT_UNTRUSTED_PREFIX in p["text"]]
                assert all(set(p) == {"type", "image_url"} for p in images)
                assert len(contexts) == len(images)
                assert all(t.count(APPSHOT_UNTRUSTED_PREFIX) == 1 for t in contexts)
                if os.environ.get("ASTRA_APPSHOT_E2E_SCREENSHOT_ONLY") == "1":
                    assert all("screenshot only; UI text was unavailable" in t for t in contexts)
                    assert all('"coverage":"unavailable"' in t and '"root":{}' in t for t in contexts)
                    assert all("AX_ONLY_SENTINEL" not in t for t in contexts)
                else:
                    assert all("AX_ONLY_SENTINEL" in t for t in contexts)
                    assert all("Task 2: visualization_of_word_embeddings.ipynb" in t for t in contexts)
                assert not any(
                    s in json.dumps(payload) for s in (str(root), "manifest_path", "media_id", "appshot_verified")
                )
                return {
                    "hashes": [
                        hashlib.sha256(base64.b64decode(p["image_url"]["url"].split(",")[1])).hexdigest()
                        for p in images
                    ],
                    "ax_count": len(contexts),
                    "internal_types": [b.type for b in original.content],
                }

            result = await fake_completion(**kwargs)
        else:
            raise AssertionError(op)
        print(json.dumps(result), flush=True)


asyncio.run(main())
