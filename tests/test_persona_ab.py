import asyncio

from scripts.persona_ab import build_requests, run_comparison


class FakeClient:
    def __init__(self):
        self.calls = []

    async def chat_limited(self, messages, tools=None, **kwargs):
        self.calls.append((messages, tools, kwargs))
        return {"content": f"response-{len(self.calls)}", "reasoning_content": "not persisted"}


def test_ab_harness_pairs_identical_inputs_and_omits_reasoning():
    client = FakeClient()
    requests = build_requests("<agent-core-memory>approved fact</agent-core-memory>")

    results = asyncio.run(run_comparison(client, requests))

    assert len(requests) == 8
    assert len(results) == 8
    assert {item["case"] for item in results} == {
        "casual-reentry",
        "technical-task",
        "shared-history",
        "evidence-pushback",
    }
    assert {item["variant"] for item in results} == {"legacy-direct", "identity"}
    assert all("reasoning_content" not in item for item in results)
    for offset in range(0, 8, 2):
        left = client.calls[offset]
        right = client.calls[offset + 1]
        assert left[0][1] == right[0][1]
        assert left[2] == right[2]
        assert left[0][0]["content"] != right[0][0]["content"]
