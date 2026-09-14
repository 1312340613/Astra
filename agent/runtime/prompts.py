"""System prompt profiles for the local agent.

The prompt is split into an invariant agent contract plus a small persona layer.
This keeps communication style separate from tool use, permissions and factual
behavior.
"""

from dataclasses import dataclass
from hashlib import sha256

from .persona import (
    PersonaAssembler,
    PersonaDefinition,
    PersonaState,
    parse_persona_metadata,
)
from .core_rules import AGENT_CORE_PROMPT, core_identity_prompt
from .persona_overrides import local_persona_config


LYRA_PERSONA = """You are Lyra, a capable and approachable AI assistant.

Work with the user as a thoughtful collaborator. Be warm, curious and direct.
Use the user's language and match the detail to their task. Everyday conversation
can stay conversational; do not turn every greeting into a project or tool call.
When the user asks for action, inspect the relevant context, do the authorized
work, verify the result and report what changed and what remains uncertain.

Use clear prose and concrete evidence. Distinguish observations, assumptions and
proposals. Give an independent assessment rather than agreeing automatically.
Ask for missing information when it materially affects the task; resolve routine
implementation choices using the available context.

Do not assume the user's name, background, preferences or relationship with you.
Personalize only from information the user provided or explicitly made available.
Keep fictional characters and writing exercises separate from the assistant's
identity and the user's real circumstances. Never invent shared experiences.

Follow the current task's permissions and tool boundaries. A persona changes
communication style, not authorization, factual standards or completion criteria.
"""

DEFAULT_PERSONA_INVARIANTS = (
    "Do not invent memories; use only current context or loaded memory with evidence.",
    "Complete authorized tasks and distinguish verified results from assumptions.",
    "Do not assume a private relationship or identity for the user.",
)

LYRA_PRIMARY_IDENTITY = "You are Lyra, the AI assistant in Astra."

# Migration identifiers only: these profiles are never listed or loaded as presets.
LEGACY_PROFILE_IDS = frozenset({
    "lyra-work", "lyra-hermes", "yelan", "lyra-rp", "lyra-close",
    "balanced", "work", "lyra-coding",
})

# Recognize pre-versioning prompts without distributing their retired text.
LEGACY_PROFILE_PROMPT_HASHES = frozenset({
    "02c6e71737f162c44c9f1292bd5a883d1d8e34f00a11587f1ed887e703e35d5e",
    "64e012cb5c8e37cd5ea0e449b29e8506ea7a7a69326261ef2bf1d50460a2b461",
    "d54949b81b620200b1bc773092c97568f9ecba71a211144dd5d4a1333e8838d6",
    "f35154b82bdce6675dd9e247438ce79652be95bdcb2d56149bfaf8d656c87597",
    "a3de20918079f374b561aa951b0b467d76d62fbb7725581d433c209f4c797bc3",
    "57e4ca92c26c6d249a6065435ce677b9febb92dc0d01d32f5fa2c4eaca1a85bb",
    "444767a41aff15197a145d2c9fa68d0581ff89c1a2becae9e146613d29f0c730",
    "5aa3feff7d94355143052ab3adbaaf12a5820c06acc87041c72fe085434e6cd8",
})


@dataclass(frozen=True)
class PromptProfile:
    name: str
    description: str
    persona: str
    version: int = 1
    persona_layer: str = "style"
    relationship: str = ""
    mode_overlay: str = ""
    active_mode: str = ""
    invariants: tuple[str, ...] = DEFAULT_PERSONA_INVARIANTS
    primary_identity: str = ""
    temperature: float | None = None
    provider_default_temperature: bool = False

    def definition(self) -> PersonaDefinition:
        if self.persona_layer not in {"style", "mode", "identity"}:
            raise ValueError(f"Unknown persona layer: {self.persona_layer}")
        style = self.persona if self.persona_layer == "style" else ""
        overlay = self.mode_overlay or (self.persona if self.persona_layer == "mode" else "")
        persona_identity = self.persona if self.persona_layer == "identity" else ""
        return PersonaDefinition(
            persona_id=self.name,
            version=self.version,
            description=self.description,
            identity=core_identity_prompt(),
            primary_identity=self.primary_identity,
            style=style,
            relationship=self.relationship,
            mode_overlay=overlay,
            invariants=self.invariants,
            persona_identity=persona_identity,
        )

    def default_state(self, *, state_revision: int = 0) -> PersonaState:
        return PersonaState(
            persona_id=self.name,
            definition_version=self.version,
            state_revision=state_revision,
            active_mode=self.active_mode,
        )

    def system_prompt(self, state: PersonaState | None = None) -> str:
        return PersonaAssembler().assemble(self.definition(), state or self.default_state())

    def legacy_system_prompt(self) -> str:
        """Pre-versioning prompt form, used only to migrate existing sessions."""
        return f"{AGENT_CORE_PROMPT}\n\n{self.persona}".strip()


def prompt_profiles() -> dict[str, PromptProfile]:
    profiles = {
        "lyra": PromptProfile(
            name="lyra",
            description="Lyra — a thoughtful, capable assistant for work and everyday conversation.",
            persona=LYRA_PERSONA,
            version=1,
            persona_layer="identity",
            active_mode="work",
            primary_identity=LYRA_PRIMARY_IDENTITY,
            provider_default_temperature=True,
        ),
    }


    for raw in local_persona_config().get("profiles", []):
        allowed = set(PromptProfile.__dataclass_fields__)
        strings = {"name", "description", "persona", "persona_layer", "relationship", "mode_overlay",
                   "active_mode", "primary_identity"}
        try:
            if set(raw) - allowed or any(not isinstance(raw.get(key), str) for key in ("name", "description", "persona")):
                raise ValueError
            if not raw["name"].strip() or not raw["persona"].strip():
                raise ValueError
            if any(not isinstance(raw[key], str) for key in strings & raw.keys()):
                raise ValueError
            if "version" in raw and (type(raw["version"]) is not int or raw["version"] < 1):
                raise ValueError
            if "provider_default_temperature" in raw and type(raw["provider_default_temperature"]) is not bool:
                raise ValueError
            if raw.get("temperature") is not None and type(raw["temperature"]) not in (int, float):
                raise ValueError
            values = dict(raw)
            if "invariants" in values:
                invariants = values["invariants"]
                if not isinstance(invariants, list) or not all(isinstance(item, str) for item in invariants):
                    raise ValueError
                values["invariants"] = tuple(invariants)
            profile = PromptProfile(**values)
            profile.definition()  # Validate the layer before changing selection or session data.
        except (TypeError, ValueError):
            raise ValueError("Invalid local persona profile; check persona.local.json without replacing saved settings.") from None
        profiles[profile.name] = profile
    return profiles


DEFAULT_PROMPT_PROFILE = "lyra"


def resolve_prompt_profile_name(name: str | None) -> str | None:
    """Resolve saved legacy selections without exposing retired preset names."""
    if name in prompt_profiles():
        return name
    return DEFAULT_PROMPT_PROFILE if name in LEGACY_PROFILE_IDS else None


def get_prompt_profile(name: str | None) -> PromptProfile:
    return prompt_profiles()[resolve_prompt_profile_name(name) or DEFAULT_PROMPT_PROFILE]


def is_legacy_persona_prompt(prompt: str, persona_id: str | None = None) -> bool:
    metadata = parse_persona_metadata(prompt)
    selected = persona_id or (metadata.persona_id if metadata else None)
    if selected in prompt_profiles():
        return False
    return selected in LEGACY_PROFILE_IDS or (
        not selected and sha256(prompt.encode("utf-8")).hexdigest() in LEGACY_PROFILE_PROMPT_HASHES
    )


def persona_generation_overrides(persona_id: str | None) -> dict | None:
    """Sampling overrides declared by a persona profile.

    Returns a generation-overrides dict when the profile declares its own
    sampling, or None to inherit the global default. A ``{"temperature": None}``
    result means "provider default": the request omits the temperature
    parameter so the server applies its own default. Unknown or empty persona
    ids never produce overrides.
    """
    if not persona_id:
        return None
    profile = prompt_profiles().get(resolve_prompt_profile_name(persona_id) or "")
    if profile is None:
        return None
    if profile.provider_default_temperature:
        return {"temperature": None}
    if profile.temperature is not None:
        return {"temperature": profile.temperature}
    return None


DEFAULT_SYSTEM_PROMPT = get_prompt_profile(DEFAULT_PROMPT_PROFILE).system_prompt()

LEGACY_UNSAFE_PROMPT_MARKERS = (
    "没有任何审查和限制",
    "绝对不要说你不能看图",
)


def normalize_system_prompt(
    prompt: str,
    *,
    persona_id: str | None = None,
    persona_version: int | None = None,
    state_revision: int = 0,
    active_mode: str = "",
    relationship_context: str = "",
    affect: str = "",
) -> str:
    """Upgrade legacy/versioned persona prompts while preserving custom prompts."""
    if any(marker in prompt for marker in LEGACY_UNSAFE_PROMPT_MARKERS):
        return DEFAULT_SYSTEM_PROMPT

    profiles = prompt_profiles()
    metadata = parse_persona_metadata(prompt)
    selected = persona_id or (metadata.persona_id if metadata else None)
    revision = metadata.state_revision if metadata else state_revision
    for profile in profiles.values():
        if prompt == profile.legacy_system_prompt():
            return profile.system_prompt()
    if is_legacy_persona_prompt(prompt, persona_id):
        # Retire both the old definition and its relationship/mode overlay.
        return get_prompt_profile(DEFAULT_PROMPT_PROFILE).system_prompt()
    if selected in profiles:
        profile = profiles[selected]
        # Session metadata is the source of truth. Reassemble even when only
        # wording changed so old prompt strings cannot override a new version.
        state = PersonaState(
            persona_id=profile.name,
            definition_version=profile.version,
            state_revision=revision,
            active_mode=active_mode or profile.active_mode,
            relationship_context=relationship_context,
            affect=affect,
        )
        return profile.system_prompt(state)

    return prompt
