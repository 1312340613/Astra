"""AgentBase — 抽象基类，reply/observe 双通道"""

from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .bus import MessageBus
    from .msg import Msg


class AgentBase(ABC):
    name: str
    bus: Optional["MessageBus"] = None

    def __init__(self, name: str):
        self.name = name
        self._observed: list["Msg"] = []

    @abstractmethod
    async def reply(self, msg: "Msg") -> Optional["Msg"]:
        ...

    async def reply_stream(self, msg: "Msg") -> AsyncIterator[dict]:
        response = await self.reply(msg)
        if response is not None:
            yield {"type": "chunk", "content": response.get_text()}
        yield {"type": "done"}

    async def observe(self, msg: "Msg"):
        self._observed.append(msg)

    async def drain_observed(self) -> list["Msg"]:
        msgs = list(self._observed)
        self._observed.clear()
        return msgs

    async def send(self, to: str, msg: "Msg") -> Optional["Msg"]:
        if self.bus:
            return await self.bus.send(to, msg)
        return None

    async def broadcast(self, msg: "Msg"):
        if self.bus:
            await self.bus.broadcast(msg)
