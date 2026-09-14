"""MessageBus — 消息总线"""

from typing import Optional, TYPE_CHECKING

from .msg import Msg

if TYPE_CHECKING:
    from .msg import Msg
    from .agent import AgentBase


class MessageBus:
    def __init__(self):
        self._agents: dict[str, "AgentBase"] = {}

    def register(self, agent: "AgentBase"):
        self._agents[agent.name] = agent
        agent.bus = self

    @property
    def agent_names(self) -> list[str]:
        return list(self._agents.keys())

    async def send_stream(self, to: str, msg: "Msg"):
        agent = self._agents.get(to)
        if not agent:
            raise ValueError(f"Agent '{to}' not found")
        async for event in agent.reply_stream(msg):
            yield event

    async def send(self, to: str, msg: "Msg") -> Optional["Msg"]:
        agent = self._agents.get(to)
        if not agent:
            raise ValueError(f"Agent '{to}' not found")
        return await agent.reply(msg)

    async def broadcast(self, msg: "Msg"):
        import asyncio
        tasks = [agent.observe(msg.copy()) for agent in self._agents.values()]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def request(self, targets: list[str], msg: "Msg") -> list["Msg"]:
        import asyncio
        tasks = [self.send(to, msg.copy()) for to in targets if to in self._agents]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if isinstance(r, Msg)]
