"""Time tools backed by the host runtime clock."""

from agent.runtime.time_utils import current_datetime, weekday_label

from .registry import ToolDef, ToolRegistry


def register_time_tools(registry: ToolRegistry):
    def _current_time(timezone: str = "") -> str:
        now = current_datetime(timezone or None)
        offset = now.strftime("%z")
        if len(offset) == 5:
            offset = f"{offset[:3]}:{offset[3:]}"
        return (
            f"{now.strftime('%Y-%m-%d')} {weekday_label(now)} "
            f"{now.strftime('%H:%M:%S')} UTC{offset}"
        )

    registry.register(ToolDef(
        name="current_time",
        description="获取当前日期、星期、时间及 UTC 偏移，用于精确时间或相对日期判断。",
        parameters={
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "IANA 时区，如 Asia/Shanghai；默认 AGENT_TIMEZONE 或本地时区。",
                    "default": "",
                },
            },
        },
        fn=_current_time,
        sandboxed=True,
    ))
