"""Runtime clock and compact relative-date helpers."""

import os
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_RELATIVE_DATE_TERMS = (
    "今天",
    "昨天",
    "明天",
    "前天",
    "后天",
    "最近",
    "近几天",
    "近三天",
    "本周",
    "上周",
    "下周",
    "本月",
    "上月",
    "下月",
    "今年",
    "去年",
    "明年",
    "今日",
    "昨日",
    "明日",
)


def current_datetime(timezone_name: str | None = None):
    tz_name = (timezone_name or os.getenv("AGENT_TIMEZONE", "")).strip()
    try:
        return datetime.now(ZoneInfo(tz_name)) if tz_name else datetime.now().astimezone()
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone()


def weekday_label(value: date) -> str:
    """Locale-independent weekday for an already resolved local date."""
    return "周" + "一二三四五六日"[value.weekday()]


def needs_relative_date_anchor(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text.strip().lower())
    return bool(normalized) and any(term in normalized for term in _RELATIVE_DATE_TERMS)


def relative_date_metadata() -> dict[str, str]:
    now = current_datetime()
    raw_offset = now.strftime("%z")
    offset = (
        f"{raw_offset[:3]}:{raw_offset[3:]}"
        if len(raw_offset) == 5
        else (raw_offset or "local")
    )
    return {"date": now.strftime("%Y-%m-%d"), "utc_offset": offset}


def relative_date_anchor(metadata: object = None) -> str:
    fields = relative_date_metadata() if metadata is None else metadata
    if not isinstance(fields, dict):
        return ""
    date_value, offset = fields.get("date"), fields.get("utc_offset")
    if not isinstance(date_value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value):
        return ""
    if not isinstance(offset, str) or not re.fullmatch(r"[+-]\d{2}:\d{2}|local", offset):
        return ""
    try:
        weekday = weekday_label(date.fromisoformat(date_value))
    except ValueError:
        return ""
    return (
        "[SYSTEM-SUPPLIED DATE ANCHOR]\n"
        f"Local date: {date_value}; UTC offset: {offset}. Weekday: {weekday}.\n"
        "Resolve relative dates silently. Call current_time for exact clock time "
        "or another timezone.\n"
        "[END DATE ANCHOR]"
    )
