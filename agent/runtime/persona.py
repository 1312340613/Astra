"""Versioned persona definitions and deterministic prompt assembly.

Persona identity is durable configuration. Relationship/mode state is mutable
runtime data. Keeping them separate makes prompt upgrades and session restore
explicit instead of treating one large string as the source of truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PERSONA_METADATA = re.compile(
    r"<!-- agent-persona:id=(?P<id>[a-z0-9-]+);version=(?P<version>\d+);revision=(?P<revision>\d+) -->"
)


@dataclass(frozen=True)
class PersonaDefinition:
    persona_id: str
    version: int
    description: str
    identity: str
    primary_identity: str = ""
    style: str = ""
    relationship: str = ""
    mode_overlay: str = ""
    invariants: tuple[str, ...] = ()
    persona_identity: str = ""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", self.persona_id):
            raise ValueError("persona_id must use lowercase letters, digits, and hyphens")
        if self.version < 1:
            raise ValueError("persona version must be >= 1")
        if not self.identity.strip():
            raise ValueError("persona identity cannot be empty")


@dataclass(frozen=True)
class PersonaState:
    persona_id: str
    definition_version: int
    state_revision: int = 0
    active_mode: str = ""
    relationship_context: str = ""
    affect: str = ""

    def __post_init__(self) -> None:
        if self.definition_version < 1:
            raise ValueError("persona definition_version must be >= 1")
        if self.state_revision < 0:
            raise ValueError("persona state_revision must be >= 0")


@dataclass(frozen=True)
class PersonaMetadata:
    persona_id: str
    definition_version: int
    state_revision: int


class PersonaAssembler:
    """Build a stable persona prefix followed by optional volatile state."""

    def assemble(self, definition: PersonaDefinition, state: PersonaState | None = None) -> str:
        state = state or PersonaState(
            persona_id=definition.persona_id,
            definition_version=definition.version,
        )
        if state.persona_id != definition.persona_id:
            raise ValueError("persona state does not match definition")
        if state.definition_version != definition.version:
            raise ValueError("persona state version does not match definition")

        stable = [
            self._metadata_line(definition, state),
            f'<persona-stable id="{definition.persona_id}" version="{definition.version}">',
        ]
        if definition.primary_identity.strip():
            stable.extend([
                "[Primary identity]",
                definition.primary_identity.strip(),
                "",
            ])
        if definition.persona_identity.strip():
            stable.extend([
                "[Persona identity]",
                definition.persona_identity.strip(),
                "",
            ])
        stable.extend(["[Identity]", definition.identity.strip()])
        if definition.style.strip():
            stable.extend(["", "[Style]", definition.style.strip()])
        if definition.relationship.strip():
            stable.extend(["", "[Relationship baseline]", definition.relationship.strip()])
        if definition.invariants:
            stable.extend(["", "[Invariants]"])
            stable.extend(f"- {item.strip()}" for item in definition.invariants if item.strip())
        stable.append("</persona-stable>")

        volatile: list[str] = []
        mode = state.active_mode.strip()
        relationship = state.relationship_context.strip()
        affect = state.affect.strip()
        overlay = definition.mode_overlay.strip()
        if mode or relationship or affect or overlay:
            volatile = [f'<persona-state revision="{state.state_revision}">']
            if mode:
                volatile.extend(["[Active mode]", mode])
            if overlay:
                volatile.extend(["", "[Mode overlay]", overlay])
            if relationship:
                volatile.extend(["", "[Current relationship context]", relationship])
            if affect:
                volatile.extend(["", "[Current affect]", affect])
            volatile.append("</persona-state>")

        return "\n\n".join(("\n".join(stable), "\n".join(volatile))).strip()

    @staticmethod
    def _metadata_line(definition: PersonaDefinition, state: PersonaState) -> str:
        return (
            f"<!-- agent-persona:id={definition.persona_id};version={definition.version};"
            f"revision={state.state_revision} -->"
        )


def parse_persona_metadata(prompt: str) -> PersonaMetadata | None:
    match = _PERSONA_METADATA.search(str(prompt))
    if match is None:
        return None
    return PersonaMetadata(
        persona_id=match.group("id"),
        definition_version=int(match.group("version")),
        state_revision=int(match.group("revision")),
    )
