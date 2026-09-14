"""Tests for session_handoff: generate_handoff, save_handoff, load_latest_handoff.

Covers:
- Document structure (header, task state, conversation summary, notes, resume instructions)
- TaskStore integration (blocked/scheduled runs and completions)
- Message extraction (dict format, Msg objects, mixed content types)
- File persistence (save_handoff, latest.md, load_latest_handoff)
- Internal helpers (_truncate, _extract_msg_text)
- Msg-to-dict conversion logic used by _auto_handoff / /handoff in cli/main.py
"""

import pytest

from agent.runtime.session_handoff import (
    generate_handoff,
    save_handoff,
    load_latest_handoff,
    _truncate,
    _extract_msg_text,
    _build_conversation_section,
    _build_task_section,
    _build_resume_instructions,
)
from agent.runtime.task_store import TaskStore
from agent.core.msg import Msg, ContentBlock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path):
    return TaskStore(path=tmp_path / "test_tasks.db")


@pytest.fixture()
def handoff_dir(tmp_path):
    return tmp_path / "handoffs"


_run_counter = 0


def _start_run(store: TaskStore, *, text: str = "test task", session_id: str = "s1") -> str:
    global _run_counter
    _run_counter += 1
    task = store.start_run(
        request_id=f"req-{_run_counter}",
        input_text=text,
        session_id=session_id,
    )
    return task["id"]


# ---------------------------------------------------------------------------
# generate_handoff — basic structure
# ---------------------------------------------------------------------------

class TestGenerateHandoffBasic:
    def test_minimal_handoff_has_header(self):
        text = generate_handoff(session_id="test-session-001", task_store=None)
        assert "# Session Handoff" in text
        assert "`test-session-001`" in text

    def test_includes_model_and_persona(self):
        text = generate_handoff(
            session_id="s1",
            task_store=None,
            model="qwen3.8-max",
            persona_id="lyra",
        )
        assert "qwen3.8-max" in text
        assert "lyra" in text

    def test_omits_model_when_empty(self):
        text = generate_handoff(session_id="s1", task_store=None, model="")
        assert "**Model**" not in text

    def test_omits_persona_when_empty(self):
        text = generate_handoff(session_id="s1", task_store=None, persona_id="")
        assert "**Persona**" not in text

    def test_always_has_resume_instructions(self):
        text = generate_handoff(session_id="s1", task_store=None)
        assert "## Resume Instructions" in text
        assert "Read this handoff" in text

    def test_resume_instructions_mention_taskstore_when_provided(self, store):
        text = generate_handoff(session_id="s1", task_store=store)
        assert "TaskStore is the single source of truth" in text

    def test_resume_instructions_no_taskstore_mention_when_none(self):
        text = generate_handoff(session_id="s1", task_store=None)
        assert "TaskStore is the single source of truth" not in text

    def test_extra_notes_included(self):
        text = generate_handoff(
            session_id="s1",
            task_store=None,
            extra_notes="Remember to fix the auth bug",
        )
        assert "## Notes" in text
        assert "Remember to fix the auth bug" in text

    def test_empty_extra_notes_omitted(self):
        text = generate_handoff(session_id="s1", task_store=None, extra_notes="   ")
        assert "## Notes" not in text

    def test_redacts_secrets_from_messages_notes_and_task_text(self, store):
        _start_run(store, text="use api_key=super-secret-value to deploy")
        text = generate_handoff(
            session_id="s1",
            task_store=store,
            messages=[{"role": "user", "content": "Authorization: Bearer abcdefghijklmnop"}],
            extra_notes="password=hunter2 sk-abcdefghijklmnop",
        )

        assert "super-secret-value" not in text
        assert "abcdefghijklmnop" not in text
        assert "hunter2" not in text
        assert "[REDACTED]" in text

    def test_generated_timestamp_present(self):
        text = generate_handoff(session_id="s1", task_store=None)
        assert "**Generated**:" in text
        assert "UTC" in text


# ---------------------------------------------------------------------------
# generate_handoff — TaskStore integration
# ---------------------------------------------------------------------------

class TestGenerateHandoffTaskStore:
    def test_empty_store_no_task_section(self, store):
        text = generate_handoff(session_id="s1", task_store=store)
        assert "## Task State" not in text

    def test_blocked_run_shown(self, store):
        run_id = _start_run(store, text="等用户确认方案")
        store.block_run(run_id, "需要用户选择数据库")
        text = generate_handoff(session_id="s1", task_store=store)
        assert "Blocked Runs" in text
        assert "等用户确认方案" in text
        assert "需要用户选择数据库" in text

    def test_waiting_external_run_shown(self, store):
        run_id = _start_run(store, text="等邮件回复")
        store.block_run(run_id, "等HR回复", kind="waiting_external")
        text = generate_handoff(session_id="s1", task_store=store)
        assert "等HR回复" in text

    def test_scheduled_run_shown(self, store):
        run_id = _start_run(store, text="定时部署")
        # Use a past date so it shows up as "due"
        store.schedule_run(run_id, "2020-01-01T09:00:00+00:00")
        text = generate_handoff(session_id="s1", task_store=store)
        assert "Scheduled" in text
        assert "定时部署" in text

    def test_completed_task_shown(self, store):
        run_id = _start_run(store, text="写单元测试")
        store.finish_run(run_id, "completed")
        text = generate_handoff(session_id="s1", task_store=store)
        assert "Recently Completed" in text
        assert "写单元测试" in text

    def test_verified_task_shows_checkmark(self, store):
        run_id = _start_run(store, text="验证通过的任务")
        store.finish_run(run_id, "completed")
        store.verify_completion(run_id, verifier="pytest")
        text = generate_handoff(session_id="s1", task_store=store)
        assert "✓" in text


# ---------------------------------------------------------------------------
# generate_handoff — messages / conversation summary
# ---------------------------------------------------------------------------

class TestGenerateHandoffMessages:
    def test_no_messages_no_conversation_section(self):
        text = generate_handoff(session_id="s1", task_store=None, messages=None)
        assert "## Conversation Summary" not in text

    def test_empty_messages_no_section(self):
        text = generate_handoff(session_id="s1", task_store=None, messages=[])
        assert "## Conversation Summary" not in text

    def test_dict_messages_shown(self):
        msgs = [
            {"role": "user", "content": "帮我修个bug"},
            {"role": "assistant", "content": "好的，我看看"},
        ]
        text = generate_handoff(session_id="s1", task_store=None, messages=msgs)
        assert "## Conversation Summary" in text
        assert "帮我修个bug" in text
        assert "好的，我看看" in text

    def test_system_and_tool_messages_filtered(self):
        msgs = [
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "user", "content": "hello"},
            {"role": "tool", "content": "tool output here"},
            {"role": "assistant", "content": "hi there"},
        ]
        text = generate_handoff(session_id="s1", task_store=None, messages=msgs)
        assert "You are a helpful assistant" not in text
        assert "tool output here" not in text
        assert "hello" in text
        assert "hi there" in text

    def test_message_count_shown(self):
        msgs = [{"role": "user", "content": f"msg {i}"} for i in range(5)]
        text = generate_handoff(session_id="s1", task_store=None, messages=msgs)
        assert "5 messages this session" in text

    def test_only_last_20_shown(self):
        msgs = [{"role": "user", "content": f"message number {i}"} for i in range(30)]
        text = generate_handoff(session_id="s1", task_store=None, messages=msgs)
        assert "message number 29" in text
        assert "message number 0" not in text
        assert "showing last 20" in text

    def test_list_content_extracted(self):
        msgs = [
            {"role": "user", "content": [
                {"type": "text", "text": "first part"},
                {"type": "text", "text": "second part"},
            ]},
        ]
        text = generate_handoff(session_id="s1", task_store=None, messages=msgs)
        assert "first part" in text
        assert "second part" in text

    def test_long_message_truncated(self):
        long_text = "x" * 200
        msgs = [{"role": "user", "content": long_text}]
        text = generate_handoff(session_id="s1", task_store=None, messages=msgs)
        # Should be truncated to 120 chars + ellipsis
        assert "x" * 200 not in text
        assert "…" in text

    def test_msg_object_conversion(self):
        """Verify the Msg-to-dict conversion used in _auto_handoff works correctly."""
        msg = Msg(
            sender="user",
            role="user",
            content=[ContentBlock.text("来自Msg对象的消息")],
        )
        # This is the exact conversion used in cli/main.py _auto_handoff
        converted = msg if isinstance(msg, dict) else {"role": msg.role, "content": msg.get_text()}
        assert isinstance(converted, dict)
        assert converted["role"] == "user"
        assert converted["content"] == "来自Msg对象的消息"

        # And it should work when passed to generate_handoff
        text = generate_handoff(session_id="s1", task_store=None, messages=[converted])
        assert "来自Msg对象的消息" in text

    def test_mixed_msg_objects_and_dicts(self):
        """Both Msg-converted dicts and raw dicts should work together."""
        msg_obj = Msg(sender="user", role="user", content=[ContentBlock.text("Msg对象")])
        converted = {"role": msg_obj.role, "content": msg_obj.get_text()}
        raw_dict = {"role": "assistant", "content": "普通dict"}
        text = generate_handoff(session_id="s1", task_store=None, messages=[converted, raw_dict])
        assert "Msg对象" in text
        assert "普通dict" in text

    def test_msg_with_image_content(self):
        """Msg with image blocks should extract text parts only."""
        msg = Msg(
            sender="user",
            role="user",
            content=[
                ContentBlock.text("看看这张图"),
                ContentBlock.image_url("data:image/png;base64,abc123"),
            ],
        )
        converted = {"role": msg.role, "content": msg.get_text()}
        text = generate_handoff(session_id="s1", task_store=None, messages=[converted])
        assert "看看这张图" in text
        # Image data should NOT appear
        assert "base64" not in text


# ---------------------------------------------------------------------------
# save_handoff / load_latest_handoff
# ---------------------------------------------------------------------------

class TestSaveAndLoad:
    def test_save_creates_file(self, handoff_dir):
        path = save_handoff("# Test Handoff", session_id="s1", base_dir=handoff_dir)
        assert path.exists()
        assert path.read_text(encoding="utf-8") == "# Test Handoff"

    def test_save_creates_latest(self, handoff_dir):
        save_handoff("# Test Handoff", session_id="s1", base_dir=handoff_dir)
        latest = handoff_dir / "latest.md"
        assert latest.exists()
        assert latest.read_text(encoding="utf-8") == "# Test Handoff"

    def test_save_creates_directory(self, tmp_path):
        nested = tmp_path / "a" / "b" / "handoffs"
        path = save_handoff("# Nested", session_id="s1", base_dir=nested)
        assert path.exists()

    def test_save_filename_contains_session_id(self, handoff_dir):
        path = save_handoff("# Test", session_id="my-session-42", base_dir=handoff_dir)
        assert "my-session-42" in path.name

    def test_session_id_cannot_escape_handoff_directory(self, handoff_dir):
        path = save_handoff("# Test", session_id="../../outside", base_dir=handoff_dir)
        assert path.parent == handoff_dir
        assert ".." not in path.name

    def test_save_overwrites_latest(self, handoff_dir):
        save_handoff("# First", session_id="s1", base_dir=handoff_dir)
        save_handoff("# Second", session_id="s2", base_dir=handoff_dir)
        latest = handoff_dir / "latest.md"
        assert latest.read_text(encoding="utf-8") == "# Second"

    def test_load_latest_returns_content(self, handoff_dir):
        save_handoff("# Loadable", session_id="s1", base_dir=handoff_dir)
        content = load_latest_handoff(base_dir=handoff_dir)
        assert content == "# Loadable"

    def test_load_latest_returns_none_when_missing(self, handoff_dir):
        content = load_latest_handoff(base_dir=handoff_dir)
        assert content is None

    def test_roundtrip_generate_save_load(self, store, handoff_dir):
        run_id = _start_run(store, text="往返测试", session_id="s1")
        store.finish_run(run_id, "completed")
        text = generate_handoff(session_id="roundtrip", task_store=store, model="test-model")
        save_handoff(text, session_id="roundtrip", base_dir=handoff_dir)
        loaded = load_latest_handoff(base_dir=handoff_dir)
        assert loaded == text
        assert "往返测试" in loaded


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------

class TestTruncate:
    def test_short_text_unchanged(self):
        assert _truncate("hello", 10) == "hello"

    def test_exact_length_unchanged(self):
        assert _truncate("12345", 5) == "12345"

    def test_long_text_truncated_with_ellipsis(self):
        result = _truncate("a" * 100, 20)
        assert len(result) == 20
        assert result.endswith("…")

    def test_whitespace_collapsed(self):
        assert _truncate("hello   world   foo", 50) == "hello world foo"

    def test_non_string_converted(self):
        assert _truncate(12345, 10) == "12345"

    def test_empty_string(self):
        assert _truncate("", 10) == ""


# ---------------------------------------------------------------------------
# _extract_msg_text
# ---------------------------------------------------------------------------

class TestExtractMsgText:
    def test_string_content(self):
        assert _extract_msg_text({"content": "hello world"}) == "hello world"

    def test_list_content_with_text_dicts(self):
        msg = {"content": [
            {"type": "text", "text": "part1"},
            {"type": "text", "text": "part2"},
        ]}
        assert _extract_msg_text(msg) == "part1 part2"

    def test_list_content_with_plain_strings(self):
        msg = {"content": ["hello", "world"]}
        assert _extract_msg_text(msg) == "hello world"

    def test_list_content_mixed(self):
        msg = {"content": [
            {"type": "text", "text": "dict part"},
            "string part",
        ]}
        assert _extract_msg_text(msg) == "dict part string part"

    def test_list_content_with_text_key_no_type(self):
        msg = {"content": [{"text": "no type field"}]}
        assert _extract_msg_text(msg) == "no type field"

    def test_empty_content(self):
        assert _extract_msg_text({"content": ""}) == ""
        assert _extract_msg_text({"content": []}) == ""
        assert _extract_msg_text({}) == ""

    def test_non_string_non_list_content(self):
        assert _extract_msg_text({"content": 42}) == ""

    def test_whitespace_stripped(self):
        assert _extract_msg_text({"content": "  hello  "}) == "hello"


# ---------------------------------------------------------------------------
# _build_conversation_section
# ---------------------------------------------------------------------------

class TestBuildConversationSection:
    def test_empty_returns_empty(self):
        assert _build_conversation_section([]) == ""

    def test_only_system_returns_empty(self):
        msgs = [{"role": "system", "content": "sys prompt"}]
        assert _build_conversation_section(msgs) == ""

    def test_user_and_assistant_included(self):
        msgs = [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ]
        result = _build_conversation_section(msgs)
        assert "question" in result
        assert "answer" in result
        assert "👤" in result
        assert "🤖" in result

    def test_empty_text_messages_skipped(self):
        msgs = [
            {"role": "user", "content": ""},
            {"role": "user", "content": "real message"},
        ]
        result = _build_conversation_section(msgs)
        assert "real message" in result
        # Count should reflect relevant messages (both are user/assistant role)
        assert "2 messages" in result


# ---------------------------------------------------------------------------
# _build_task_section
# ---------------------------------------------------------------------------

class TestBuildTaskSection:
    def test_empty_store_returns_empty(self, store):
        assert _build_task_section(store) == ""


# ---------------------------------------------------------------------------
# _build_resume_instructions
# ---------------------------------------------------------------------------

class TestBuildResumeInstructions:
    def test_without_store(self):
        result = _build_resume_instructions(None)
        assert "Resume Instructions" in result
        assert "TaskStore" not in result

    def test_with_store(self, store):
        result = _build_resume_instructions(store)
        assert "TaskStore is the single source of truth" in result

    def test_standard_steps_present(self):
        result = _build_resume_instructions(None)
        assert "format_today()" in result
        assert "TaskRun checkpoint" in result
        assert "Do NOT re-do completed items" in result


# ---------------------------------------------------------------------------
# Integration: full handoff with all sections
# ---------------------------------------------------------------------------

class TestFullIntegration:
    def test_complete_handoff_all_sections(self, store, handoff_dir):
        # Set up task state
        run1 = _start_run(store, text="已完成的任务", session_id="s1")
        store.finish_run(run1, "completed")
        run2 = _start_run(store, text="被阻塞的任务", session_id="s1")
        store.block_run(run2, "等用户确认")

        # Messages
        msgs = [
            {"role": "user", "content": "开始干活"},
            {"role": "assistant", "content": "收到，马上开始"},
        ]

        text = generate_handoff(
            session_id="integration-test",
            task_store=store,
            messages=msgs,
            model="test-model",
            persona_id="lyra",
            extra_notes="注意：API key 不要提交到 git",
        )

        # Verify all sections present
        assert "# Session Handoff" in text
        assert "## Task State" in text
        assert "## Conversation Summary" in text
        assert "## Notes" in text
        assert "## Resume Instructions" in text

        # Verify content
        assert "已完成的任务" in text
        assert "被阻塞的任务" in text
        assert "等用户确认" in text
        assert "开始干活" in text
        assert "API key 不要提交到 git" in text

        # Save and reload
        save_handoff(text, session_id="integration-test", base_dir=handoff_dir)
        loaded = load_latest_handoff(base_dir=handoff_dir)
        assert loaded == text

    def test_handoff_idempotent_overwrite(self, handoff_dir):
        """Generating twice for same session overwrites latest, creates new timestamped file."""
        text1 = generate_handoff(session_id="same-session", task_store=None, extra_notes="v1")
        text2 = generate_handoff(session_id="same-session", task_store=None, extra_notes="v2")

        path1 = save_handoff(text1, session_id="same-session", base_dir=handoff_dir)
        path2 = save_handoff(text2, session_id="same-session", base_dir=handoff_dir)

        # Both timestamped files exist
        assert path1.exists()
        assert path2.exists()

        # latest.md has the newest
        latest = load_latest_handoff(base_dir=handoff_dir)
        assert "v2" in latest
