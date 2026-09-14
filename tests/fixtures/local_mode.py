"""Neutral, tool-free controller for exercising the optional host protocol."""

from pathlib import Path

from agent.runtime import conversation_edits
from agent.runtime.context import AgentContext
from agent.runtime.local_mode import LocalMode

API_VERSION = 1


class FixtureController:
    def __init__(self, agent):
        self.agent = agent
        self.parked = None

    @property
    def active(self):
        return self.parked is not None

    @property
    def session_name(self):
        return Path(self.agent.context.session_path).stem if self.active else ""

    def _context(self, path):
        context = AgentContext("Local fixture context.")
        context.set_session(str(path))
        context.load()
        context.set_system_prompt("Local fixture context.")
        context.save()
        return context

    def enter(self, path):
        if self.active:
            return False
        self.agent.context.save()
        context = self._context(path)
        fields = {
            "context": context, "local_mode": True, "tools_enabled": False,
            "tool_allowlist": set(), "memory_store": None,
            "external_memory_provider": None, "skill_store": None, "task_store": None,
            "runtime_context_provider": None, "runtime_turn_context_provider": None,
            "generation_overrides_provider": None, "finalize_after_tools_provider": None,
            "forced_tool_name": None,
        }
        self.parked = {name: getattr(self.agent, name) for name in fields}
        for name, value in fields.items():
            setattr(self.agent, name, value)
        return True

    def switch(self, path):
        self.agent.context.save()
        self.agent.context = self._context(path)

    def leave(self):
        if not self.active:
            return False
        self.agent.context.save()
        for name, value in self.parked.items():
            setattr(self.agent, name, value)
        self.parked = None
        return True

    def undo_last_reply(self):
        if not self.active:
            return False, "No local session is open"
        return conversation_edits.undo_last_reply(self.agent.context)

    def undo_last_exchanges(self, count):
        return conversation_edits.undo_last_exchanges(self.agent.context, count)

    def take_last_request(self):
        if not self.active:
            return False, "No local session is open", ""
        return conversation_edits.take_last_request(self.agent.context)


def create_mode(agent):
    return LocalMode(
        FixtureController(agent), command="/fixture", label="Fixture",
        description="isolated protocol fixture", namespace="fixture",
        status_template="Fixture open · session {session} · tools off · memory off",
    )
