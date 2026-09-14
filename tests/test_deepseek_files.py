import asyncio
import base64
import copy
import time
from types import SimpleNamespace as NS

import pytest

from agent.runtime.deepseek_files import DeepSeekImageFiles, supports_file_reuse
from agent.runtime.llm import LLMConfig


def messages(data=b"image bytes", detail="auto"):
    return [{"role": "user", "content": [{"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + base64.b64encode(data).decode(), "detail": detail,
    }}]}]


class Client:
    def __init__(self, error=None):
        self.files = self
        self.calls = []
        self.error = error

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        await asyncio.sleep(0)
        if self.error:
            raise self.error
        return NS(id=f"file-api-{len(self.calls)}", bytes=len(kwargs["file"][1]), expires_at=time.time() + 3600)


def test_bytes_reused_concurrently_and_after_restart_with_scope_isolation(tmp_path):
    path = tmp_path / "files.db"
    cache = DeepSeekImageFiles("https://api.deepseek.com", "secret-a", path)
    client = Client()
    original = messages()
    saved = copy.deepcopy(original)

    async def exercise():
        results = await asyncio.gather(cache.prepare(original, client), cache.prepare(original, client))
        restored = DeepSeekImageFiles("https://api.deepseek.com", "secret-a", path)
        assert await restored.prepare(original, client) == results[0]
        other = DeepSeekImageFiles("https://api.deepseek.com", "secret-b", path)
        await other.prepare(original, client)
        assert len(client.calls) == 2
        assert results[0][0][0]["content"] == [{"type": "file", "file_id": "file-api-1"}]
        assert cache.hits == 1
    asyncio.run(exercise())
    assert original == saved
    assert b"secret-a" not in path.read_bytes()
    assert b"image bytes" not in path.read_bytes()
    assert client.calls[0]["extra_body"]["expires_after[seconds]"] == "3600"


def test_expired_or_invalidated_references_are_uploaded_again(tmp_path):
    cache = DeepSeekImageFiles("https://api.deepseek.com", "key", tmp_path / "files.db")
    client = Client()

    async def exercise():
        _, ids = await cache.prepare(messages(), client)
        cache.invalidate(ids)
        await cache.prepare(messages(), client)
        with cache._db() as db:
            db.execute("UPDATE uploads SET expires=?", (time.time() + 30,))
        await cache.prepare(messages(), client)
    asyncio.run(exercise())
    assert len(client.calls) == 3


def test_upload_failure_falls_back_and_does_not_repeat_every_turn(tmp_path):
    cache = DeepSeekImageFiles("https://api.deepseek.com", "key", tmp_path / "files.db")
    client = Client(RuntimeError("quota"))

    async def exercise():
        for _ in range(3):
            original = messages()
            prepared, ids = await cache.prepare(original, client)
            assert prepared is original and not ids
    asyncio.run(exercise())
    assert len(client.calls) == 1


def test_cancellation_is_not_converted_to_inline_retry(tmp_path):
    cache = DeepSeekImageFiles("https://api.deepseek.com", "key", tmp_path / "files.db")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cache.prepare(messages(), Client(asyncio.CancelledError())))
    assert cache.disabled_until == 0


def test_low_detail_and_custom_routes_retain_existing_protocol(tmp_path):
    cache = DeepSeekImageFiles("https://api.deepseek.com", "key", tmp_path / "files.db")
    client = Client()
    original = messages(detail="low")
    assert asyncio.run(cache.prepare(original, client)) == (original, set())
    assert not client.calls
    assert supports_file_reuse(LLMConfig(capabilities=frozenset({"vision"})))
    assert not supports_file_reuse(LLMConfig(base_url="http://localhost:8000", capabilities=frozenset({"vision"})))
