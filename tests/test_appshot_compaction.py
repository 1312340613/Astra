"""Fresh Appshots can compact before admission without mutating rejected turns."""

import asyncio
import copy
import os
from types import SimpleNamespace

import pytest

from agent.cli.appshot_admission import prepare_appshot_message
from agent.cli.appshots import AppshotValidationError
from agent.core.msg import ContentBlock, Msg
from agent.runtime.llm import LLMConfig
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolRegistry
from test_appshot_backend import bundle as bundle
from test_llm import png_url


class SummaryLLM:
    def __init__(self):
        self.config = LLMConfig(
            model="deepseek-flash", base_url="https://api.deepseek.com",
            context_limit=40000, max_tokens=8192, capabilities=frozenset({"vision"}),
        )
        self.summaries = []
        self.requests = []
        self.summary_error = False

    async def chat_limited(self, messages, **kwargs):
        self.summaries.append(messages)
        if self.summary_error:
            raise RuntimeError("fixture summary failure")
        return {"content": "## Completed Work\nThe earlier task is complete.\n## Active Task\nNone."}

    async def chat_stream(self, messages, tools, **kwargs):
        self.requests.append(messages)
        yield {"type": "done", "content": "seen", "usage": None}


@pytest.fixture
def agent(tmp_path):
    instance = ReActAgent(
        "fixture", SummaryLLM(), ToolRegistry(), system_prompt="System",
        vision_cache_root=tmp_path / "cache", timing_log_enabled=False, query_profile_enabled=False,
    )
    instance.context.set_session(str(tmp_path.resolve() / "session.json"))
    instance.context.max_prompt_tokens = 20000
    instance.context.add_user("Earlier task: " + "history detail " * 16000)
    instance.context.add_assistant("Completed.")
    instance.context.add_user("Most recent completed task")
    instance.context.add_assistant("Done.")
    instance.context.save()
    return instance


def appshot(text="Inspect this new window"):
    image = ContentBlock.image_url(png_url())
    image.data["appshot_verified"] = True
    return Msg(content=[
        ContentBlock.text(text), image,
        ContentBlock.appshot_context({"window_title": "Fixture"}, {"root": {"role": "AXWindow"}}),
    ])


def test_budget_recovery_stages_history_without_accepting_or_saving(agent):
    before = copy.deepcopy(agent.context.messages)
    compressor = agent.context.compressor
    assert compressor is not None
    msg = appshot()
    prepared = asyncio.run(prepare_appshot_message(agent, msg))
    assert len(agent.llm.summaries) == 1
    assert not agent.llm.requests
    assert prepared is not None
    assert agent.context.messages == before
    assert agent.context.compressor is compressor
    assert compressor.compression_count == 0
    assert compressor._previous_summary is None
    assert agent.context._session_store.load()["messages"] == before
    assert msg.get_text() == "Inspect this new window"
    assert msg.content[1].data["url"] == png_url()


@pytest.mark.parametrize("failure", ["summary", "oversized_message", "disabled"])
def test_failed_recovery_keeps_live_history_and_compressor(agent, failure):
    if failure == "summary":
        agent.llm.summary_error = True
    elif failure == "disabled":
        agent.context.compaction_enabled = False
    msg = appshot("new request " * 20000 if failure == "oversized_message" else "Inspect")
    before = copy.deepcopy(agent.context.messages)
    compressor = agent.context.compressor
    with pytest.raises(AppshotValidationError, match="context_budget_exceeded"):
        asyncio.run(prepare_appshot_message(agent, msg))
    assert len(agent.llm.summaries) == (0 if failure == "disabled" else 1)
    assert not agent.llm.requests
    assert agent.context.messages == before
    assert agent.context.compressor is compressor
    assert compressor.compression_count == 0
    assert compressor._failure_cooldown_until == 0
    assert agent.context._session_store.load()["messages"] == before


def intake(agent, bundle, events, launch):
    from agent.cli.appshot_admission import AppshotAdmission

    agent.context.compaction_observer = events.append
    command = dict(
        type="message", text="Inspect this new window", submission_id="recovery",
        appshots=[dict(label="[Appshot #1]", manifest_path=str(bundle[1]))],
        appshot_session_id="session", appshot_broker_id="broker",
    )
    admission = AppshotAdmission(
        lock=asyncio.Lock(), busy=lambda: False, context=lambda: agent.context,
        prepare=lambda msg: prepare_appshot_message(agent, msg),
        launch=launch, send=events.append, runtime_root=bundle[0],
        parent_identity=lambda: SimpleNamespace(uid=os.getuid(), process_start="1234"),
    )
    return admission, command


def test_verified_submission_recovers_once_launches_and_persists(agent, bundle):
    from agent.cli.appshot_admission import schedule_reserved_turn
    from agent.runtime.context_compressor import SUMMARY_PREFIX

    async def run():
        events, launched, tasks = [], [], []
        original_compressor = agent.context.compressor
        hooks = original_compressor.hooks

        async def turn(msg):
            assert events[-1]["type"] == "message_accepted"
            return [event async for event in agent.reply_stream(msg)]

        def launch(msg, text):
            assert agent.context.compressor.compression_count == 1
            assert any(SUMMARY_PREFIX in str(m.get("content")) for m in agent.context.messages)
            launched.append(msg)
            tasks.append(schedule_reserved_turn(turn(msg), admission.lock, name="fixture-appshot"))
            return True

        admission, command = intake(agent, bundle, events, launch)
        await admission.submit(command)
        assert [e.get("status", e["type"]) for e in events] == [
            "started", "completed", "message_accepted",
        ]
        assert events[1]["messages_before"] == 4
        assert events[1]["messages_after"] == len(agent.context.messages)
        assert events[1]["method"] == "summary"
        assert events[1]["tokens_after"] < events[1]["tokens_before"]
        assert admission.lock.locked()
        await tasks[0]
        await asyncio.sleep(0)
        assert not admission.lock.locked()
        assert agent.context.compressor.hooks is hooks
        assert original_compressor.compression_count == 0
        await admission.submit(command)
        assert events[-1]["replayed"] is True
        assert len(launched) == len(agent.llm.requests) == len(agent.llm.summaries) == 1
        assert len([m for m in agent.context.messages if isinstance(m.get("content"), list)]) == 1
        saved = agent.context._session_store.load()["messages"]
        assert saved == agent.context.messages
        assert any(SUMMARY_PREFIX in str(m.get("content")) for m in saved)
        assert not any("history detail" in str(m.get("content")) for m in saved)
        storage = launched[0].to_storage_content()
        assert any(part["type"] == "appshot_image" for part in storage)
        prompt = agent.llm.requests[0]
        assert any("Inspect this new window" in str(m.get("content")) for m in prompt)
        assert any(part.get("type") == "image_url" for m in prompt
                   if isinstance(m.get("content"), list) for part in m["content"])

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["media", "identity", "busy", "launch_false", "launch_error", "cancel"])
def test_late_admission_failure_rolls_back_compaction(agent, bundle, monkeypatch, failure):
    from agent.runtime.appshot_media import AppshotMediaStore

    async def run():
        events = []
        before = copy.deepcopy(agent.context.messages)
        original_compressor = agent.context.compressor
        original_count = agent.context._saved_message_count
        original_tokens = agent.context.last_prompt_tokens

        def launch(msg, text):
            assert failure in {"launch_false", "launch_error"}
            assert agent.context.compressor.compression_count == 1
            if failure == "launch_error":
                raise RuntimeError("fixture launch failure")
            return False

        admission, command = intake(agent, bundle, events, launch)
        if failure == "media":
            def fail_media(*args, **kwargs):
                raise OSError("fixture media failure")
            monkeypatch.setattr(AppshotMediaStore, "put", fail_media)
        elif failure == "identity":
            admission.parent_identity = lambda: SimpleNamespace(
                uid=os.getuid(), process_start="changed" if agent.llm.summaries else "1234",
            )
        elif failure == "busy":
            admission.busy = lambda: bool(agent.llm.summaries)
        elif failure == "cancel":
            entered = asyncio.Event()
            async def cancelled_summary(messages, **kwargs):
                agent.llm.summaries.append(messages)
                entered.set()
                await asyncio.Future()
            agent.llm.chat_limited = cancelled_summary

        if failure == "cancel":
            task = asyncio.create_task(admission.submit(command))
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await admission.submit(command)
        assert events[-1]["type"] == "message_rejected"
        assert [e["status"] for e in events if e["type"] == "context_compaction"] == [
            "started", "cancelled" if failure == "cancel" else "failed",
        ]
        assert all("tokens_after" not in e for e in events if e["type"] == "context_compaction")
        assert not admission.lock.locked()
        assert not admission.reserved
        assert agent.context.messages == before
        assert agent.context.compressor is original_compressor
        assert original_compressor.compression_count == 0
        assert original_compressor._previous_summary is None
        assert original_compressor._failure_cooldown_until == 0
        assert agent.context.last_prompt_tokens == original_tokens
        assert agent.context._saved_message_count == original_count
        assert agent.context._session_store.load()["messages"] == before
        assert not agent.llm.requests
        assert len(agent.llm.summaries) == 1
        assert bundle[1].exists()  # The unaccepted draft still owns its artifact.
        await admission.submit(command)
        assert events[-1]["replayed"] is True
        assert len(agent.llm.summaries) == 1

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled", "exception"])
def test_auto_compaction_reports_progress_while_summary_is_pending(agent, outcome):
    async def run():
        events = []
        agent.context.compaction_observer = events.append
        entered = asyncio.Event()
        release = asyncio.Event()
        original_summary = agent.llm.chat_limited

        async def summary(messages, **kwargs):
            entered.set()
            await release.wait()
            if outcome == "failed":
                agent.llm.summary_error = True
            if outcome == "exception":
                raise asyncio.TimeoutError()
            return await original_summary(messages, **kwargs)

        agent.llm.chat_limited = summary
        task = asyncio.create_task(agent.context.compress_if_needed())
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert len(events) == 1
        assert events[0]["type"] == "context_compaction"
        assert events[0]["status"] == "started"
        assert events[0]["messages_before"] == events[0]["messages_after"] == 4
        assert events[0]["tokens_before"] > events[0]["target_tokens"]
        assert "tokens_after" not in events[0]
        if outcome == "cancelled":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            await task
        assert len(events) == 2
        assert events[1]["status"] == ("failed" if outcome == "exception" else outcome)
        assert events[1]["messages_after"] == len(agent.context.messages)
        assert events[1]["tokens_after"] > 0
        if outcome != "completed":
            assert events[1]["tokens_before"] == events[1]["tokens_after"]
        assert not agent.llm.requests

    asyncio.run(run())


def test_no_progress_when_compaction_is_disabled_or_unneeded(agent):
    events = []
    agent.context.compaction_observer = events.append
    agent.context.compaction_enabled = False
    asyncio.run(agent.context.compress_if_needed(force=True))
    agent.context.compaction_enabled = True
    agent.context.max_prompt_tokens = 1_000_000
    asyncio.run(agent.context.compress_if_needed())
    assert events == []
    assert not agent.llm.summaries
