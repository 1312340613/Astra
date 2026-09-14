import asyncio
import base64
import json

from agent.core.msg import ContentBlock, Msg
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def run(coro):
    return asyncio.run(coro)


def test_comfyui_draw_result_is_sent_back_to_llm_as_image(tmp_path):
    image = tmp_path / "generated.png"
    image.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    ))

    class FakeLLM:
        def __init__(self):
            self.calls = 0
            self.second_messages = None

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "call-1",
                        "name": "comfyui_draw",
                        "arguments": json.dumps({"prompt": "test"}),
                    }],
                    "content": "",
                    "usage": None,
                }
                return

            self.second_messages = messages
            yield {"type": "done", "content": "I inspected the generated image.", "usage": None}

    async def scenario():
        registry = ToolRegistry()

        def fake_draw(prompt: str):
            return json.dumps({"success": True, "paths": [str(image)]})

        registry.register(ToolDef(
            name="comfyui_draw",
            description="fake draw",
            parameters={"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
            fn=fake_draw,
        ))
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=3)

        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("draw and inspect")]))]

        assert events[-1]["type"] == "done"
        assert llm.calls == 2
        image_messages = [
            msg for msg in llm.second_messages
            if isinstance(msg.get("content"), list)
            and any(part.get("type") == "image_url" for part in msg["content"])
        ]
        assert image_messages
        image_part = next(part for part in image_messages[0]["content"] if part["type"] == "image_url")
        assert image_part["image_url"]["url"].startswith("data:image/png;base64,")

        stored_image_messages = [
            msg for msg in agent.context.messages
            if isinstance(msg.get("content"), list)
            and any(part.get("text") == "[Image: generated.png]" for part in msg["content"])
        ]
        assert stored_image_messages

    run(scenario())
