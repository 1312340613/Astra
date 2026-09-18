import asyncio
import copy
import json

import pytest

from agent.runtime.context import AgentContext
from agent.runtime.prompts import get_prompt_profile
from agent.runtime.session_store import SessionStore
from agent.runtime.system_suffix import SKILL_CATALOG_INTRO, split_legacy_suffix


def catalog(description="Core guidance"):
    return (
        f"<available-skills>\n{SKILL_CATALOG_INTRO}\n[builtin]\n"
        f"- astra-core: {description} [origin: builtin]\n</available-skills>"
    )


class EchoHeadCompressor:
    async def compress(self, messages, *args, **kwargs):
        return [copy.deepcopy(messages[0]), {"role": "user", "content": "summary"}]

    def reset(self):
        pass


def test_twenty_compactions_never_promote_effective_head_into_base(tmp_path):
    async def scenario():
        ctx = AgentContext(system_prompt="User identity and custom rules")
        ctx.set_session(str(tmp_path / "session.json"))
        ctx.set_stable_system_suffix(catalog())
        ctx.compressor = EchoHeadCompressor()
        for _ in range(20):
            ctx.add_user("continue")
            await ctx.compress_if_needed(force=True)
            assert ctx.system_prompt == "User identity and custom rules"
            assert ctx.effective_system_prompt.count("<available-skills>") == 1
        ctx.save()
        restored = AgentContext()
        restored.set_stable_system_suffix(catalog("New catalog"))
        restored.set_session(ctx.session_path)
        assert restored.load()
        assert restored.system_prompt == ctx.system_prompt
        assert restored.effective_system_prompt.count("<available-skills>") == 1
        assert "New catalog" in restored.effective_system_prompt
        assert "Core guidance" not in restored.effective_system_prompt
    asyncio.run(scenario())


def test_legacy_migration_keeps_reversible_backup_and_resets_projection(tmp_path):
    project = "## Project guidance: /workspace/AGENTS.md\nUser project instructions."
    base = "Custom persona\nDo not change me."
    original = base + ("\n\n" + project + "\n\n" + catalog("Old version")) * 4
    original += ("\n\n" + project + "\n\n" + catalog("Other old version")) * 4
    ctx = AgentContext(system_prompt=original)
    ctx.set_session(str(tmp_path / "legacy.json"))
    ctx.add_user("history stays")
    ctx.system_projection.project(ctx.get_prompt(), "series")
    ctx.save()
    header_path = SessionStore(ctx.session_path).header_path
    before = header_path.read_bytes()
    restored = AgentContext()
    restored.set_session(ctx.session_path)
    restored.set_stable_system_suffix(project + "\n\n" + catalog("Current"))
    assert restored.load()
    assert header_path.read_bytes() == before  # migration in memory until save
    assert restored.system_prompt == base
    assert restored.system_projection.state == {}
    assert restored.effective_system_prompt.count("<available-skills>") == 1
    assert restored.effective_system_prompt.count(project) == 1
    assert restored.messages[0]["content"] == "history stays"
    restored.save()
    header = json.loads(header_path.read_text())
    assert header["system_prompt_migration"]["original_system_prompt"] == original
    assert header["system_prompt_migration"]["skill_catalogs"] == 8
    assert header["system_prompt_migration"]["project_blocks"] == 8
    assert restored.load()
    assert restored._system_prompt_migration == header["system_prompt_migration"]
    assert original not in json.dumps(restored.get_prompt())


def test_persona_normalization_cannot_hide_contaminated_projection_or_backup(tmp_path):
    profile = get_prompt_profile("lyra")
    original = profile.system_prompt() + ("\n\n" + catalog("Old version")) * 2
    ctx = AgentContext(system_prompt=original)
    ctx.set_session(str(tmp_path / "persona.json"))
    ctx.add_user("history stays")
    ctx.system_projection.project(ctx.get_prompt(), "series")
    ctx.save()

    restored = AgentContext()
    restored.set_session(ctx.session_path)
    restored.set_stable_system_suffix(catalog("Current"))
    assert restored.load()
    assert restored.persona_id == profile.name
    assert restored.system_prompt == profile.system_prompt()
    assert restored.system_projection.state == {}
    assert restored._system_prompt_migration["original_system_prompt"] == original
    assert restored._system_prompt_migration["skill_catalogs"] == 2
    projected = restored.system_projection.project(restored.get_prompt(), "series")
    assert projected[0]["content"].count("<available-skills>") == 1
    assert "Old version" not in projected[0]["content"]
    restored.save()
    assert restored.load()
    assert restored._system_prompt_migration["original_system_prompt"] == original


@pytest.mark.parametrize("extension", [
    "\n\nCustom user instructions stay here.",
    "\n\n## Project guidance: /old/AGENTS.md\nUnknown historical project rules.",
    "\n\n<available-skills>\nUser-authored notes\n</available-skills>",
])
@pytest.mark.parametrize("local_profile", [False, True])
def test_persona_upgrade_preserves_unknown_extensions_during_migration(
    tmp_path, monkeypatch, extension, local_profile,
):
    name = "lyra"
    if local_profile:
        name = "local-example"
        config = tmp_path / "persona.local.json"
        config.write_text(json.dumps({"schema": 1, "profiles": [{
            "name": name, "description": "Local example", "persona": "User-defined example identity",
            "version": 9, "persona_layer": "identity",
        }]}))
        monkeypatch.setenv("ASTRA_PERSONA_FILE", str(config))
    profile = get_prompt_profile(name)
    base = profile.system_prompt() + extension
    original = base + "\n\n" + catalog("Old generated catalog")
    ctx = AgentContext(system_prompt=original)
    ctx.set_session(str(tmp_path / "custom-persona.json"))
    ctx.add_user("history")
    ctx.save()
    restored = AgentContext()
    restored.set_session(ctx.session_path)
    restored.set_stable_system_suffix(catalog("Current generated catalog"))
    assert restored.load()
    assert restored.system_prompt == base
    assert restored._system_prompt_migration["original_system_prompt"] == original
    restored.save()
    assert restored.load()
    assert restored.system_prompt == base


def test_catalog_migration_preserves_public_persona_retirement(tmp_path):
    old_base = "<!-- agent-persona:id=balanced;version=7;revision=4 -->\nRetired example body"
    original = old_base + ("\n\n" + catalog("Old catalog")) * 2
    path = tmp_path / "retired.json"
    path.write_text(json.dumps({
        "system_prompt": original,
        "persona_state": {"active_mode": "retired-mode", "relationship_context": "retired-state"},
        "messages": [{"role": "user", "content": "Keep this conversation."}],
    }))
    context = AgentContext()
    context.set_session(str(path))
    context.set_stable_system_suffix(catalog("Current"))
    assert context.load()
    assert context.persona_id == "lyra"
    assert context.persona_relationship_context == ""
    assert context.system_prompt == get_prompt_profile("lyra").system_prompt()
    assert context._system_prompt_migration["original_system_prompt"] == original
    assert context._system_prompt_migration["skill_catalogs"] == 2
    wire = json.dumps(context.get_prompt())
    assert "Retired example body" not in wire
    assert "Old catalog" not in wire
    assert context.effective_system_prompt.count("<available-skills>") == 1
    context.save()
    assert context.load()
    assert context._system_prompt_migration["original_system_prompt"] == original
    assert context.messages[0]["content"] == "Keep this conversation."


@pytest.mark.parametrize("text", [
    "Custom\n\n<available-skills>\nuser-authored tags\n</available-skills>",
    "Custom\n\n" + catalog() + "\nMy own instructions after the block.",
    "Custom\n\n## Project guidance: /other/AGENTS.md\nOld project text",
])
def test_ambiguous_or_custom_blocks_are_preserved_verbatim(text):
    assert split_legacy_suffix(text) == (text, 0, 0)


def test_unknown_project_block_is_preserved_when_retiring_catalog():
    project = "Custom\n\n## Project guidance: /old/AGENTS.md\nUnknown old text"
    assert split_legacy_suffix(project + "\n\n" + catalog()) == (project, 1, 0)


def test_no_suffix_modes_and_compaction_cannot_inject_system_instructions():
    async def scenario():
        for base in ("", "Local mode prompt"):
            ctx = AgentContext(system_prompt=base)
            ctx.compressor = EchoHeadCompressor()
            ctx.add_user("hello")
            await ctx.compress_if_needed(force=True)
            assert ctx.system_prompt == base
            assert ctx.effective_system_prompt == base
    asyncio.run(scenario())
