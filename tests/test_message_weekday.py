from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from agent.runtime import context as context_module
from agent.runtime import time_utils
from agent.runtime.context import AgentContext, _model_timestamp_marker


def local_timezone(monkeypatch, timezone):
    class LocalClock:
        @staticmethod
        def fromtimestamp(value):
            local = datetime.fromtimestamp(value, ZoneInfo(timezone))
            return SimpleNamespace(astimezone=lambda: local)

    monkeypatch.setattr(context_module, "datetime", LocalClock)


@pytest.mark.parametrize("day,weekday", list(enumerate("一二三四五六日", start=7)))
def test_model_timestamp_includes_each_weekday(monkeypatch, day, weekday):
    local_timezone(monkeypatch, "Asia/Singapore")
    authored = datetime.fromisoformat(f"2026-09-{day:02d}T17:08:09+08:00")
    assert _model_timestamp_marker(authored.timestamp()) == (
        f"<message_time>2026-09-{day:02d}T17:08:09+08:00 周{weekday}</message_time>"
    )


@pytest.mark.parametrize("timezone,expected", [
    ("Asia/Singapore", "2026-09-14T00:30:00+08:00 周一"),
    ("UTC", "2026-09-13T16:30:00+00:00 周日"),
    ("America/Los_Angeles", "2026-09-13T09:30:00-07:00 周日"),
])
def test_weekday_uses_the_timestamp_local_date(monkeypatch, timezone, expected):
    local_timezone(monkeypatch, timezone)
    authored = datetime.fromisoformat("2026-09-13T16:30:00+00:00")
    assert _model_timestamp_marker(authored.timestamp()) == f"<message_time>{expected}</message_time>"


def test_history_and_new_messages_keep_their_own_weekday(monkeypatch):
    local_timezone(monkeypatch, "Asia/Singapore")
    before = datetime.fromisoformat("2026-09-13T23:59:59+08:00").timestamp()
    ctx = AgentContext(system_prompt="sys")
    monkeypatch.setattr(context_module.time, "time", lambda: before)
    ctx.add_user("before midnight")
    original = dict(ctx.messages[0])
    monkeypatch.setattr(context_module.time, "time", lambda: before + 1)
    ctx.add_user("after midnight")
    prompt = [item for item in ctx.get_prompt() if item["role"] == "user"]
    assert "2026-09-13T23:59:59+08:00 周日" in prompt[0]["content"]
    assert "2026-09-14T00:00:00+08:00 周一" in prompt[1]["content"]
    assert ctx.messages[0] == original
    assert ctx.messages[1]["content"] == "after midnight"


def test_relative_anchor_derives_weekday_from_legacy_saved_date(monkeypatch):
    monkeypatch.setattr(time_utils, "current_datetime", lambda: pytest.fail("must not use today's date"))
    metadata = {"date": "2026-09-13", "utc_offset": "+08:00"}
    anchor = time_utils.relative_date_anchor(metadata)
    assert "Local date: 2026-09-13; UTC offset: +08:00. Weekday: 周日." in anchor
    assert metadata == {"date": "2026-09-13", "utc_offset": "+08:00"}


@pytest.mark.parametrize("date", ["2026-02-30", "2026-13-01", "0000-01-01"])
def test_invalid_saved_date_does_not_invent_a_weekday(date):
    assert time_utils.relative_date_anchor({"date": date, "utc_offset": "+08:00"}) == ""
