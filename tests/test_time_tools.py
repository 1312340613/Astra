import asyncio
from datetime import datetime, timedelta

import pytest

from agent.runtime import time_utils
from agent.runtime.tools import time as time_tools
from agent.runtime.tools.registry import ToolRegistry


def current_time(arguments=None):
    registry = ToolRegistry()
    time_tools.register_time_tools(registry)
    result = asyncio.run(registry.execute("current_time", arguments or {}))
    assert not result.get("error")
    return result["output"]


@pytest.mark.parametrize("day,weekday", list(enumerate("一二三四五六日", start=7)))
def test_compact_output_covers_every_weekday(monkeypatch, day, weekday):
    frozen = datetime.fromisoformat(f"2026-09-{day:02d}T17:08:09+08:00")
    monkeypatch.setattr(time_tools, "current_datetime", lambda tz: frozen)

    output = current_time()

    assert output == f"2026-09-{day:02d} 周{weekday} 17:08:09 UTC+08:00"
    assert "\n" not in output


@pytest.mark.parametrize("timezone,expected", [
    ("UTC", "2026-09-09 周三 23:30:00 UTC+00:00"),
    ("Asia/Shanghai", "2026-09-10 周四 07:30:00 UTC+08:00"),
    ("Asia/Kathmandu", "2026-09-10 周四 05:15:00 UTC+05:45"),
    ("America/Los_Angeles", "2026-09-09 周三 16:30:00 UTC-07:00"),
])
def test_weekday_and_offset_follow_requested_timezone(monkeypatch, timezone, expected):
    frozen = datetime.fromisoformat("2026-09-09T23:30:00+00:00")

    class FrozenDateTime:
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz)

    monkeypatch.setattr(time_utils, "datetime", FrozenDateTime)

    assert current_time({"timezone": timezone}) == expected


def test_default_timezone_uses_environment(monkeypatch):
    frozen = datetime.fromisoformat("2026-09-09T23:30:00+00:00")

    class FrozenDateTime:
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz)

    monkeypatch.setenv("AGENT_TIMEZONE", "Asia/Shanghai")
    monkeypatch.setattr(time_utils, "datetime", FrozenDateTime)

    assert current_time() == "2026-09-10 周四 07:30:00 UTC+08:00"


def test_each_call_refreshes_date_and_weekday(monkeypatch):
    before_midnight = datetime.fromisoformat("2026-09-13T23:59:59+08:00")
    clock = iter([before_midnight, before_midnight + timedelta(seconds=1)])
    monkeypatch.setattr(time_tools, "current_datetime", lambda tz: next(clock))
    registry = ToolRegistry()
    time_tools.register_time_tools(registry)

    async def scenario():
        first = await registry.execute("current_time", {})
        second = await registry.execute("current_time", {})
        assert first["output"] == "2026-09-13 周日 23:59:59 UTC+08:00"
        assert second["output"] == "2026-09-14 周一 00:00:00 UTC+08:00"

    asyncio.run(scenario())
